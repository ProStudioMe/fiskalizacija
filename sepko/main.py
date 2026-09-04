from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from sepko import __version__
from sepko.config import get_settings
from sepko.db import dispose_async_engine, init_db
from sepko.routers import bookkeeping, health, invoices
from sepko.routers import settings as settings_router
from sepko.security_middleware import (
    AuditBindMiddleware,
    CsrfOriginMiddleware,
    ProbeBlockMiddleware,
    SecurityHeadersMiddleware,
)
from sepko.web import router as web_router
from sepko.web_admin import router as admin_router
from sepko.web_finansije import router as finansije_router
from sepko.web_izvjestaji import router as izvjestaji_router
from sepko.web_pwa import router as pwa_router
from sepko.web_troskovi import router as troskovi_router
from sepko.web_ulazne import router as ulazne_router
from sepko.web_auth import AdminAuthRequired, AuthRequired, TenantSuspended, logout_user
from sepko.web_security import flash


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield
    await dispose_async_engine()


app_settings = get_settings()

app = FastAPI(
    title="Sepko",
    description="SaaS fiskalizacija CG — back-office + API (Navira partner).",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if app_settings.env == "production" else "/docs",
    redoc_url=None if app_settings.env == "production" else "/redoc",
    openapi_url=None if app_settings.env == "production" else "/openapi.json",
)

if app_settings.env == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=app_settings.allowed_host_list)
    app.add_middleware(CsrfOriginMiddleware, enabled=True)
app.add_middleware(SecurityHeadersMiddleware, env=app_settings.env)
# Inner od Session: audit vidi already-decrypted cookie
app.add_middleware(AuditBindMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=app_settings.secret_key,
    session_cookie="sepko_session",
    same_site="lax",
    https_only=app_settings.env == "production",
    max_age=60 * 60 * 12,
)
# Posljednji = prvi na ulazu: skeneri odmah 404, bez sesije/DB
app.add_middleware(
    ProbeBlockMiddleware,
    enabled=app_settings.block_probes,
    rate_limit=app_settings.global_rate_limit,
)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(health.router)
app.include_router(invoices.router)
app.include_router(bookkeeping.router)
app.include_router(settings_router.router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(finansije_router)
app.include_router(ulazne_router)
app.include_router(troskovi_router)
app.include_router(pwa_router)
app.include_router(izvjestaji_router)


@app.get("/cron/raspored")
async def cron_schedules(token: str = ""):
    """Pozovi jednom dnevno (Task Scheduler / cron): /cron/raspored?token=SEPKO_CRON_SECRET"""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from sepko.db import AsyncSessionLocal
    from sepko.schedules import run_due_schedules

    settings = get_settings()
    secret = (settings.cron_secret or "").strip()
    if not secret or token != secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    async with AsyncSessionLocal() as session:
        result = await session.run_sync(run_due_schedules)
        return JSONResponse(result)


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, _exc: AuthRequired):
    if request.url.path.startswith("/admin"):
        return RedirectResponse("/admin/login", status_code=303)
    if request.session.get("role") == "superadmin":
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(AdminAuthRequired)
async def admin_auth_required_handler(_request: Request, _exc: AdminAuthRequired):
    return RedirectResponse("/admin/login", status_code=303)


@app.exception_handler(TenantSuspended)
async def tenant_suspended_handler(request: Request, _exc: TenantSuspended):
    logout_user(request)
    flash(request, "Nalog firme je suspendovan. Kontaktirajte podršku.", "error")
    return RedirectResponse("/login", status_code=303)
