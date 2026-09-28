from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_key: str
    workspace_id: str
    region: str
    model: str
    ws_url_override: str | None
    host: str
    port: int
    log_level: str

    @property
    def qwen_ws_url(self) -> str:
        if self.ws_url_override:
            return self.ws_url_override
        query = urlencode({"model": self.model})
        return (
            f"wss://{self.workspace_id}.{self.region}.maas.aliyuncs.com"
            f"/api-ws/v1/realtime?{query}"
        )


def get_settings() -> Settings:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    workspace_id = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()

    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    if not workspace_id:
        raise RuntimeError("DASHSCOPE_WORKSPACE_ID is not set")

    return Settings(
        api_key=api_key,
        workspace_id=workspace_id,
        region=os.getenv("DASHSCOPE_REGION", "ap-southeast-1").strip(),
        model=os.getenv(
            "QWEN_MODEL", "qwen3.8-livetranslate-flash-realtime"
        ).strip(),
        ws_url_override=os.getenv("QWEN_WS_URL") or None,
        host=os.getenv("APP_HOST", "127.0.0.1").strip(),
        port=int(os.getenv("APP_PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info").strip().lower(),
    )
