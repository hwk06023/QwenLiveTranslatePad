"""On-prem translation engine running on the local NVIDIA GPU.

Cascaded pipeline, all Qwen open-weight models:
    Qwen3-ASR (speech -> text, language ID)
    -> Qwen3 Instruct LLM (streaming text translation)
    -> Qwen3-TTS CustomVoice (sentence-by-sentence speech synthesis)

torch/transformers are imported lazily so the cloud-only deployment (Vercel)
never needs them.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import logging
import re
import threading
import time
from contextlib import suppress
from typing import Any, Callable, Iterator

import numpy as np
from fastapi import WebSocketDisconnect

from .config import Settings
from .metrics import ClientChannel

log = logging.getLogger(__name__)

INPUT_RATE = 16_000
AUDIO_CHUNK_SAMPLES = 12_000  # 0.5 s of 24 kHz audio per browser message
PARTIAL_INTERVAL_S = 1.5
PARTIAL_MIN_AUDIO_S = 1.0

# Languages Qwen3-TTS can speak; these are the valid local translation targets.
# code -> (English name used in prompts / TTS / ASR, preferred preset voice)
LOCAL_LANGUAGES: dict[str, tuple[str, str]] = {
    "ko": ("Korean", "Sohee"),
    "en": ("English", "Ryan"),
    "ja": ("Japanese", "Ono_Anna"),
    "zh": ("Chinese", "Vivian"),
    "de": ("German", "Ryan"),
    "fr": ("French", "Serena"),
    "ru": ("Russian", "Aiden"),
    "pt": ("Portuguese", "Serena"),
    "es": ("Spanish", "Aiden"),
    "it": ("Italian", "Serena"),
}

# Qwen3-ASR language names -> ISO codes, for reporting the detected language.
ASR_LANGUAGE_CODES = {
    "Chinese": "zh", "English": "en", "Cantonese": "yue", "Arabic": "ar",
    "German": "de", "French": "fr", "Spanish": "es", "Portuguese": "pt",
    "Indonesian": "id", "Italian": "it", "Korean": "ko", "Russian": "ru",
    "Thai": "th", "Vietnamese": "vi", "Japanese": "ja", "Turkish": "tr",
    "Hindi": "hi", "Malay": "ms", "Dutch": "nl", "Swedish": "sv",
    "Danish": "da", "Finnish": "fi", "Polish": "pl", "Czech": "cs",
    "Filipino": "fil", "Persian": "fa", "Greek": "el", "Romanian": "ro",
    "Hungarian": "hu", "Macedonian": "mk",
}
CODE_TO_ASR_NAME = {code: name for name, code in ASR_LANGUAGE_CODES.items()}

TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional interpreter at a Korean public service (civil affairs) "
    "counter, translating between a staff member and a visitor. "
    "Translate the user's utterance{source} into {target}. "
    "Keep names, numbers, dates and amounts exact. Use natural, polite spoken {target}. "
    "Output only the {target} translation - no notes, quotes or explanations."
)

# Sentence boundaries for incremental TTS. CJK enders need no trailing space;
# Latin enders must be followed by whitespace so "3.5" does not split.
_SENTENCE_END = re.compile(r"[。！？!?…]+|\.(?=\s)|\n+")
_SOFT_BREAK = re.compile(r"[,，、;；:：](?=\s|[^\d])")
_SOFT_BREAK_MIN_CHARS = 40
_MIN_TTS_CHARS = 10

# Typical speaking rate (characters per second) used to cap TTS length: sampled
# codec generation occasionally runs away on short inputs (seen: 29 s of audio
# for "こんにちは。").
_CHARS_PER_SECOND = {"zh": 5.0, "ja": 7.0, "ko": 6.0}
_DEFAULT_CHARS_PER_SECOND = 14.0
_CODEC_FRAMES_PER_SECOND = 12.5


def tts_max_frames(text: str, target: str) -> int:
    expected_s = len(text) / _CHARS_PER_SECOND.get(target, _DEFAULT_CHARS_PER_SECOND)
    return int((1.5 + 2.0 * expected_s) * _CODEC_FRAMES_PER_SECOND)


def pop_sentences(
    buffer: str, final: bool = False, soft: bool = False
) -> tuple[list[str], str]:
    """Split complete sentences off the front of a streaming text buffer.

    soft=True also allows one break at a comma in a long clause, used only for
    the first chunk so the first audio starts sooner without choppy prosody later.
    """
    sentences: list[str] = []
    while True:
        # Very short chunks ("Hello.") make sampled TTS unstable; merge them forward.
        start = 0
        while (match := _SENTENCE_END.search(buffer, start)) and len(
            buffer[: match.end()].strip()
        ) < _MIN_TTS_CHARS:
            start = match.end()
        if not match and soft and not sentences and len(buffer) >= _SOFT_BREAK_MIN_CHARS:
            match = _SOFT_BREAK.search(buffer, _SOFT_BREAK_MIN_CHARS // 2)
        if not match:
            break
        cut = match.end()
        sentence = buffer[:cut].strip()
        buffer = buffer[cut:]
        if sentence:
            sentences.append(sentence)
    if final and buffer.strip():
        sentences.append(buffer.strip())
        buffer = ""
    return sentences, buffer


class LocalModels:
    """Process-wide holder for the three GPU models."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = "unloaded"  # unloaded | loading | ready | error
        self.error: str | None = None
        self.load_ms: dict[str, float] = {}
        self.loaded_at: float | None = None
        self.turns_served = 0
        self.asr: Any = None
        self.llm: Any = None
        self.tokenizer: Any = None
        self.tts: Any = None
        self.tts_streamer: Any = None
        self._load_lock = threading.Lock()
        # Partial captions may overlap another session's final ASR.
        self._asr_lock = threading.Lock()
        # One turn at a time on the GPU; partial captions skip when it is busy.
        self.turn_lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def ensure_loaded(self) -> float:
        """Load all models if needed. Returns milliseconds spent loading (0 if warm)."""
        if self.ready:
            return 0.0
        with self._load_lock:
            if self.ready:
                return 0.0
            started = time.perf_counter()
            self.state = "loading"
            self.error = None
            try:
                self._load()
            except Exception as exc:
                log.exception("Local model load failed")
                self.state = "error"
                self.error = f"{type(exc).__name__}: {exc}"
                self._release()
                raise
            self.state = "ready"
            self.loaded_at = time.time()
            return (time.perf_counter() - started) * 1000

    def _load(self) -> None:
        import torch
        from qwen_asr import Qwen3ASRModel
        from qwen_tts import Qwen3TTSModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is not available to PyTorch")

        s = self.settings
        dtype = torch.bfloat16
        self.load_ms = {}

        t = time.perf_counter()
        self.asr = Qwen3ASRModel.from_pretrained(
            s.local_asr_model,
            dtype=dtype,
            device_map=s.local_device,
            max_inference_batch_size=1,
            max_new_tokens=384,
        )
        self.load_ms["asr"] = _elapsed_ms(t)

        t = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(s.local_llm_model)
        self.llm = AutoModelForCausalLM.from_pretrained(
            s.local_llm_model, dtype=dtype, device_map=s.local_device
        )
        self.llm.eval()
        self.load_ms["llm"] = _elapsed_ms(t)

        t = time.perf_counter()
        self.tts = Qwen3TTSModel.from_pretrained(
            s.local_tts_model, dtype=dtype, device_map=s.local_device
        )
        from .tts_speedup import StreamingSynthesizer, patch_code_predictor

        if s.local_tts_cuda_graph:
            patch_code_predictor(self.tts.model.talker.code_predictor)
        self.tts_streamer = StreamingSynthesizer(self.tts)
        self.load_ms["tts"] = _elapsed_ms(t)

        # Run each model once so CUDA kernels/graphs are compiled before the first turn.
        t = time.perf_counter()
        self.transcribe(np.zeros(INPUT_RATE, dtype=np.float32), "Korean")
        "".join(self.translate_stream("안녕하세요.", "ko", "en", max_new_tokens=8))
        self.synthesize_stream("Hello, how can I help you?", "en", lambda _: None)
        self.load_ms["warmup"] = _elapsed_ms(t)

    def unload(self) -> None:
        with self._load_lock:
            self._release()
            self.state = "unloaded"
            self.loaded_at = None

    def _release(self) -> None:
        self.asr = self.llm = self.tokenizer = self.tts = self.tts_streamer = None
        gc.collect()
        with suppress(Exception):
            import torch

            torch.cuda.empty_cache()

    # ---- inference (blocking; call from a worker thread) ------------------

    def transcribe(self, wav16k: np.ndarray, language: str | None) -> tuple[str, str]:
        """Returns (text, raw language string such as 'Korean' or 'Chinese,English')."""
        with self._asr_lock:
            result = self.asr.transcribe(audio=(wav16k, INPUT_RATE), language=language)[0]
        return result.text.strip(), (result.language or "").strip()

    def translate_stream(
        self,
        text: str,
        source: str | None,
        target: str,
        max_new_tokens: int | None = None,
        on_first_token: Callable[[], None] | None = None,
    ) -> Iterator[str]:
        import torch
        from transformers import TextIteratorStreamer

        target_name = LOCAL_LANGUAGES[target][0]
        source_name = CODE_TO_ASR_NAME.get(source or "", "")
        system = TRANSLATE_SYSTEM_PROMPT.format(
            source=f" from {source_name}" if source_name else "",
            target=target_name,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.llm.device)
        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True
        )
        limit = max_new_tokens or min(768, 64 + 3 * len(text))
        error: list[BaseException] = []

        def generate() -> None:
            try:
                with torch.inference_mode():
                    self.llm.generate(
                        **inputs,
                        streamer=streamer,
                        max_new_tokens=limit,
                        do_sample=False,
                        temperature=None,
                        top_p=None,
                        top_k=None,
                    )
            except BaseException as exc:  # surface errors to the consumer
                error.append(exc)
                streamer.end()

        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        first = True
        for piece in streamer:
            if piece and first:
                first = False
                if on_first_token:
                    on_first_token()
            if piece:
                yield piece
        worker.join()
        if error:
            raise error[0]

    def synthesize_stream(
        self, text: str, target: str, on_chunk: Callable[[np.ndarray], None]
    ) -> int:
        """Synthesize speech, calling on_chunk(int16 PCM) as audio becomes ready.

        Returns the sample rate.
        """
        language, preferred = LOCAL_LANGUAGES[target]
        self.tts_streamer.synthesize(
            text,
            language,
            self._speaker(preferred),
            lambda wav: on_chunk((np.clip(wav, -1.0, 1.0) * 32767).astype("<i2")),
            max_new_tokens=tts_max_frames(text, target),
        )
        return self.tts_streamer.sample_rate

    def _speaker(self, preferred: str) -> str:
        configured = self.settings.local_tts_speaker
        speaker = preferred if configured.lower() == "auto" else configured
        supported = getattr(self.tts.model, "get_supported_speakers", lambda: None)()
        if supported:
            lookup = {name.lower(): name for name in supported}
            return lookup.get(speaker.lower(), sorted(supported)[0])
        return speaker

    # ---- reporting -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self.settings
        info: dict[str, Any] = {
            "enabled": s.local_enabled,
            "state": self.state,
            "error": self.error,
            "device": s.local_device,
            "models": {
                "asr": s.local_asr_model,
                "llm": s.local_llm_model,
                "tts": s.local_tts_model,
            },
            "load_ms": self.load_ms,
            "loaded_at": self.loaded_at,
            "turns_served": self.turns_served,
            "languages": {code: name for code, (name, _) in LOCAL_LANGUAGES.items()},
            "busy": self.turn_lock.locked(),
        }
        torch = _torch_if_imported()
        if torch is not None and torch.cuda.is_available():
            info["torch"] = torch.__version__
            info["cuda_device"] = torch.cuda.get_device_name(0)
            info["gpu_allocated_gb"] = round(torch.cuda.memory_allocated() / 1e9, 2)
            info["gpu_reserved_gb"] = round(torch.cuda.memory_reserved() / 1e9, 2)
        return info


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _torch_if_imported() -> Any:
    import sys

    return sys.modules.get("torch")


