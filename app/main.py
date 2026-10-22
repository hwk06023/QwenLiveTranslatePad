from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import system_stats
from .admin import (
    COOKIE_NAME,
    SESSION_TTL_S,
    get_runtime,
    is_admin,
    issue_token,
    password_matches,
)
from .config import BASE_DIR, ENGINES, get_settings
from .local_engine import LOCAL_LANGUAGES, LocalPipelineBridge, get_local_models
from .metrics import STORE, ClientChannel, TurnRecorder
from .qwen_realtime import SUPPORTED_TARGETS, QwenRealtimeBridge

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qwen-live-translate")

PUBLIC_DIR = BASE_DIR / "public"

ENGINE_LABELS = {"api": "Cloud API", "local": "On-prem GPU"}


@lru_cache(maxsize=1)
def _local_runtime_installed() -> bool:
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("torch", "transformers", "qwen_asr", "qwen_tts")
    )


def engine_status() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    local = get_local_models(settings)
    local_available = (
        settings.local_enabled and _local_runtime_installed() and local.state != "error"
    )
    return {
        "api": {
            "label": ENGINE_LABELS["api"],
            "available": settings.api_configured,
            "languages": sorted(SUPPORTED_TARGETS),
        },
        "local": {
            "label": ENGINE_LABELS["local"],
            "available": local_available,
            "state": local.state,
            "languages": list(LOCAL_LANGUAGES),
        },
    }


