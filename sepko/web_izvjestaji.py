"""Osnovni izvještaji — izlaz / ulaz / trošak / PDV pregled."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.models import Expense, IncomingInvoice, Invoice, InvoiceStatus
from sepko.ulazne import d, month_expense_summary
from sepko.web_auth import AuthRequired
from sepko.web_security import redirect
from sepko.web_templates import render

router = APIRouter(tags=["izvjestaji"])


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


def _period(year: int | None, month: int | None) -> tuple[int, int, date, date]:
    today = date.today()
    y = year or today.year
    m = month or today.month
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return y, m, start, end


@router.get("/izvjestaji", response_class=HTMLResponse)
def izvjestaji_hub(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return render(
        request,
        "reports.html",
        {"user": user, "tenant": tenant},
    )


@router.get("/izvjestaji/pregled", response_class=HTMLResponse)
def izvjestaji_pregled(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    y, m, start, end = _period(year, month)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

    out_q = db.query(Invoice).filter(
        Invoice.tenant_id == tenant.id,
        Invoice.status == InvoiceStatus.fiscalized.value,
        Invoice.fiscalized_at >= start_dt,
        Invoice.fiscalized_at < end_dt,
    )
    out_count = out_q.count()
    out_gross = d(
        db.query(func.coalesce(func.sum(Invoice.total_gross), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.fiscalized_at >= start_dt,
            Invoice.fiscalized_at < end_dt,
        )
        .scalar()
    )
    out_vat = d(
        db.query(func.coalesce(func.sum(Invoice.total_vat), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.fiscalized_at >= start_dt,
            Invoice.fiscalized_at < end_dt,
        )
        .scalar()
    )
    out_net = d(
        db.query(func.coalesce(func.sum(Invoice.total_net), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.fiscalized_at >= start_dt,
            Invoice.fiscalized_at < end_dt,
        )
        .scalar()
    )

    in_gross = d(
        db.query(func.coalesce(func.sum(IncomingInvoice.total_gross), 0))
        .filter(
            IncomingInvoice.tenant_id == tenant.id,
            IncomingInvoice.issue_date >= start,
            IncomingInvoice.issue_date < end,
        )
        .scalar()
    )
    in_vat = d(
        db.query(func.coalesce(func.sum(IncomingInvoice.total_vat), 0))
        .filter(
            IncomingInvoice.tenant_id == tenant.id,
            IncomingInvoice.issue_date >= start,
            IncomingInvoice.issue_date < end,
        )
        .scalar()
    )
    in_count = (
        db.query(func.count(IncomingInvoice.id))
        .filter(
            IncomingInvoice.tenant_id == tenant.id,
            IncomingInvoice.issue_date >= start,
            IncomingInvoice.issue_date < end,
        )
        .scalar()
        or 0
    )

    exp_total = d(
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(
            Expense.tenant_id == tenant.id,
            Expense.expense_date >= start,
            Expense.expense_date < end,
        )
        .scalar()
    )
    exp_summary = month_expense_summary(db, tenant, year=y, month=m)

    return render(
        request,
        "reports_pregled.html",
        {
            "user": user,
            "tenant": tenant,
            "year": y,
            "month": m,
            "out_count": out_count,
            "out_gross": out_gross,
            "out_vat": out_vat,
            "out_net": out_net,
            "in_count": in_count,
            "in_gross": in_gross,
            "in_vat": in_vat,
            "exp_total": exp_total,
            "exp_summary": exp_summary,
            "pdv_saldo": (out_vat - in_vat).quantize(Decimal("0.01")),
        },
    )
