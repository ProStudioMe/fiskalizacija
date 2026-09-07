"""PWA shell — mobilni pregled, SW na /sw.js. Brza fiskalizacija → /kasa."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.models import Expense, IncomingInvoice, Invoice, InvoiceStatus
from sepko.web_auth import AuthRequired
from sepko.web_security import redirect
from sepko.brand import PWA_NAME, PWA_SHORT
from sepko.web_templates import render

router = APIRouter(tags=["pwa"])

_STATIC = Path(__file__).resolve().parent / "static"


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


@router.get("/sw.js")
def service_worker():
    path = _STATIC / "sw.js"
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/manifest.webmanifest")
def pwa_manifest(request: Request):
    origin = str(request.base_url).rstrip("/")
    manifest_url = f"{origin}/manifest.webmanifest"
    return JSONResponse(
        {
            "name": PWA_NAME,
            "short_name": PWA_SHORT,
            "id": "/",
            "description": "Fiskalizacija CG — izlazne, ulazne, troškovnik",
            "start_url": "/app",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0f0a1a",
            "theme_color": "#5b21b6",
            "lang": "sr-ME",
            "launch_handler": {"client_mode": "focus-existing"},
            "prefer_related_applications": False,
            "related_applications": [
                {"platform": "webapp", "url": manifest_url},
            ],
            "icons": [
                {
                    "src": f"{origin}/static/img/sepko-mark.png?v=14",
                    "sizes": "1024x1024",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": f"{origin}/static/img/sepko-mark.svg?v=12",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                },
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/app", response_class=HTMLResponse)
def pwa_home(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    today = date.today()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    inv_today = (
        db.query(func.count(Invoice.id))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.fiscalized_at >= start,
        )
        .scalar()
        or 0
    )
    gross_today = (
        db.query(func.coalesce(func.sum(Invoice.total_gross), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.fiscalized_at >= start,
        )
        .scalar()
        or 0
    )
    month_start = date(today.year, today.month, 1)
    expenses_month = (
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(
            Expense.tenant_id == tenant.id,
            Expense.expense_date >= month_start,
        )
        .scalar()
        or 0
    )
    recent_out = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id)
        .order_by(Invoice.id.desc())
        .limit(8)
        .all()
    )
    recent_in = (
        db.query(IncomingInvoice)
        .filter(IncomingInvoice.tenant_id == tenant.id)
        .order_by(IncomingInvoice.id.desc())
        .limit(5)
        .all()
    )
    return render(
        request,
        "pwa_home.html",
        {
            "user": user,
            "tenant": tenant,
            "inv_today": inv_today,
            "gross_today": float(gross_today),
            "expenses_month": float(expenses_month),
            "recent_out": recent_out,
            "recent_in": recent_in,
        },
    )


@router.get("/app/brzo", response_class=HTMLResponse)
def pwa_quick_form(request: Request, db: Session = Depends(get_db)):
    """Legacy — PWA kasa."""
    try:
        _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return redirect("/app/kasa")


@router.post("/app/brzo")
def pwa_quick_fiscalize(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    amount_gross: str = Form(""),
    vat_rate: str = Form("21"),
    payment: str = Form("BANKNOTE"),
    db: Session = Depends(get_db),
):
    try:
        _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return redirect("/app/kasa")
