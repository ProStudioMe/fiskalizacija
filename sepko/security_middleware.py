from __future__ import annotations

import re
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response


# Tipični path-ovi koje skeneri / botovi traže (WordPress, PHP, secrets…)
_PROBE_EXACT = frozenset(
    {
        "/.env",
        "/.env.local",
        "/.env.production",
        "/.git",
        "/.git/config",
        "/.git/HEAD",
        "/wp-login.php",
        "/wp-admin",
        "/wp-admin/admin-ajax.php",
        "/xmlrpc.php",
        "/wordpress",
        "/wp",
        "/phpmyadmin",
        "/pma",
        "/admin.php",
        "/administrator",
        "/config.php",
        "/config.json",
        "/actuator",
        "/actuator/health",
        "/server-status",
        "/server-info",
        "/cgi-bin",
        "/vendor/phpunit",
        "/telescope",
        "/_ignition",
        "/debug",
        "/debug/default",
        "/solr",
        "/manager/html",
        "/boaform/admin",
        "/hudson",
        "/jenkins",
        "/containers/json",
        "/v2/_catalog",
        "/api/v1/namespaces",
    }
)

_PROBE_PREFIXES = (
    "/wp-",
    "/wordpress/",
    "/.git/",
    "/.aws/",
    "/.svn/",
    "/phpmyadmin",
    "/phpMyAdmin",
    "/vendor/",
    "/cgi-bin/",
    "/actuator/",
    "/laravel/",
    "/sites/default/",
)

_PROBE_RE = re.compile(
    r"(?i)("
    r"\.php$"
    r"|\.asp$"
    r"|\.aspx$"
    r"|wp-content"
    r"|wp-includes"
    r"|phpunit"
    r"|eval-stdin"
    r"|shell\.php"
    r"|passwd"
    r"|etc/shadow"
    r")"
)

# Globalni blagi rate-limit (po IP)
_rate_hits: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 60.0
_RATE_MAX = 120  # zahtjeva / min / IP


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_probe_path(path: str) -> bool:
    p = path.rstrip("/") or "/"
    low = path.lower()
    if p in _PROBE_EXACT or low in _PROBE_EXACT:
        return True
    if any(low.startswith(pref.lower()) for pref in _PROBE_PREFIXES):
        return True
    if not low.startswith("/static/") and _PROBE_RE.search(low):
        return True
    return False


def _rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _rate_hits[ip] if now - t < _RATE_WINDOW]
    recent.append(now)
    _rate_hits[ip] = recent
    if len(_rate_hits) > 5000:
        stale = [k for k, v in _rate_hits.items() if not v or now - v[-1] > _RATE_WINDOW]
        for k in stale[:2000]:
            _rate_hits.pop(k, None)
    return len(recent) > _RATE_MAX


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, env: str = "development"):
        super().__init__(app)
        self._env = env

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if self._env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
            "connect-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://api.qrserver.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Content-Security-Policy"] = csp
        return response


class ProbeBlockMiddleware(BaseHTTPMiddleware):
    """Blokira skenere (wp/php/.env) i daje robots.txt Disallow sve."""

    def __init__(self, app, *, enabled: bool = True, rate_limit: bool = True):
        super().__init__(app)
        self._enabled = enabled
        self._rate_limit = rate_limit

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path or "/"

        if path == "/robots.txt":
            return PlainTextResponse(
                "User-agent: *\nDisallow: /\n",
                media_type="text/plain; charset=utf-8",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        if self._enabled and _is_probe_path(path):
            return PlainTextResponse("Not Found", status_code=404)

        if self._rate_limit and path not in ("/health", "/robots.txt"):
            if not path.startswith("/static/") and _rate_limited(_client_ip(request)):
                return PlainTextResponse("Too Many Requests", status_code=429)

        return await call_next(request)


_CSRF_SKIP_PREFIX = ("/v1/", "/static/")
_CSRF_SKIP_EXACT = frozenset({"/health", "/cron/raspored", "/cron/licence"})


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """U produkciji Origin/Referer mora odgovarati Host-u (uz CSRF token na formama)."""

    def __init__(self, app, *, enabled: bool = False):
        super().__init__(app)
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self._enabled or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await call_next(request)
        path = request.url.path or "/"
        if path.startswith(_CSRF_SKIP_PREFIX) or path in _CSRF_SKIP_EXACT:
            return await call_next(request)
        host = (request.headers.get("host") or "").split(":")[0].lower()
        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        from urllib.parse import urlparse

        def _host_ok(url: str) -> bool:
            if not url:
                return False
            parsed = urlparse(url)
            return (parsed.hostname or "").lower() == host

        if origin:
            ok = _host_ok(origin)
        elif referer:
            ok = _host_ok(referer)
        else:
            ok = False
        if not ok:
            return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)


class AuditBindMiddleware(BaseHTTPMiddleware):
    """Veže user_id / tenant_id / IP na zahtjev za audit_log."""

    async def dispatch(self, request: Request, call_next) -> Response:
        from sepko.audit import AuditActor, bind_actor

        user_id = None
        tenant_id = None
        email = ""
        try:
            session = request.session
            raw_uid = session.get("user_id")
            user_id = int(raw_uid) if raw_uid is not None else None
            raw_tid = session.get("tenant_id")
            tenant_id = int(raw_tid) if raw_tid is not None else None
            email = str(session.get("email") or "")
        except (AssertionError, AttributeError, ValueError, TypeError):
            pass
        bind_actor(
            AuditActor(
                user_id=user_id,
                tenant_id=tenant_id,
                email=email,
                ip=_client_ip(request),
            )
        )
        try:
            return await call_next(request)
        finally:
            bind_actor(None)