_MODELS: LocalModels | None = None


def get_local_models(settings: Settings) -> LocalModels:
    global _MODELS
    if _MODELS is None:
        _MODELS = LocalModels(settings)
    return _MODELS


class LocalPipelineBridge:
    """Runs one push-to-talk turn through the on-prem pipeline."""

    def __init__(
        self,
        client: ClientChannel,
        settings: Settings,
        target_language: str,
        source_language: str,
    ):
        self.client = client
        self.rec = client.rec
        self.settings = settings
        self.models = get_local_models(settings)
        self.target = target_language.lower().strip()
        source = source_language.lower().strip()
        self.source_hint = CODE_TO_ASR_NAME.get(source)  # None -> auto-detect
        self.max_samples = settings.local_max_audio_seconds * INPUT_RATE
        self._pcm = bytearray()
        self._partial_task: asyncio.Task | None = None
        self._partial_len = 0

    async def run(self) -> None:
        if self.target not in LOCAL_LANGUAGES:
            await self.client.send_json(
                {
                    "type": "error",
                    "message": f"On-prem engine does not support target language: {self.target}",
                }
            )
            return

        self.rec.info.update(
            {
                "asr_model": _short(self.settings.local_asr_model),
                "llm_model": _short(self.settings.local_llm_model),
                "tts_model": _short(self.settings.local_tts_model),
                "source_hint": self.source_hint or "auto",
            }
        )
        try:
            if not self.models.ready:
                await self.client.send_json({"type": "processing", "stage": "loading"})
                # Measure the wait, not the load: a preload thread may already be loading.
                started = time.perf_counter()
                await asyncio.to_thread(self.models.ensure_loaded)
                self.rec.stage("model_load", _elapsed_ms(started))
                self.rec.info["cold_start"] = True

            await self.client.send_json(
                {
                    "type": "ready",
                    "engine": "local",
                    "target": self.target,
                    "input_sample_rate": INPUT_RATE,
                    "output_sample_rate": 24_000,
                    "max_audio_seconds": self.settings.local_max_audio_seconds,
                }
            )

            if not await self._record():
                return  # browser left before releasing the button
            await self._process()
        except WebSocketDisconnect:
            log.info("Browser disconnected")
        except Exception as exc:
            log.exception("Local pipeline failed")
            await self.client.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            if self._partial_task and not self._partial_task.done():
                self._partial_task.cancel()

    # ---- recording --------------------------------------------------------

    async def _record(self) -> bool:
        """Collect PCM until the browser sends finish. Returns False on disconnect."""
        last_partial = time.perf_counter()
        while True:
            message = await self.client.receive()
            if message.get("type") == "websocket.disconnect":
                return False

            pcm = message.get("bytes")
            if pcm:
                if len(self._pcm) // 2 < self.max_samples:
                    self._pcm.extend(pcm)
                now = time.perf_counter()
                if self._should_run_partial(now - last_partial):
                    last_partial = now
                    self._partial_task = asyncio.create_task(self._partial_caption())
                continue

            text = message.get("text")
            if text:
                with suppress(json.JSONDecodeError):
                    if json.loads(text).get("type") == "finish":
                        return True

    def _should_run_partial(self, since_last: float) -> bool:
        if not self.settings.local_partial_asr or since_last < PARTIAL_INTERVAL_S:
            return False
        if self._partial_task and not self._partial_task.done():
            return False
        if self.models.turn_lock.locked():
            return False  # another turn owns the GPU
        new_audio = (len(self._pcm) - self._partial_len) / 2 / INPUT_RATE
        return len(self._pcm) / 2 / INPUT_RATE >= PARTIAL_MIN_AUDIO_S and new_audio > 0.3

    async def _partial_caption(self) -> None:
        self._partial_len = len(self._pcm)
        wav = _pcm_to_float(bytes(self._pcm))
        started = time.perf_counter()
        try:
            text, _ = await asyncio.to_thread(self.models.transcribe, wav, self.source_hint)
        except Exception:
            log.debug("Partial ASR failed", exc_info=True)
            return
        self.rec.stage("partial_asr", _elapsed_ms(started))
        self.rec.info["partial_asr_runs"] = self.rec.info.get("partial_asr_runs", 0) + 1
        if text:
            await self.client.send_json({"type": "source_partial", "text": text})

    # ---- processing ---------------------------------------------------------

    async def _process(self) -> None:
        if self._partial_task and not self._partial_task.done():
            waited = time.perf_counter()
            with suppress(Exception):
                await self._partial_task
            self.rec.stage("partial_drain", _elapsed_ms(waited))

        if len(self._pcm) < INPUT_RATE // 5 * 2:  # < 0.2 s
            await self.client.send_json({"type": "error", "message": "음성이 너무 짧습니다."})
            return

        if self.models.turn_lock.locked():
            await self.client.send_json({"type": "processing", "stage": "queued"})
        queued = time.perf_counter()
        async with self.models.turn_lock:
            self.rec.stage("queue_wait", _elapsed_ms(queued))
            await self._run_pipeline()
            self.models.turns_served += 1

        await self.client.send_json({"type": "response_done"})
        await self.client.send_json({"type": "session_finished"})

    async def _run_pipeline(self) -> None:
        wav = _pcm_to_float(bytes(self._pcm))

        # 1) ASR
        await self.client.send_json({"type": "processing", "stage": "transcribing"})
        started = time.perf_counter()
        text, raw_language = await asyncio.to_thread(
            self.models.transcribe, wav, self.source_hint
        )
        self.rec.stage("asr", _elapsed_ms(started))
        detected = ASR_LANGUAGE_CODES.get(raw_language.split(",")[0].strip())
        source_code = detected or ASR_LANGUAGE_CODES.get(self.source_hint or "")
        await self.client.send_json(
            {"type": "source_done", "text": text, "language": source_code}
        )
        if not text:
            await self.client.send_json({"type": "error", "message": "음성을 인식하지 못했습니다."})
            return

        # 2) LLM translation streamed into 3) sentence-level TTS
        await self.client.send_json({"type": "processing", "stage": "translating"})
        loop = asyncio.get_running_loop()
        deltas: asyncio.Queue[str | None] = asyncio.Queue()
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        llm_started = time.perf_counter()
        first_token_at: list[float] = []

        def produce() -> None:
            try:
                for piece in self.models.translate_stream(
                    text,
                    source_code,
                    self.target,
                    on_first_token=lambda: first_token_at.append(time.perf_counter()),
                ):
                    loop.call_soon_threadsafe(deltas.put_nowait, piece)
            finally:
                loop.call_soon_threadsafe(deltas.put_nowait, None)

        producer = asyncio.create_task(asyncio.to_thread(produce))
        tts_task = asyncio.create_task(self._speak(sentences))

        translation = ""
        buffer = ""
        queued = 0
        while (piece := await deltas.get()) is not None:
            translation += piece
            buffer += piece
            await self.client.send_json({"type": "translation_delta", "text": piece})
            ready, buffer = pop_sentences(buffer, soft=not queued)
            for sentence in ready:
                sentences.put_nowait(sentence)
                queued += 1
        await producer  # re-raises LLM errors

        llm_ms = _elapsed_ms(llm_started)
        self.rec.stage("llm", llm_ms)
        if first_token_at:
            self.rec.stage("llm_ttft", round((first_token_at[0] - llm_started) * 1000, 1))
        tokens = len(self.models.tokenizer(translation)["input_ids"]) if translation else 0
        self.rec.info["llm_tokens"] = tokens
        if llm_ms > 0 and tokens:
            self.rec.info["llm_tok_per_s"] = round(tokens / (llm_ms / 1000), 1)

        await self.client.send_json({"type": "translation_done", "text": translation.strip()})
        rest, _ = pop_sentences(buffer, final=True)
        for sentence in rest:
            sentences.put_nowait(sentence)
        sentences.put_nowait(None)
        await tts_task

    async def _speak(self, sentences: asyncio.Queue[str | None]) -> None:
        synth_ms = 0.0
        audio_s = 0.0
        count = 0
        loop = asyncio.get_running_loop()
        sample_rate = 24_000
        while (sentence := await sentences.get()) is not None:
            if count == 0:
                await self.client.send_json({"type": "processing", "stage": "synthesizing"})
            chunks: asyncio.Queue[np.ndarray | None] = asyncio.Queue()
            started = time.perf_counter()

            def run(text: str = sentence) -> int:
                try:
                    return self.models.synthesize_stream(
                        text, self.target, lambda pcm: loop.call_soon_threadsafe(chunks.put_nowait, pcm)
                    )
                finally:
                    loop.call_soon_threadsafe(chunks.put_nowait, None)

            worker = asyncio.create_task(asyncio.to_thread(run))
            while (pcm := await chunks.get()) is not None:
                if count == 0 and "tts_first" not in self.rec.stages:
                    self.rec.stage("tts_first", _elapsed_ms(started))
                audio_s += len(pcm) / sample_rate
                for offset in range(0, len(pcm), AUDIO_CHUNK_SAMPLES):
                    piece = pcm[offset : offset + AUDIO_CHUNK_SAMPLES]
                    await self.client.send_json(
                        {
                            "type": "audio",
                            "audio": base64.b64encode(piece.tobytes()).decode("ascii"),
                            "sample_rate": sample_rate,
                        }
                    )
            sample_rate = await worker  # re-raises synthesis errors
            synth_ms += _elapsed_ms(started)
            count += 1
        self.rec.stage("tts", synth_ms)
        self.rec.info["tts_sentences"] = count
        if audio_s:
            self.rec.info["tts_rtf"] = round(synth_ms / 1000 / audio_s, 3)


def _pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _short(model: str) -> str:
    return model.rstrip("/").rsplit("/", 1)[-1]
