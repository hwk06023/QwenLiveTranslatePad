"""End-to-end smoke test: stream a WAV file through /api/ws/translate.

    .venv/bin/python scripts/smoke_test.py speech.wav --target en --engine local

The WAV must be 16 kHz mono PCM16. Prints server events and the turn latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave

import websockets


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav")
    parser.add_argument("--url", default="ws://127.0.0.1:8090/api/ws/translate")
    parser.add_argument("--target", default="en")
    parser.add_argument("--engine", default="local")
    parser.add_argument("--direction", default="staff")
    parser.add_argument("--source", default="ko")
    args = parser.parse_args()

    with wave.open(args.wav) as wav:
        assert wav.getframerate() == 16000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
        pcm = wav.readframes(wav.getnframes())

    url = (
        f"{args.url}?target={args.target}&engine={args.engine}"
        f"&direction={args.direction}&source={args.source}"
    )
    async with websockets.connect(url, max_size=None) as ws:
        start = time.perf_counter()
        ms = lambda: f"{(time.perf_counter() - start) * 1000:7.0f} ms"
        while True:
            event = json.loads(await ws.recv())
            print(ms(), event["type"], {k: v for k, v in event.items() if k != "type"})
            if event["type"] == "ready":
                break
            if event["type"] == "error":
                return
        # Real-time pacing: 20 ms packets.
        for offset in range(0, len(pcm), 640):
            await ws.send(pcm[offset : offset + 640])
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"type": "finish"}))
        released = time.perf_counter()
        audio_bytes = 0
        translation = ""
        async for raw in ws:
            event = json.loads(raw)
            kind = event["type"]
            if kind == "audio":
                if not audio_bytes:
                    print(ms(), f"first audio {1000 * (time.perf_counter() - released):.0f} ms after release")
                audio_bytes += len(event["audio"]) * 3 // 4
                continue
            if kind == "translation_delta":
                translation += event["text"]
                continue
            print(ms(), kind, {k: v for k, v in event.items() if k != "type"})
        print(f"translation: {translation.strip()}")
        print(f"audio out: {audio_bytes / 2 / 24000:.2f} s")


asyncio.run(main())
