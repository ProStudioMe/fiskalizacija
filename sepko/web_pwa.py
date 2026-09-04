"""PWA shell — mobilni pregled, brza fiskalizacija, SW na /sw.js."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.models import Expense, IncomingInvoice, Invoice, InvoiceStatus
from sepko.schemas import FiscalizeRequest, InvoiceLineIn, TotalsIn
from sepko.services import fiscalize_invoice
from sepko.web_auth import AuthRequired
from sepko.web_security import flash, redirect, validate_csrf
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
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return render(
        request,
        "pwa_quick.html",
        {"user": user, "tenant": tenant},
    )


@router.post("/app/brzo")
def pwa_quick_fiscalize(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(...),
    amount_gross: str = Form(...),
    vat_rate: str = Form("21"),
    payment: str = Form("BANKNOTE"),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/app/brzo")
    try:
        from sepko.money import parse_nonneg_money

        gross = parse_nonneg_money(amount_gross, quantize="0.01")
        if gross is None or gross <= 0:
            raise ValueError("Iznos mora biti > 0")
        rate = Decimal(str(vat_rate or "21").replace(",", "."))
        net = (gross / (1 + rate / Decimal("100"))).quantize(Decimal("0.01"))
        vat = (gross - net).quantize(Decimal("0.01"))
        pay = (payment or "BANKNOTE").upper()
        inv_type = "CASH" if pay in ("BANKNOTE", "CARD") else "NONCASH"
        if inv_type == "NONCASH" and pay in ("BANKNOTE", "CARD"):
            pay = "ORDER"
        body = FiscalizeRequest(
            issue_datetime=datetime.now(timezone.utc),
            invoice_type=inv_type,
            payment_method=pay,
            lines=[
                InvoiceLineIn(
                    code="BRZO",
                    name=(name or "Usluga")[:255],
                    quantity=Decimal("1"),
                    unit_price_net=net,
                    vat_rate=rate,
                    total_gross=gross,
                )
            ],
            totals=TotalsIn(net=net, vat=vat, gross=gross),
        )
        result = fiscalize_invoice(db, tenant, body)
        if result.status == "fiscalized":
            flash(request, f"Fiskalizovano: {result.inv_num or result.external_id}")
            inv = (
                db.query(Invoice)
                .filter(
                    Invoice.tenant_id == tenant.id,
                    Invoice.external_id == result.external_id,
                )
                .first()
            )
            if inv:
                return redirect(f"/racuni/{inv.id}")
            return redirect("/app")
        flash(request, result.error_message or "Fiskalizacija nije uspjela", "error")
        return redirect("/app/brzo")
    except Exception as exc:
        flash(request, str(exc), "error")
        return redirect("/app/brzo")
