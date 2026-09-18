"""Security helpers for Казна (finance data).

Env:
  KAZNA_ENV=production|prod  — strict mode (also auto on Railway)
  KAZNA_SECRET               — required in production (≥32 chars)
  KAZNA_HTTPS=true           — Secure cookies (default on in production)
  KAZNA_ALLOW_DEMO=1         — seed demo users + show demo logins
  KAZNA_SESSION_HOURS=12     — cookie lifetime
  KAZNA_HOSTS=a.com,b.com    — optional TrustedHost allowlist
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from collections import defaultdict
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("kazna.security")

ENC_PREFIX = "enc:v1:"


def _truthy(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def is_production() -> bool:
    env = os.environ.get("KAZNA_ENV", "").strip().lower()
    if env in {"prod", "production"}:
        return True
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return True
    return False


def allow_demo() -> bool:
    if _truthy("KAZNA_ALLOW_DEMO"):
        return True
    return not is_production()


def resolve_secret() -> str:
    raw = (os.environ.get("KAZNA_SECRET") or "").strip()
    if is_production():
        if len(raw) < 32:
            raise RuntimeError(
                "В production обязателен KAZNA_SECRET (≥32 символов). "
                "Сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return raw
    if raw:
        return raw
    # Local/dev only — unstable across restarts (sessions drop). Prefer setting KAZNA_SECRET.
    log.warning("KAZNA_SECRET не задан — сессии сбросятся при рестарте")
    return secrets.token_hex(32)


def https_only_cookies() -> bool:
    if os.environ.get("KAZNA_HTTPS") is not None and os.environ.get("KAZNA_HTTPS") != "":
        return _truthy("KAZNA_HTTPS")
    return is_production()


def session_max_age() -> int:
    try:
        hours = float(os.environ.get("KAZNA_SESSION_HOURS", "12"))
    except ValueError:
        hours = 12.0
    return max(3600, int(hours * 3600))


def docs_enabled() -> bool:
    if _truthy("KAZNA_DOCS"):
        return True
    return not is_production()


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal_secret(plaintext: str, app_secret: str) -> str:
    text = (plaintext or "").strip()
    if not text:
        return ""
    if text.startswith(ENC_PREFIX):
        return text
    token = _fernet(app_secret).encrypt(text.encode("utf-8")).decode("ascii")
    return ENC_PREFIX + token


def open_secret(stored: str, app_secret: str) -> str:
    text = (stored or "").strip()
    if not text:
        return ""
    if not text.startswith(ENC_PREFIX):
        return text  # legacy plaintext until re-saved
    token = text[len(ENC_PREFIX) :]
    try:
        return _fernet(app_secret).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Не удалось расшифровать секрет (проверьте KAZNA_SECRET)") from e


class LoginRateLimiter:
    """In-memory limiter: max fails per IP+email in a window."""

    def __init__(self, max_fails: int = 8, window_sec: int = 900):
        self.max_fails = max_fails
        self.window = window_sec
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _key(self, ip: str, email: str) -> str:
        return f"{ip}|{email.lower()}"

    def check(self, ip: str, email: str) -> None:
        now = time.time()
        key = self._key(ip, email)
        with self._lock:
            stamps = [t for t in self._hits[key] if now - t < self.window]
            self._hits[key] = stamps
            if len(stamps) >= self.max_fails:
                raise PermissionError("Слишком много попыток входа. Подождите 15 минут.")

    def fail(self, ip: str, email: str) -> None:
        with self._lock:
            self._hits[self._key(ip, email)].append(time.time())

    def success(self, ip: str, email: str) -> None:
        with self._lock:
            self._hits.pop(self._key(ip, email), None)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # CSP: allow Google Fonts + same-origin scripts (inline needed for current pages)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        if https_only_cookies():
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def audit(event: str, **fields: object) -> None:
    parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.info("audit event=%s %s", event, parts)


UPLOAD_MAX_BYTES = 15 * 1024 * 1024
