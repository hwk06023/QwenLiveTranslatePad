from __future__ import annotations

from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.qwen_realtime import QwenRealtimeBridge

app = FastAPI(title="Qwen Live Translate Pad API", version="0.1.0")


@app.get("/api")
async def api_root() -> dict[str, object]:
    return {"ok": True, "service": "qwen-live-translate-pad"}


@app.get("/api/health")
async def health() -> JSONResponse:
    try:
        settings = get_settings()
        return JSONResponse(
            {
                "ok": True,
                "configured": True,
                "model": settings.model,
                "region": settings.region,
            }
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"ok": False, "configured": False, "error": str(exc)},
            status_code=503,
        )


@app.websocket("/api/ws/translate")
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
