from __future__ import annotations

import secrets
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import RedirectResponse


# IP -> list of attempt timestamps
_login_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_WINDOW_SEC = 60


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_rate_limited(request: Request) -> bool:
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in _login_attempts[ip] if now - t < _WINDOW_SEC]
    _login_attempts[ip] = recent
    return len(recent) >= _MAX_ATTEMPTS


def record_login_attempt(request: Request) -> None:
    _login_attempts[client_ip(request)].append(time.time())


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def rotate_csrf(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def clear_login_attempts(request: Request) -> None:
    ip = client_ip(request)
    _login_attempts.pop(ip, None)


def validate_csrf(request: Request, token: str | None) -> bool:
    expected = request.session.get("csrf_token")
    if not expected or not token:
        return False
    return secrets.compare_digest(expected, token)


def flash(request: Request, message: str, flash_type: str = "ok") -> None:
    request.session["flash"] = message
    request.session["flash_type"] = flash_type


def pop_flash(request: Request) -> tuple[str | None, str | None]:
    msg = request.session.pop("flash", None)
    ftype = request.session.pop("flash_type", None)
    return msg, ftype


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
