from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

ENGINES = ("api", "local")


def _ensure_writable_hf_home() -> None:
    """A host-wide HF_HOME may point at an unmounted disk; fall back to ~/.cache."""
    hf_home = os.getenv("HF_HOME")
    if not hf_home:
        return
    path = Path(hf_home)
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        os.environ["HF_HOME"] = str(Path.home() / ".cache" / "huggingface")


_ensure_writable_hf_home()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _model_path(env: str, default_name: str, model_dir: Path) -> str:
    """Prefer an explicit env value, then models/<name>, then the HF repo id."""
    explicit = os.getenv(env, "").strip()
    if explicit:
        return explicit
    local = model_dir / default_name
    if local.is_dir():
        return str(local)
    return f"Qwen/{default_name}"


@dataclass(frozen=True)
class Settings:
    # Cloud API engine (Alibaba Cloud Model Studio)
    api_key: str
    workspace_id: str
    region: str
    model: str
    ws_url_override: str | None

    # On-prem engine (local NVIDIA GPU)
    local_enabled: bool
    local_preload: bool
    local_device: str
    local_asr_model: str
    local_llm_model: str
    local_tts_model: str
    local_tts_speaker: str
    local_partial_asr: bool
    local_tts_cuda_graph: bool
    local_max_audio_seconds: int

    # App
    default_engine: str
    admin_password: str
    session_secret: str
    host: str
    port: int
    log_level: str

    @property
    def api_configured(self) -> bool:
        return bool(self.api_key and (self.workspace_id or self.ws_url_override))

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_password)

    @property
    def qwen_ws_url(self) -> str:
        if self.ws_url_override:
            return self.ws_url_override
        query = urlencode({"model": self.model})
        return (
            f"wss://{self.workspace_id}.{self.region}.maas.aliyuncs.com"
            f"/api-ws/v1/realtime?{query}"
        )

    def require_api(self) -> None:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not set")
        if not self.workspace_id and not self.ws_url_override:
            raise RuntimeError("DASHSCOPE_WORKSPACE_ID is not set")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    model_dir = Path(os.getenv("LOCAL_MODEL_DIR", str(BASE_DIR / "models"))).expanduser()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    default_engine = os.getenv("DEFAULT_ENGINE", "local").strip().lower()
    if default_engine not in ENGINES:
        default_engine = "local"

    return Settings(
        api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        workspace_id=os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip(),
        region=os.getenv("DASHSCOPE_REGION", "ap-southeast-1").strip(),
        model=os.getenv(
            "QWEN_MODEL", "qwen3.8-livetranslate-flash-realtime"
        ).strip(),
        ws_url_override=os.getenv("QWEN_WS_URL") or None,
        local_enabled=_bool("LOCAL_ENGINE_ENABLED", True),
        local_preload=_bool("LOCAL_PRELOAD", False),
        local_device=os.getenv("LOCAL_DEVICE", "cuda:0").strip(),
        local_asr_model=_model_path("LOCAL_ASR_MODEL", "Qwen3-ASR-1.7B", model_dir),
        local_llm_model=_model_path(
            "LOCAL_LLM_MODEL", "Qwen3-4B-Instruct-2507", model_dir
        ),
        local_tts_model=_model_path(
            "LOCAL_TTS_MODEL", "Qwen3-TTS-12Hz-1.7B-CustomVoice", model_dir
        ),
        # "auto" picks a native-sounding preset voice per target language.
        local_tts_speaker=os.getenv("LOCAL_TTS_SPEAKER", "auto").strip(),
        local_partial_asr=_bool("LOCAL_PARTIAL_ASR", True),
        local_tts_cuda_graph=_bool("LOCAL_TTS_CUDA_GRAPH", True),
        local_max_audio_seconds=int(os.getenv("LOCAL_MAX_AUDIO_SECONDS", "30")),
        default_engine=default_engine,
        admin_password=admin_password,
        # Signing key for the admin cookie. Derived from the password when not set,
        # so changing the password invalidates existing sessions.
        session_secret=os.getenv("SESSION_SECRET", "").strip()
        or f"qltp::{admin_password}",
        host=os.getenv("APP_HOST", "127.0.0.1").strip(),
        port=int(os.getenv("APP_PORT", "8090")),
        log_level=os.getenv("LOG_LEVEL", "info").strip().lower(),
    )
