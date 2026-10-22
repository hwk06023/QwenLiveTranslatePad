"""Admin authentication and runtime-adjustable settings."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Mapping

from .config import BASE_DIR, ENGINES, Settings

log = logging.getLogger(__name__)

COOKIE_NAME = "qltp_admin"
SESSION_TTL_S = 12 * 60 * 60
RUNTIME_FILE = BASE_DIR / "runtime_settings.json"


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(settings: Settings) -> str:
    expires = str(int(time.time()) + SESSION_TTL_S)
    return f"{expires}.{_sign(settings.session_secret, expires)}"


def verify_token(settings: Settings, token: str | None) -> bool:
    if not settings.admin_enabled or not token or "." not in token:
        return False
    expires, signature = token.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(settings.session_secret, expires))


def password_matches(settings: Settings, candidate: str) -> bool:
    if not settings.admin_enabled:
        return False
    return hmac.compare_digest(candidate.encode(), settings.admin_password.encode())


def is_admin(settings: Settings, cookies: Mapping[str, str]) -> bool:
    return verify_token(settings, cookies.get(COOKIE_NAME))


@dataclass
class RuntimeSettings:
    """Settings an admin can change from the dashboard without a restart."""

    default_engine: str
    public_engine_choice: bool = True

    def update(self, data: Mapping[str, object]) -> None:
        engine = data.get("default_engine")
        if isinstance(engine, str) and engine in ENGINES:
            self.default_engine = engine
        choice = data.get("public_engine_choice")
        if isinstance(choice, bool):
            self.public_engine_choice = choice
        self._save()

    def _save(self) -> None:
        try:
            RUNTIME_FILE.write_text(json.dumps(asdict(self), indent=2))
        except OSError:
            # Read-only filesystems (Vercel) keep the change in memory only.
            log.info("Runtime settings not persisted (read-only filesystem)")


_RUNTIME: RuntimeSettings | None = None


def get_runtime(settings: Settings) -> RuntimeSettings:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = RuntimeSettings(default_engine=settings.default_engine)
        try:
            saved = json.loads(RUNTIME_FILE.read_text())
            if saved.get("default_engine") in ENGINES:
                _RUNTIME.default_engine = saved["default_engine"]
            _RUNTIME.public_engine_choice = bool(saved.get("public_engine_choice", True))
        except (OSError, ValueError):
            pass
    return _RUNTIME
