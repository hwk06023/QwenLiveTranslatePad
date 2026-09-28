from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .qwen_realtime import QwenRealtimeBridge

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qwen-live-translate")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Qwen Live Translate Pad", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> JSONResponse:
    try:
        settings = get_settings()
        return JSONResponse(
            {
                "ok": True,
                "model": settings.model,
                "region": settings.region,
                "configured": True,
            }
        )
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "configured": False, "error": str(exc)}, status_code=503)


@app.websocket("/ws/translate")
async def translate_socket(
    websocket: WebSocket,
    target: str = Query(default="en", min_length=2, max_length=8),
) -> None:
    await websocket.accept()
    try:
        settings = get_settings()
    except RuntimeError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=1011)
        return

    bridge = QwenRealtimeBridge(websocket, settings, target)
    await bridge.run()
