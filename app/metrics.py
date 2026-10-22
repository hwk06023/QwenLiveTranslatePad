"""Per-turn latency recording.

Every push-to-talk turn gets a TurnRecorder. ClientChannel sits between the
browser WebSocket and an engine bridge and marks timestamps as messages pass,
so engines only need to report their internal stage timings.

Transcripts are never stored; only their lengths.
"""

from __future__ import annotations

import base64
import statistics
import time
import uuid
from collections import deque
from contextlib import suppress
from typing import Any

from fastapi import WebSocket

HISTORY_SIZE = 300

# Marks are milliseconds since the WebSocket was accepted.
MARKS = (
    "ready",
    "audio_in_first",
    "release",
    "source_first",
    "source_done",
    "translation_first",
    "translation_done",
    "audio_out_first",
    "done",
)

# Headline latencies, relative to the moment the speaker released the button.
SUMMARY_KEYS = (
    "connect_ms",
    "release_to_source_ms",
    "release_to_translation_ms",
    "release_to_audio_ms",
    "release_to_done_ms",
)


class TurnRecorder:
    def __init__(self, engine: str, direction: str, target: str, source: str, client: str):
        self.id = uuid.uuid4().hex[:10]
        self.engine = engine
        self.direction = direction
        self.target = target
        self.source = source
        self.client = client
        self.started_at = time.time()
        self._t0 = time.perf_counter()
        self.marks: dict[str, float] = {}
        self.stages: dict[str, float] = {}
        self.info: dict[str, Any] = {}
        self.audio_in_bytes = 0
        self.audio_out_samples = 0
        self.audio_out_rate = 24_000
        self.source_chars = 0
        self.translation_chars = 0
        self.error: str | None = None
        self.summary: dict[str, Any] | None = None

    def now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, round(self.now_ms(), 1))

    def stage(self, name: str, ms: float) -> None:
        self.stages[name] = round(self.stages.get(name, 0.0) + ms, 1)

    def finish(self) -> dict[str, Any]:
        if self.summary is not None:
            return self.summary
        self.mark("done")
        m = self.marks
        release = m.get("release")

        def since_release(key: str) -> float | None:
            if release is None or key not in m:
                return None
            return round(m[key] - release, 1)

        audio_in_s = self.audio_in_bytes / 2 / 16_000
        audio_out_s = self.audio_out_samples / self.audio_out_rate if self.audio_out_rate else 0
        self.summary = {
            "id": self.id,
            "started_at": self.started_at,
            "engine": self.engine,
            "direction": self.direction,
            "source": self.source,
            "target": self.target,
            "client": self.client,
            "marks": dict(m),
            "stages": dict(self.stages),
            "info": dict(self.info),
            "audio_in_s": round(audio_in_s, 2),
            "audio_out_s": round(audio_out_s, 2),
            "source_chars": self.source_chars,
            "translation_chars": self.translation_chars,
            "error": self.error,
            "connect_ms": m.get("ready"),
            "release_to_source_ms": since_release("source_done")
            if "source_done" in m
            else since_release("source_first"),
            "release_to_translation_ms": since_release("translation_first"),
            "release_to_audio_ms": since_release("audio_out_first"),
            "release_to_done_ms": since_release("done"),
            "total_ms": m.get("done"),
        }
        STORE.add(self.summary)
        return self.summary


class ClientChannel:
    """Wraps the browser socket; records timing as messages flow through it."""

    def __init__(self, websocket: WebSocket, recorder: TurnRecorder):
        self.ws = websocket
        self.rec = recorder

    async def receive(self) -> dict[str, Any]:
        message = await self.ws.receive()
        pcm = message.get("bytes")
        if pcm:
            self.rec.mark("audio_in_first")
            self.rec.audio_in_bytes += len(pcm)
        elif message.get("text") and '"finish"' in message["text"]:
            self.rec.mark("release")
        return message

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._observe(payload)
        with suppress(Exception):
            await self.ws.send_json(payload)

    async def close(self, code: int = 1000) -> None:
        with suppress(Exception):
            await self.ws.close(code=code)

    def _observe(self, payload: dict[str, Any]) -> None:
        rec = self.rec
        kind = payload.get("type")
        if kind == "ready":
            rec.mark("ready")
        elif kind == "source_delta":
            rec.mark("source_first")
            rec.source_chars += len(payload.get("text", ""))
        elif kind == "source_done":
            rec.mark("source_first")
            rec.mark("source_done")
            text = payload.get("text", "")
            rec.source_chars = max(rec.source_chars, len(text))
            if payload.get("language"):
                rec.info["detected_language"] = payload["language"]
        elif kind == "translation_delta":
            rec.mark("translation_first")
            rec.translation_chars += len(payload.get("text", ""))
        elif kind == "translation_done":
            rec.mark("translation_first")
            rec.mark("translation_done")
            rec.translation_chars = max(rec.translation_chars, len(payload.get("text", "")))
        elif kind == "audio":
            rec.mark("audio_out_first")
            rec.audio_out_rate = payload.get("sample_rate", rec.audio_out_rate)
            audio = payload.get("audio", "")
            rec.audio_out_samples += len(base64.b64decode(audio)) // 2 if audio else 0
        elif kind == "error":
            rec.error = str(payload.get("message", "error"))[:300]


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


class MetricsStore:
    def __init__(self, size: int = HISTORY_SIZE):
        self._turns: deque[dict[str, Any]] = deque(maxlen=size)

    def add(self, summary: dict[str, Any]) -> None:
        self._turns.append(summary)

    def clear(self) -> None:
        self._turns.clear()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._turns)[-limit:][::-1]

    def aggregate(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        engines = sorted({t["engine"] for t in self._turns})
        for engine in engines:
            turns = [t for t in self._turns if t["engine"] == engine]
            ok = [t for t in turns if not t["error"]]
            entry: dict[str, Any] = {"turns": len(turns), "errors": len(turns) - len(ok)}
            for key in SUMMARY_KEYS:
                values = [t[key] for t in ok if isinstance(t.get(key), (int, float))]
                entry[key] = {
                    "p50": _percentile(values, 0.5),
                    "p90": _percentile(values, 0.9),
                    "mean": round(statistics.fmean(values), 1) if values else None,
                }
            stage_names = sorted({s for t in ok for s in t["stages"]})
            entry["stages"] = {
                name: _percentile([t["stages"][name] for t in ok if name in t["stages"]], 0.5)
                for name in stage_names
            }
            result[engine] = entry
        return result


STORE = MetricsStore()
