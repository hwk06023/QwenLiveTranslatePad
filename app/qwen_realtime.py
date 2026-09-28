from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import suppress
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from .config import Settings

log = logging.getLogger(__name__)

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
SUPPORTED_TARGETS = {
    "ko", "en", "ja", "zh", "yue", "es", "fr", "de", "it", "pt", "ru",
    "ar", "th", "vi", "id", "ms", "tr", "nl", "pl", "hi", "bn", "ur",
    "fa", "he", "sv", "da", "no", "fi", "cs", "el",
}


class QwenRealtimeBridge:
    """Bridges one browser push-to-talk session to one Qwen realtime session."""

    def __init__(self, client: WebSocket, settings: Settings, target_language: str):
        self.client = client
        self.settings = settings
        self.target_language = target_language.lower().strip()
        self._upstream: Any | None = None
        self._finished = False

    async def run(self) -> None:
        if self.target_language not in SUPPORTED_TARGETS:
            await self.client.send_json(
                {
                    "type": "error",
                    "message": f"Unsupported target language: {self.target_language}",
                }
            )
            await self.client.close(code=1008)
            return

        try:
            async with websockets.connect(
                self.settings.qwen_ws_url,
                extra_headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                },
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as upstream:
                self._upstream = upstream
                await self._initialize_session()

                upstream_task = asyncio.create_task(self._upstream_to_browser())
                browser_task = asyncio.create_task(self._browser_to_upstream())

                done, _ = await asyncio.wait(
                    {upstream_task, browser_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # A normal browser-task completion means the user released the
                # push-to-talk button and we sent session.finish. Keep receiving
                # Qwen events so the final transcript/audio is not truncated.
                if browser_task in done:
                    exc = browser_task.exception()
                    if exc:
                        upstream_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await upstream_task
                        raise exc

                    if self._finished and not upstream_task.done():
                        try:
                            await asyncio.wait_for(upstream_task, timeout=15)
                        except asyncio.TimeoutError:
                            upstream_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await upstream_task
                    elif not upstream_task.done():
                        upstream_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await upstream_task

                if upstream_task in done:
                    exc = upstream_task.exception()
                    if not browser_task.done():
                        browser_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await browser_task
                    if exc:
                        raise exc

        except WebSocketDisconnect:
            log.info("Browser disconnected")
        except websockets.ConnectionClosed as exc:
            log.info("Qwen connection closed: %s", exc)
            await self._safe_send_json(
                {"type": "closed", "code": exc.code, "reason": exc.reason}
            )
        except Exception as exc:
            log.exception("Realtime bridge failed")
            await self._safe_send_json({"type": "error", "message": str(exc)})
        finally:
            self._upstream = None

    async def _initialize_session(self) -> None:
        assert self._upstream is not None

        while True:
            raw = await asyncio.wait_for(self._upstream.recv(), timeout=15)
            event = json.loads(raw)
            if event.get("type") == "session.created":
                await self._safe_send_json(
                    {
                        "type": "session_created",
                        "model": event.get("session", {}).get("model"),
                    }
                )
                break
            if event.get("type") == "error":
                raise RuntimeError(self._format_qwen_error(event))

        await self._upstream.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "output_modalities": ["text", "audio"],
                        "audio": {
                            "input": {
                                "turn_detection": {
                                    "type": "speaker_detection",
                                    "threshold": 0.5,
                                    "silence_duration_ms": 800,
                                }
                            }
                        },
                        "translation": {"language": self.target_language},
                    },
                },
                ensure_ascii=False,
            )
        )

        while True:
            raw = await asyncio.wait_for(self._upstream.recv(), timeout=15)
            event = json.loads(raw)
            event_type = event.get("type")

            if event_type == "session.updated":
                session = event.get("session", {})
                await self._safe_send_json(
                    {
                        "type": "ready",
                        "target": self.target_language,
                        "input_sample_rate": session.get("audio", {})
                        .get("input", {})
                        .get("format", {})
                        .get("sample_rate", INPUT_RATE),
                        "output_sample_rate": session.get("audio", {})
                        .get("output", {})
                        .get("format", {})
                        .get("sample_rate", OUTPUT_RATE),
                        "voice": session.get("audio", {})
                        .get("output", {})
                        .get("voice"),
                    }
                )
                return

            if event_type == "error":
                raise RuntimeError(self._format_qwen_error(event))

    async def _browser_to_upstream(self) -> None:
        assert self._upstream is not None

        while True:
            message = await self.client.receive()
            message_type = message.get("type")

            if message_type == "websocket.disconnect":
                raise WebSocketDisconnect(code=message.get("code", 1000))

            pcm = message.get("bytes")
            if pcm:
                await self._upstream.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm).decode("ascii"),
                        }
                    )
                )
                continue

            text = message.get("text")
            if not text:
                continue

            try:
                command = json.loads(text)
            except json.JSONDecodeError:
                continue

            if command.get("type") == "finish":
                if not self._finished:
                    self._finished = True
                    await self._upstream.send(json.dumps({"type": "session.finish"}))
                return

    async def _upstream_to_browser(self) -> None:
        assert self._upstream is not None

        async for raw in self._upstream:
            event = json.loads(raw)
            event_type = event.get("type", "")

            if event_type == "conversation.item.input_audio_transcription.delta":
                await self._safe_send_json(
                    {"type": "source_delta", "text": event.get("delta", "")}
                )

            elif event_type == "conversation.item.input_audio_transcription.completed":
                text = (
                    event.get("transcript")
                    or event.get("text")
                    or event.get("item", {}).get("content", [{}])[0].get("transcript")
                    or ""
                )
                await self._safe_send_json({"type": "source_done", "text": text})

            elif event_type == "response.audio_transcript.delta":
                await self._safe_send_json(
                    {"type": "translation_delta", "text": event.get("delta", "")}
                )

            elif event_type == "response.audio_transcript.done":
                await self._safe_send_json(
                    {
                        "type": "translation_done",
                        "text": event.get("transcript", ""),
                    }
                )

            elif event_type == "response.text.delta":
                await self._safe_send_json(
                    {"type": "translation_delta", "text": event.get("delta", "")}
                )

            elif event_type == "response.text.done":
                await self._safe_send_json(
                    {"type": "translation_done", "text": event.get("text", "")}
                )

            elif event_type == "response.audio.delta":
                await self._safe_send_json(
                    {
                        "type": "audio",
                        "audio": event.get("delta", ""),
                        "sample_rate": OUTPUT_RATE,
                    }
                )

            elif event_type in {
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
            }:
                await self._safe_send_json(
                    {"type": "speech", "state": event_type.rsplit("_", 1)[-1]}
                )

            elif event_type == "response.done":
                await self._safe_send_json({"type": "response_done"})

            elif event_type == "error":
                await self._safe_send_json(
                    {"type": "error", "message": self._format_qwen_error(event)}
                )

            elif event_type == "session.finished":
                await self._safe_send_json({"type": "session_finished"})
                with suppress(Exception):
                    await self.client.close(code=1000)
                return

    @staticmethod
    def _format_qwen_error(event: dict[str, Any]) -> str:
        error = event.get("error", event)
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or json.dumps(error, ensure_ascii=False)
            return f"{code}: {message}" if code else str(message)
        return str(error)

    async def _safe_send_json(self, payload: dict[str, Any]) -> None:
        with suppress(Exception):
            await self.client.send_json(payload)
