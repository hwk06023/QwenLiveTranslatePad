"""Lower per-frame overhead in Qwen3-TTS generation.

For every 80 ms codec frame, the talker calls `code_predictor.generate(...)`
(Hugging Face generate) to emit 15 residual codebooks. The model is tiny
(5 layers), so on Jetson Thor the time goes to generate()'s Python setup
(logits processors, stopping criteria, cache bookkeeping) rather than the GPU,
keeping synthesis at ~1.1x real time.

`patch_code_predictor` replaces that call with a minimal decode loop that
samples the same way (temperature -> top-k -> top-p). For batch size 1 the
whole 15-step loop runs over a StaticCache with fixed shapes, so it is captured
once as a CUDA graph and replayed per frame; if capture fails it falls back to
the eager loop.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import torch
from transformers.cache_utils import DynamicCache, StaticCache

log = logging.getLogger(__name__)


def _sample(
    logits: torch.Tensor,
    do_sample: bool,
    temperature: float | None,
    top_k: int | None,
    top_p: float | None,
) -> torch.Tensor:
    if not do_sample:
        return logits.argmax(dim=-1)
    logits = logits.float()
    if temperature and temperature != 1.0:
        logits = logits / temperature
    if top_k and 0 < top_k < logits.shape[-1]:
        kth = torch.topk(logits, top_k, dim=-1).values[..., -1:]
        logits = logits.masked_fill(logits < kth, float("-inf"))
    if top_p is not None and top_p < 1.0:
        sorted_logits, sorted_idx = torch.sort(logits, descending=True, dim=-1)
        cumulative = sorted_logits.softmax(dim=-1).cumsum(dim=-1)
        remove = cumulative - sorted_logits.softmax(dim=-1) > top_p
        sorted_logits = sorted_logits.masked_fill(remove, float("-inf"))
        logits = torch.full_like(logits, float("-inf")).scatter(-1, sorted_idx, sorted_logits)
    return torch.multinomial(logits.softmax(dim=-1), num_samples=1).squeeze(-1)


def _decode(code_predictor: Any, inputs_embeds: torch.Tensor, cache: Any, max_new_tokens: int,
            sampling: tuple, cache_positions: list[torch.Tensor] | None = None) -> torch.Tensor:
    # Prefill with [talker hidden, codebook-0 embedding] -> codebook-1 logits.
    kwargs = {"cache_position": cache_positions[0]} if cache_positions else {}
    out = code_predictor(inputs_embeds=inputs_embeds, past_key_values=cache, use_cache=True, **kwargs)
    steps = out.generation_steps
    tokens = []
    for i in range(max_new_tokens):
        token = _sample(out.logits[:, -1, :], *sampling)
        tokens.append(token)
        if i == max_new_tokens - 1:
            break
        if cache_positions:
            kwargs = {"cache_position": cache_positions[i + 1]}
        out = code_predictor(
            input_ids=token[:, None],
            past_key_values=cache,
            use_cache=True,
            generation_steps=steps,
            **kwargs,
        )
        steps = out.generation_steps
    return torch.stack(tokens, dim=1)


class _GraphedDecoder:
    """One CUDA graph per (sampling params) for batch-1 codebook decoding."""

    def __init__(self, code_predictor: Any, max_new_tokens: int, sampling: tuple, example: torch.Tensor):
        self.cp = code_predictor
        device = example.device
        self.static_in = torch.zeros_like(example)
        self.cache = StaticCache(config=code_predictor.config, max_cache_len=max_new_tokens + 1)
        self.positions = [torch.arange(0, 2, device=device)] + [
            torch.tensor([2 + i], device=device) for i in range(max_new_tokens - 1)
        ]
        run = lambda: _decode(self.cp, self.static_in, self.cache, max_new_tokens, sampling, self.positions)

        self.static_in.copy_(example)
        stream = torch.cuda.Stream(device)
        stream.wait_stream(torch.cuda.current_stream(device))
        with torch.cuda.stream(stream):
            for _ in range(2):  # warm up: allocates the static cache, selects kernels
                run()
        torch.cuda.current_stream(device).wait_stream(stream)

        self.graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(self.graph, capture_error_mode="thread_local"):
            self.static_out = run()

    def __call__(self, inputs_embeds: torch.Tensor) -> torch.Tensor:
        self.static_in.copy_(inputs_embeds)
        self.graph.replay()
        return self.static_out.clone()


def patch_code_predictor(code_predictor: Any, use_cuda_graph: bool = True) -> None:
    """Swap `code_predictor.generate` for a lean loop (idempotent)."""
    if getattr(code_predictor, "_fast_generate", False):
        return
    graphs: dict[tuple, Any] = {}

    @torch.inference_mode()
    def fast_generate(
        inputs_embeds: torch.Tensor,
        max_new_tokens: int,
        do_sample: bool = True,
        top_p: float | None = 1.0,
        top_k: int | None = 50,
        temperature: float | None = 0.9,
        **_: Any,
    ) -> SimpleNamespace:
        sampling = (do_sample, temperature, top_k, top_p)
        key = (inputs_embeds.shape, inputs_embeds.dtype, max_new_tokens, sampling)
        if use_cuda_graph and inputs_embeds.is_cuda and inputs_embeds.shape[0] == 1:
            graphed = graphs.get(key)
            if graphed is None:
                try:
                    graphed = graphs[key] = _GraphedDecoder(
                        code_predictor, max_new_tokens, sampling, inputs_embeds
                    )
                    log.info("Captured TTS code-predictor CUDA graph for %s", key)
                except Exception:
                    log.warning("CUDA graph capture failed; using eager decode", exc_info=True)
                    graphed = graphs[key] = False
            if graphed:
                return SimpleNamespace(sequences=graphed(inputs_embeds))
        sequences = _decode(code_predictor, inputs_embeds, DynamicCache(), max_new_tokens, sampling)
        return SimpleNamespace(sequences=sequences)

    code_predictor.generate = fast_generate
    code_predictor._fast_generate = True


class StreamingSynthesizer:
    """Stream Qwen3-TTS audio while codec frames are still being generated.

    The 12 Hz speech-tokenizer decoder is causal, and the library's own
    `chunked_decode` decodes long inputs in chunks with left context. We do the
    same incrementally: a forward hook on the talker collects each finished
    frame, and every few frames the new ones are decoded and handed to
    `on_chunk`, so the first audio is ready after ~0.5 s of speech is generated
    instead of after the whole sentence. With 72 frames of context (the
    decoder's attention window) the result matches offline decoding (>38 dB SNR).
    """

    def __init__(
        self,
        tts: Any,
        first_chunk_frames: int = 6,
        chunk_frames: int = 12,
        left_context_frames: int = 72,  # = decoder attention sliding window
    ):
        self.tts = tts
        self.decoder = tts.model.speech_tokenizer.model.decoder
        self.samples_per_frame = int(tts.model.speech_tokenizer.get_decode_upsample_rate())
        self.sample_rate = int(tts.model.speech_tokenizer.get_output_sample_rate())
        self.first_chunk_frames = first_chunk_frames
        self.chunk_frames = chunk_frames
        self.left_context_frames = left_context_frames
        self.eos_id = int(tts.model.config.talker_config.codec_eos_token_id)

    @torch.inference_mode()
    def synthesize(
        self,
        text: str,
        language: str,
        speaker: str,
        on_chunk: Any,
        **generate_kwargs: Any,
    ) -> int:
        """Generate speech, calling on_chunk(float32 numpy array) as audio is ready.

        Returns the number of codec frames generated. Not thread-safe: the
        caller must serialize calls on one model.
        """
        wrapper = self.tts
        wrapper._validate_languages([language])
        wrapper._validate_speakers([speaker])
        input_ids = wrapper._tokenize_texts([wrapper._build_assistant_text(text)])
        gen_kwargs = wrapper._merge_generate_kwargs(**generate_kwargs)

        talker = wrapper.model.talker
        frames: list[torch.Tensor] = []
        emitted = 0
        stopped = False

        def flush(final: bool = False) -> None:
            nonlocal emitted, stopped
            pending = len(frames) - emitted
            needed = self.first_chunk_frames if emitted == 0 else self.chunk_frames
            if stopped or pending <= 0 or (not final and pending < needed):
                return
            # Drop anything from an end-of-speech frame on (as the library does).
            is_eos = (torch.stack(frames[emitted:])[:, 0, 0] == self.eos_id).tolist()
            if True in is_eos:
                del frames[emitted + is_eos.index(True) :]
                stopped = True
                if len(frames) == emitted:
                    return
            context = min(self.left_context_frames, emitted)
            codes = torch.stack(frames[emitted - context :], dim=-1).clamp(min=0)  # [1, 16, T]
            wav = self.decoder(codes)[..., context * self.samples_per_frame :]
            emitted = len(frames)
            on_chunk(wav.reshape(-1).float().cpu().numpy())

        def tap(_module: Any, _args: Any, out: Any) -> None:
            codec_ids = out.hidden_states[-1]  # [1, 16] for the frame fed this step
            if codec_ids is not None and not stopped:
                frames.append(codec_ids)
                flush()

        handle = talker.register_forward_hook(tap)
        try:
            wrapper.model.generate(
                input_ids=input_ids,
                instruct_ids=[None],
                languages=[language],
                speakers=[speaker],
                non_streaming_mode=True,
                **gen_kwargs,
            )
        finally:
            handle.remove()
        flush(final=True)
        return len(frames)