def resolve_engine(requested: str | None, admin: bool) -> tuple[str | None, str | None]:
    """Pick the engine for a turn. Returns (engine, notice-for-the-user)."""
    settings = get_settings()
    runtime = get_runtime(settings)
    status = engine_status()

    engine = runtime.default_engine
    if requested in ENGINES and (admin or runtime.public_engine_choice):
        engine = requested

    if status[engine]["available"]:
        return engine, None
    fallback = next((e for e in ENGINES if status[e]["available"]), None)
    if fallback is None:
        return None, None
    return fallback, (
        f"{ENGINE_LABELS[engine]} 엔진을 사용할 수 없어 "
        f"{ENGINE_LABELS[fallback]}로 전환했습니다."
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if settings.local_enabled and settings.local_preload and _local_runtime_installed():
        models = get_local_models(settings)
        threading.Thread(target=_preload, args=(models,), daemon=True).start()
    yield


def _preload(models: Any) -> None:
    try:
        ms = models.ensure_loaded()
        log.info("Local models preloaded in %.1f s", ms / 1000)
    except Exception:
        log.exception("Local model preload failed")


app = FastAPI(title="Qwen Live Translate Pad", version="0.2.0", lifespan=lifespan)
api = APIRouter(prefix="/api")


# ---------------------------------------------------------------- public API


@api.get("/health")
async def health() -> JSONResponse:
    status = engine_status()
    ok = any(e["available"] for e in status.values())
    return JSONResponse(
        {
            "ok": ok,
            "default_engine": get_runtime(get_settings()).default_engine,
            "engines": {k: {"available": v["available"]} for k, v in status.items()},
        },
        status_code=200 if ok else 503,
    )


@api.get("/config")
async def public_config(request: Request) -> dict[str, Any]:
    settings = get_settings()
    runtime = get_runtime(settings)
    return {
        "default_engine": runtime.default_engine,
        "public_engine_choice": runtime.public_engine_choice,
        "engines": engine_status(),
        "is_admin": is_admin(settings, request.cookies),
        "admin_enabled": settings.admin_enabled,
    }


@api.websocket("/ws/translate")
async def translate_socket(
    websocket: WebSocket,
    target: str = Query(default="en", min_length=2, max_length=8),
    engine: str | None = Query(default=None, max_length=8),
    direction: str = Query(default="staff", max_length=10),
    source: str = Query(default="auto", max_length=8),
) -> None:
    await websocket.accept()
    settings = get_settings()
    admin = is_admin(settings, websocket.cookies)
    chosen, notice = resolve_engine(engine, admin)

    forwarded = websocket.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() or (websocket.client.host if websocket.client else "")
    recorder = TurnRecorder(
        engine=chosen or "none",
        direction=direction,
        target=target,
        source=source,
        client=client_ip,
    )
    channel = ClientChannel(websocket, recorder)

    if notice:
        await channel.send_json({"type": "notice", "message": notice})
    if chosen is None:
        await channel.send_json(
            {"type": "error", "message": "사용 가능한 번역 엔진이 없습니다. 서버 설정을 확인하세요."}
        )
        await channel.close(code=1011)
        return

    await channel.send_json({"type": "engine", "engine": chosen, "label": ENGINE_LABELS[chosen]})
    try:
        if chosen == "api":
            settings.require_api()
            await QwenRealtimeBridge(channel, settings, target).run()
        else:
            await LocalPipelineBridge(channel, settings, target, source).run()
    except RuntimeError as exc:
        await channel.send_json({"type": "error", "message": str(exc)})
    finally:
        summary = recorder.finish()
        if admin:
            await channel.send_json({"type": "metrics", "metrics": summary})
        await channel.close()


# ----------------------------------------------------------------- admin API


def _require_admin(request: Request) -> JSONResponse | None:
    if not is_admin(get_settings(), request.cookies):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


@api.post("/admin/login")
async def admin_login(request: Request) -> Response:
    settings = get_settings()
    if not settings.admin_enabled:
        return JSONResponse(
            {"error": "ADMIN_PASSWORD가 설정되지 않아 관리자 기능이 꺼져 있습니다."},
            status_code=503,
        )
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not password_matches(settings, str(body.get("password", ""))):
        await asyncio.sleep(0.8)  # slow down guessing
        return JSONResponse({"error": "비밀번호가 올바르지 않습니다."}, status_code=401)

    response = JSONResponse({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE_NAME,
        issue_token(settings),
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return response


@api.post("/admin/logout")
async def admin_logout() -> Response:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@api.get("/admin/overview")
async def admin_overview(request: Request, limit: int = 100) -> Response:
    if denied := _require_admin(request):
        return denied
    settings = get_settings()
    runtime = get_runtime(settings)
    return JSONResponse(
        {
            "runtime": {
                "default_engine": runtime.default_engine,
                "public_engine_choice": runtime.public_engine_choice,
            },
            "engines": engine_status(),
            "api": {
                "configured": settings.api_configured,
                "model": settings.model,
                "region": settings.region,
            },
            "local": get_local_models(settings).status(),
            "aggregate": STORE.aggregate(),
            "turns": STORE.recent(max(1, min(limit, 300))),
        }
    )


@api.get("/admin/system")
async def admin_system(request: Request) -> Response:
    if denied := _require_admin(request):
        return denied
    snapshot = await asyncio.to_thread(system_stats.snapshot)
    local = get_local_models(get_settings())
    return JSONResponse({**snapshot, "local_state": local.state, "local_busy": local.turn_lock.locked()})


@api.post("/admin/settings")
async def admin_settings(request: Request) -> Response:
    if denied := _require_admin(request):
        return denied
    runtime = get_runtime(get_settings())
    runtime.update(await request.json())
    return JSONResponse(
        {
            "default_engine": runtime.default_engine,
            "public_engine_choice": runtime.public_engine_choice,
        }
    )


@api.post("/admin/local/load")
async def admin_local_load(request: Request) -> Response:
    if denied := _require_admin(request):
        return denied
    settings = get_settings()
    if not (settings.local_enabled and _local_runtime_installed()):
        return JSONResponse({"error": "On-prem 런타임이 설치되지 않았습니다."}, status_code=503)
    models = get_local_models(settings)
    if models.state not in {"ready", "loading"}:
        threading.Thread(target=_preload, args=(models,), daemon=True).start()
    return JSONResponse({"state": "loading" if models.state != "ready" else "ready"})


@api.post("/admin/local/unload")
async def admin_local_unload(request: Request) -> Response:
    if denied := _require_admin(request):
        return denied
    models = get_local_models(get_settings())
    if models.turn_lock.locked():
        return JSONResponse({"error": "번역 처리 중에는 언로드할 수 없습니다."}, status_code=409)
    await asyncio.to_thread(models.unload)
    return JSONResponse({"state": models.state})


@api.delete("/admin/metrics")
async def admin_clear_metrics(request: Request) -> Response:
    if denied := _require_admin(request):
        return denied
    STORE.clear()
    return JSONResponse({"ok": True})


app.include_router(api)

# Pre-/api paths kept for existing clients and reverse-proxy configs.
app.add_api_route("/health", health, methods=["GET"])
app.add_api_websocket_route("/ws/translate", translate_socket)


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "admin.html")


# On Vercel the CDN serves public/; locally FastAPI does.
if PUBLIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
