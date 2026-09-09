"""REST — ulazne fakture i troškovi (API key auth)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sepko.auth import get_tenant
from sepko.db import get_db
from sepko.models import Expense, IncomingInvoice, Tenant
from sepko.ulazne import create_from_qr, create_incoming_invoice, d, ensure_expense_categories, parse_date

router = APIRouter(tags=["bookkeeping"])


class IncomingLineIn(BaseModel):
    code: str = ""
    name: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Decimal("1")
    unit_price_net: Decimal = Decimal("0")
    vat_rate: Decimal = Decimal("21")
    total_gross: Decimal = Decimal("0")


class IncomingCreateIn(BaseModel):
    number: str = ""
    issue_date: str | None = None
    customer_id: int | None = None
    supplier_pib: str | None = None
    supplier_name: str | None = None
    notes: str | None = None
    status: str = "recorded"
    total_net: Decimal | None = None
    total_vat: Decimal | None = None
    total_gross: Decimal | None = None
    lines: list[IncomingLineIn] = Field(default_factory=list)


class IncomingFromQrIn(BaseModel):
    qr_raw: str = Field(min_length=8, max_length=2000)
    fetch: bool = True


class IncomingOut(BaseModel):
    id: int
    number: str
    status: str
    source: str
    customer_id: int | None = None
    supplier_pib: str | None
    supplier_name: str | None
    issue_date: date | None
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    ikof: str | None = None
    qr_url: str | None = None


class ExpenseCreateIn(BaseModel):
    amount: Decimal = Field(gt=0)
    expense_date: str | None = None
    category_id: int | None = None
    description: str | None = None
    notes: str | None = None
    incoming_invoice_id: int | None = None


class ExpenseOut(BaseModel):
    id: int
    amount: Decimal
    expense_date: date | None
    description: str | None
    category_id: int | None
    incoming_invoice_id: int | None


class DashboardSummaryOut(BaseModel):
    invoices_today: int
    gross_today: Decimal
    incoming_open: int
    expenses_month: Decimal


def _incoming_out(inv: IncomingInvoice) -> IncomingOut:
    return IncomingOut(
        id=inv.id,
        number=inv.number or "",
        status=inv.status,
        source=inv.source,
        customer_id=inv.customer_id,
        supplier_pib=inv.supplier_pib,
        supplier_name=inv.supplier_name,
        issue_date=inv.issue_date,
        total_net=inv.total_net or Decimal("0"),
        total_vat=inv.total_vat or Decimal("0"),
        total_gross=inv.total_gross or Decimal("0"),
        ikof=inv.ikof,
        qr_url=inv.qr_url,
    )


@router.get("/v1/incoming-invoices", response_model=list[IncomingOut])
def list_incoming(
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    rows = (
        db.query(IncomingInvoice)
        .filter(IncomingInvoice.tenant_id == tenant.id)
        .order_by(IncomingInvoice.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [_incoming_out(r) for r in rows]


@router.post("/v1/incoming-invoices", response_model=IncomingOut, status_code=201)
def create_incoming(
    body: IncomingCreateIn,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    try:
        lines = [ln.model_dump() for ln in body.lines] if body.lines else None
        inv = create_incoming_invoice(
            db,
            tenant,
            number=body.number,
            issue_date=parse_date(body.issue_date),
            customer_id=body.customer_id,
            supplier_pib=body.supplier_pib,
            supplier_name=body.supplier_name,
            status=body.status,
            notes=body.notes,
            lines=lines,
            total_net=body.total_net,
            total_vat=body.total_vat,
            total_gross=body.total_gross,
        )
        db.commit()
        db.refresh(inv)
        return _incoming_out(inv)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/v1/incoming-invoices/from-qr", response_model=IncomingOut, status_code=201)
def incoming_from_qr(
    body: IncomingFromQrIn,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    try:
        inv, _draft = create_from_qr(db, tenant, body.qr_raw, fetch=body.fetch)
        db.commit()
        db.refresh(inv)
        return _incoming_out(inv)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/v1/expenses", response_model=list[ExpenseOut])
def list_expenses(
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    rows = (
        db.query(Expense)
        .filter(Expense.tenant_id == tenant.id)
        .order_by(Expense.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [
        ExpenseOut(
            id=e.id,
            amount=e.amount,
            expense_date=e.expense_date,
            description=e.description,
            category_id=e.category_id,
            incoming_invoice_id=e.incoming_invoice_id,
        )
        for e in rows
    ]


@router.post("/v1/expenses", response_model=ExpenseOut, status_code=201)
def create_expense(
    body: ExpenseCreateIn,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    from sepko.models import Expense as ExpenseModel
    from sepko.audit import write_audit

    ensure_expense_categories(db, tenant)
    exp = ExpenseModel(
        tenant_id=tenant.id,
        category_id=body.category_id,
        incoming_invoice_id=body.incoming_invoice_id,
        amount=d(body.amount),
        expense_date=parse_date(body.expense_date) or date.today(),
        description=(body.description or None),
        notes=body.notes,
    )
    db.add(exp)
    write_audit(db, "expense.create", tenant_id=tenant.id, entity_type="expense", detail=str(body.amount))
    db.commit()
    db.refresh(exp)
    return ExpenseOut(
        id=exp.id,
        amount=exp.amount,
        expense_date=exp.expense_date,
        description=exp.description,
        category_id=exp.category_id,
        incoming_invoice_id=exp.incoming_invoice_id,
    )


@router.get("/v1/dashboard/summary", response_model=DashboardSummaryOut)
def dashboard_summary(
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    from datetime import datetime, timezone
    from sqlalchemy import func
    from sepko.models import Invoice, InvoiceStatus

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
    gross = (
        db.query(func.coalesce(func.sum(Invoice.total_gross), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.fiscalized_at >= start,
        )
        .scalar()
        or 0
    )
    open_in = (
        db.query(func.count(IncomingInvoice.id))
        .filter(
            IncomingInvoice.tenant_id == tenant.id,
            IncomingInvoice.status.in_(["draft", "recorded"]),
        )
        .scalar()
        or 0
    )
    month_start = date(today.year, today.month, 1)
    exp_sum = (
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.tenant_id == tenant.id, Expense.expense_date >= month_start)
        .scalar()
        or 0
    )
    return DashboardSummaryOut(
        invoices_today=int(inv_today),
        gross_today=d(gross),
        incoming_open=int(open_in),
        expenses_month=d(exp_sum),
    )
