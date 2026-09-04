"""Ulazne fakture, dobavljači, troškovnik — domain helpers."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from sepko.audit import write_audit
from sepko.models import (
    Expense,
    ExpenseCategory,
    IncomingInvoice,
    IncomingInvoiceLine,
    IncomingInvoiceSource,
    IncomingInvoiceStatus,
    Supplier,
    Tenant,
)
from sepko.qr_import import QrInvoiceDraft, draft_from_qr

DEFAULT_EXPENSE_CATEGORIES = (
    ("Gorivo", "#ea580c"),
    ("Telekom", "#2563eb"),
    ("Najam", "#7c3aed"),
    ("Materijal", "#059669"),
    ("Ostalo", "#64748b"),
)


def d(value: Any) -> Decimal:
    from sepko.money import parse_amount

    return parse_amount(value, quantize="0.01") or Decimal("0.00")


def ensure_expense_categories(db: Session, tenant: Tenant) -> list[ExpenseCategory]:
    existing = (
        db.query(ExpenseCategory)
        .filter(ExpenseCategory.tenant_id == tenant.id)
        .order_by(ExpenseCategory.name)
        .all()
    )
    if existing:
        return existing
    rows = []
    for name, color in DEFAULT_EXPENSE_CATEGORIES:
        row = ExpenseCategory(tenant_id=tenant.id, name=name, color=color, active=True)
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def get_or_create_supplier(
    db: Session,
    tenant: Tenant,
    *,
    pib: str,
    name: str,
    street: str | None = None,
    city: str | None = None,
) -> Supplier:
    pib_c = (pib or "").strip()
    name_c = (name or "").strip() or pib_c or "Dobavljač"
    if not pib_c:
        raise ValueError("PIB dobavljača je obavezan")
    row = (
        db.query(Supplier)
        .filter(Supplier.tenant_id == tenant.id, Supplier.pib == pib_c)
        .first()
    )
    if row:
        if name_c and row.name != name_c:
            row.name = name_c[:255]
        return row
    row = Supplier(
        tenant_id=tenant.id,
        pib=pib_c[:32],
        name=name_c[:255],
        street=(street or None),
        city=(city or None),
        country="Crna Gora",
        active=True,
    )
    db.add(row)
    db.flush()
    return row


def _recalc_totals(lines: list[dict[str, Any]]) -> tuple[Decimal, Decimal, Decimal]:
    net = Decimal("0")
    vat = Decimal("0")
    gross = Decimal("0")
    for ln in lines:
        qty = Decimal(str(ln.get("quantity") or 1))
        up = Decimal(str(ln.get("unit_price_net") or 0))
        rate = Decimal(str(ln.get("vat_rate") or 0))
        line_net = (qty * up).quantize(Decimal("0.01"))
        line_vat = (line_net * rate / Decimal("100")).quantize(Decimal("0.01"))
        line_gross = d(ln.get("total_gross"))
        if line_gross <= 0:
            line_gross = (line_net + line_vat).quantize(Decimal("0.01"))
        ln["total_gross"] = line_gross
        net += line_net
        vat += line_vat
        gross += line_gross
    return net.quantize(Decimal("0.01")), vat.quantize(Decimal("0.01")), gross.quantize(Decimal("0.01"))


def create_incoming_invoice(
    db: Session,
    tenant: Tenant,
    *,
    number: str = "",
    issue_date: date | None = None,
    supplier_pib: str | None = None,
    supplier_name: str | None = None,
    supplier_id: int | None = None,
    status: str = IncomingInvoiceStatus.recorded.value,
    source: str = IncomingInvoiceSource.manual.value,
    currency: str = "EUR",
    notes: str | None = None,
    ikof: str | None = None,
    jikr: str | None = None,
    qr_url: str | None = None,
    lines: list[dict[str, Any]] | None = None,
    total_net: Decimal | None = None,
    total_vat: Decimal | None = None,
    total_gross: Decimal | None = None,
) -> IncomingInvoice:
    lines = list(lines or [])
    supplier: Supplier | None = None
    if supplier_id:
        supplier = (
            db.query(Supplier)
            .filter(Supplier.tenant_id == tenant.id, Supplier.id == supplier_id)
            .first()
        )
    elif supplier_pib:
        supplier = get_or_create_supplier(
            db, tenant, pib=supplier_pib, name=supplier_name or supplier_pib
        )

    if lines:
        net, vat, gross = _recalc_totals(lines)
    else:
        net = d(total_net)
        vat = d(total_vat)
        gross = d(total_gross)
        if gross > 0 and net == 0 and vat == 0:
            # Assume 21% included if only gross known
            net = (gross / Decimal("1.21")).quantize(Decimal("0.01"))
            vat = (gross - net).quantize(Decimal("0.01"))
        if not lines and gross > 0:
            lines = [
                {
                    "code": "",
                    "name": notes or number or "Ulazna faktura",
                    "quantity": Decimal("1"),
                    "unit_price_net": net,
                    "vat_rate": Decimal("21") if vat else Decimal("0"),
                    "total_gross": gross,
                }
            ]

    inv = IncomingInvoice(
        tenant_id=tenant.id,
        supplier_id=supplier.id if supplier else None,
        number=(number or "")[:64],
        issue_date=issue_date,
        supplier_pib=(supplier.pib if supplier else supplier_pib or None),
        supplier_name=(supplier.name if supplier else supplier_name or None),
        status=status,
        source=source,
        currency=(currency or "EUR")[:8],
        total_net=net,
        total_vat=vat,
        total_gross=gross,
        ikof=(ikof or None),
        jikr=(jikr or None),
        qr_url=(qr_url or None),
        notes=notes,
    )
    db.add(inv)
    db.flush()
    for ln in lines:
        db.add(
            IncomingInvoiceLine(
                invoice_id=inv.id,
                code=str(ln.get("code") or "")[:64],
                name=str(ln.get("name") or "Stavka")[:255],
                quantity=Decimal(str(ln.get("quantity") or 1)),
                unit_price_net=Decimal(str(ln.get("unit_price_net") or 0)),
                vat_rate=Decimal(str(ln.get("vat_rate") or 0)),
                total_gross=d(ln.get("total_gross")),
            )
        )
    write_audit(
        db,
        "incoming_invoice.create",
        tenant_id=tenant.id,
        entity_type="incoming_invoice",
        entity_id=inv.id,
        detail=f"{inv.number} {inv.total_gross} {inv.source}",
    )
    db.flush()
    return inv


def create_from_qr(
    db: Session,
    tenant: Tenant,
    raw_qr: str,
    *,
    fetch: bool = True,
    status: str = IncomingInvoiceStatus.recorded.value,
) -> tuple[IncomingInvoice, QrInvoiceDraft]:
    draft = draft_from_qr(raw_qr, fetch=fetch)
    inv = create_incoming_invoice(
        db,
        tenant,
        number=draft.number or (draft.ikof or "")[:64],
        issue_date=draft.issue_date,
        supplier_pib=draft.supplier_pib,
        supplier_name=draft.supplier_name or draft.supplier_pib,
        status=status,
        source=IncomingInvoiceSource.qr.value,
        notes=draft.notes or draft.fetch_error,
        ikof=draft.ikof,
        jikr=draft.jikr,
        qr_url=draft.qr_url,
        lines=draft.lines or None,
        total_gross=draft.total_gross,
    )
    return inv, draft


def expense_from_incoming(
    db: Session,
    tenant: Tenant,
    invoice: IncomingInvoice,
    *,
    category_id: int | None = None,
    description: str | None = None,
) -> Expense:
    ensure_expense_categories(db, tenant)
    exp = Expense(
        tenant_id=tenant.id,
        category_id=category_id,
        incoming_invoice_id=invoice.id,
        amount=invoice.total_gross or Decimal("0"),
        expense_date=invoice.issue_date or date.today(),
        description=description
        or f"Ulazna {invoice.number or invoice.id} — {invoice.supplier_name or ''}".strip(),
    )
    db.add(exp)
    write_audit(
        db,
        "expense.create",
        tenant_id=tenant.id,
        entity_type="expense",
        entity_id=None,
        detail=f"from incoming #{invoice.id}",
    )
    db.flush()
    return exp


def month_expense_summary(
    db: Session, tenant: Tenant, *, year: int, month: int
) -> list[dict[str, Any]]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    rows = (
        db.query(Expense)
        .filter(
            Expense.tenant_id == tenant.id,
            Expense.expense_date >= start,
            Expense.expense_date < end,
        )
        .all()
    )
    by_cat: dict[str, Decimal] = {}
    for e in rows:
        cat = e.category.name if e.category else "Bez kategorije"
        by_cat[cat] = by_cat.get(cat, Decimal("0")) + (e.amount or Decimal("0"))
    return [
        {"category": k, "total": float(v)}
        for k, v in sorted(by_cat.items(), key=lambda x: -x[1])
    ]


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    v = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v[:10], fmt).date()
        except ValueError:
            continue
    return None
