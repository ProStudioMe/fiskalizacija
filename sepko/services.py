from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from sepko.audit import write_audit
from sepko.efi import CASH_PAY_METHODS, build_inv_num, load_tenant_fiscal
from sepko.models import CashDeposit, Invoice, InvoiceLine, InvoiceStatus, Tenant
from sepko.partner import PartnerAdapter, get_partner_adapter, utcnow
from sepko.schemas import (
    BuyerIn,
    CashDaySummary,
    CashDepositRequest,
    CashDepositResponse,
    FiscalizeRequest,
    FiscalizeResponse,
    InvoiceLineIn,
    TotalsIn,
)


TOLERANCE = Decimal("0.05")


def validate_totals(request: FiscalizeRequest) -> str | None:
    line_sum = sum((line.total_gross for line in request.lines), Decimal("0"))
    if abs(line_sum - request.totals.gross) > TOLERANCE:
        return f"lines sum {line_sum} != totals.gross {request.totals.gross}"
    expected = request.totals.net + request.totals.vat
    if abs(expected - request.totals.gross) > TOLERANCE:
        return f"net+vat {expected} != gross {request.totals.gross}"
    if request.invoice_type == "NONCASH" and not (request.buyer and request.buyer.pib):
        return "buyer.pib required for NONCASH invoices"
    if request.invoice_type == "CASH" and request.payment_method not in CASH_PAY_METHODS:
        return "CASH invoice requires BANKNOTE, CARD or OTHER-CASH payment"
    if request.invoice_type == "NONCASH" and request.payment_method in CASH_PAY_METHODS:
        return "NONCASH invoice cannot use BANKNOTE/CARD/OTHER-CASH"
    return None


def next_ord_num(db: Session, tenant: Tenant, year: int) -> int:
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    current = (
        db.query(func.coalesce(func.max(Invoice.inv_ord_num), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.issue_datetime >= start,
            Invoice.issue_datetime < end,
        )
        .scalar()
    )
    return int(current or 0) + 1


def preview_inv_num(db: Session, tenant: Tenant, when: datetime | None = None) -> tuple[str, int]:
    """Sljedeći EFI broj računa: {PJ}/{rbr}/{godina}/{ENU}."""
    when = when or utcnow()
    fiscal = load_tenant_fiscal(tenant)
    ord_num = next_ord_num(db, tenant, when.year)
    return build_inv_num(fiscal.busin_unit_code, ord_num, when.year, fiscal.tcr_code), ord_num


def _to_response(invoice: Invoice) -> FiscalizeResponse:
    return FiscalizeResponse(
        external_id=invoice.external_id,
        status=invoice.status,
        ikof=invoice.ikof,
        jikr=invoice.jikr,
        qr_url=invoice.qr_url,
        partner_ref=invoice.partner_ref,
        inv_num=invoice.inv_num,
        inv_ord_num=invoice.inv_ord_num,
        fiscalized_at=invoice.fiscalized_at,
        error_message=invoice.error_message,
    )


def fiscalize_invoice(
    db: Session,
    tenant: Tenant,
    request: FiscalizeRequest,
    partner: PartnerAdapter | None = None,
) -> FiscalizeResponse:
    err = validate_totals(request)
    if err:
        return FiscalizeResponse(
            external_id=request.external_id or "",
            status=InvoiceStatus.failed.value,
            error_message=err,
        )

    existing = None
    if request.external_id:
        existing = (
            db.query(Invoice)
            .filter(Invoice.tenant_id == tenant.id, Invoice.external_id == request.external_id)
            .first()
        )
        if existing and existing.status == InvoiceStatus.fiscalized.value:
            return _to_response(existing)

    year = request.issue_datetime.year
    inv_ord_num = request.inv_ord_num or (
        existing.inv_ord_num if existing and existing.inv_ord_num else next_ord_num(db, tenant, year)
    )

    adapter = partner or get_partner_adapter()
    result = adapter.fiscalize(tenant, request, inv_ord_num=inv_ord_num)
    inv_num = result.inv_num or build_inv_num(
        load_tenant_fiscal(tenant).busin_unit_code,
        inv_ord_num,
        year,
        load_tenant_fiscal(tenant).tcr_code,
    )
    external_id = (request.external_id or "").strip() or inv_num

    if existing is None:
        # Ako je auto-broj, idempotency po inv_num
        by_num = (
            db.query(Invoice)
            .filter(Invoice.tenant_id == tenant.id, Invoice.external_id == external_id)
            .first()
        )
        if by_num and by_num.status == InvoiceStatus.fiscalized.value:
            return _to_response(by_num)
        existing = by_num

    if existing is None:
        invoice = Invoice(
            tenant_id=tenant.id,
            external_id=external_id,
            invoice_type=request.invoice_type,
            payment_method=request.payment_method,
            currency=request.currency,
            issue_datetime=request.issue_datetime,
            buyer_pib=request.buyer.pib if request.buyer else None,
            buyer_name=request.buyer.name if request.buyer else None,
            buyer_address=request.buyer.address if request.buyer else None,
            notes=request.notes,
            total_net=request.totals.net,
            total_vat=request.totals.vat,
            total_gross=request.totals.gross,
            payload_json=request.model_dump_json(),
            inv_num=inv_num,
            inv_ord_num=result.inv_ord_num or inv_ord_num,
            type_of_inv=request.invoice_type,
            inv_type=request.inv_type,
        )
        for line in request.lines:
            invoice.lines.append(
                InvoiceLine(
                    code=line.code,
                    name=line.name,
                    quantity=line.quantity,
                    unit_price_net=line.unit_price_net,
                    vat_rate=line.vat_rate,
                    total_gross=line.total_gross,
                )
            )
        db.add(invoice)
    else:
        invoice = existing
        invoice.payload_json = request.model_dump_json()
        invoice.invoice_type = request.invoice_type
        invoice.payment_method = request.payment_method
        invoice.inv_num = inv_num
        invoice.inv_ord_num = result.inv_ord_num or inv_ord_num
        invoice.type_of_inv = request.invoice_type
        invoice.inv_type = request.inv_type
        invoice.external_id = external_id

    if result.ok:
        invoice.status = InvoiceStatus.fiscalized.value
        invoice.ikof = result.ikof
        invoice.jikr = result.jikr
        invoice.qr_url = result.qr_url
        invoice.partner_ref = result.partner_ref
        invoice.fiscalized_at = utcnow()
        invoice.error_message = None
    else:
        invoice.status = InvoiceStatus.failed.value
        invoice.error_message = result.error_message

    db.commit()
    db.refresh(invoice)
    write_audit(
        db,
        "invoice.fiscalize" if result.ok else "invoice.fiscalize_failed",
        tenant_id=tenant.id,
        entity_type="invoice",
        entity_id=invoice.id,
        detail=f"{invoice.inv_num or invoice.external_id} {invoice.status}",
    )
    db.commit()
    return _to_response(invoice)


def save_draft_invoice(
    db: Session,
    tenant: Tenant,
    request: FiscalizeRequest,
    *,
    is_template: bool = False,
) -> Invoice:
    """Sačuvaj fakturu kao nacrt (bez poziva partnera / EFI)."""
    token = secrets.token_hex(8)
    external_id = (request.external_id or "").strip() or f"draft-{token}"
    now = request.issue_datetime or utcnow()
    invoice = Invoice(
        tenant_id=tenant.id,
        external_id=external_id,
        status=InvoiceStatus.draft.value,
        invoice_type=request.invoice_type,
        payment_method=request.payment_method,
        currency=request.currency or "EUR",
        issue_datetime=now,
        buyer_pib=request.buyer.pib if request.buyer else None,
        buyer_name=request.buyer.name if request.buyer else None,
        buyer_address=request.buyer.address if request.buyer else None,
        notes=request.notes,
        total_net=request.totals.net,
        total_vat=request.totals.vat,
        total_gross=request.totals.gross,
        payload_json=request.model_dump_json(),
        type_of_inv=request.invoice_type,
        inv_type=request.inv_type,
        inv_num=None,
        inv_ord_num=None,
        is_template=bool(is_template),
    )
    for line in request.lines:
        invoice.lines.append(
            InvoiceLine(
                code=line.code,
                name=line.name,
                quantity=line.quantity,
                unit_price_net=line.unit_price_net,
                vat_rate=line.vat_rate,
                total_gross=line.total_gross,
            )
        )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    write_audit(
        db,
        "invoice.draft",
        tenant_id=tenant.id,
        entity_type="invoice",
        entity_id=invoice.id,
        detail=invoice.external_id,
    )
    db.commit()
    return invoice


EDITABLE_INVOICE_STATUSES = frozenset(
    {
        InvoiceStatus.draft.value,
        InvoiceStatus.pending.value,
        InvoiceStatus.failed.value,
    }
)


def invoice_is_editable(invoice: Invoice) -> bool:
    return invoice.status in EDITABLE_INVOICE_STATUSES


def update_draft_invoice(
    db: Session,
    tenant: Tenant,
    invoice: Invoice,
    request: FiscalizeRequest,
    *,
    is_template: bool | None = None,
) -> Invoice:
    """Ažuriraj nacrt / nefiskalizovanu fakturu. Fiskalizovane se ne smiju dirati."""
    if invoice.tenant_id != tenant.id:
        raise ValueError("Faktura ne pripada tenant-u.")
    if not invoice_is_editable(invoice):
        raise ValueError("Fiskalizovani račun se ne može mijenjati.")

    now = request.issue_datetime or utcnow()
    invoice.status = InvoiceStatus.draft.value
    invoice.invoice_type = request.invoice_type
    invoice.payment_method = request.payment_method
    invoice.currency = request.currency or "EUR"
    invoice.issue_datetime = now
    invoice.buyer_pib = request.buyer.pib if request.buyer else None
    invoice.buyer_name = request.buyer.name if request.buyer else None
    invoice.buyer_address = request.buyer.address if request.buyer else None
    invoice.notes = request.notes
    invoice.total_net = request.totals.net
    invoice.total_vat = request.totals.vat
    invoice.total_gross = request.totals.gross
    invoice.payload_json = request.model_dump_json()
    invoice.type_of_inv = request.invoice_type
    invoice.inv_type = request.inv_type
    invoice.error_message = None
    if is_template is not None:
        invoice.is_template = bool(is_template)
    # Nacrt nema fiskalne brojeve / identifikatore
    invoice.ikof = None
    invoice.jikr = None
    invoice.qr_url = None
    invoice.partner_ref = None
    invoice.inv_num = None
    invoice.inv_ord_num = None
    invoice.fiscalized_at = None

    invoice.lines.clear()
    for line in request.lines:
        invoice.lines.append(
            InvoiceLine(
                code=line.code,
                name=line.name,
                quantity=line.quantity,
                unit_price_net=line.unit_price_net,
                vat_rate=line.vat_rate,
                total_gross=line.total_gross,
            )
        )
    db.commit()
    db.refresh(invoice)
    write_audit(
        db,
        "invoice.update",
        tenant_id=tenant.id,
        entity_type="invoice",
        entity_id=invoice.id,
        detail=invoice.external_id,
    )
    db.commit()
    return invoice


def invoice_to_fiscalize_request(invoice: Invoice) -> FiscalizeRequest:
    """Rekonstituiši FiscalizeRequest iz sačuvanog nacrta."""
    if invoice.payload_json:
        try:
            req = FiscalizeRequest.model_validate_json(invoice.payload_json)
            return req.model_copy(
                update={
                    "external_id": invoice.external_id,
                    "issue_datetime": utcnow(),
                }
            )
        except Exception:
            pass
    buyer = None
    if invoice.buyer_pib or invoice.buyer_name:
        buyer = BuyerIn(
            pib=invoice.buyer_pib,
            name=invoice.buyer_name,
            address=invoice.buyer_address,
        )
    lines = [
        InvoiceLineIn(
            code=ln.code,
            name=ln.name,
            quantity=ln.quantity,
            unit_price_net=ln.unit_price_net,
            vat_rate=ln.vat_rate,
            total_gross=ln.total_gross,
        )
        for ln in invoice.lines
    ]
    if not lines:
        raise ValueError("Faktura nema stavki")
    return FiscalizeRequest(
        external_id=invoice.external_id,
        issue_datetime=utcnow(),
        invoice_type=invoice.type_of_inv or invoice.invoice_type or "NONCASH",
        payment_method=invoice.payment_method or "ORDER",
        inv_type=invoice.inv_type or "INVOICE",
        currency=invoice.currency or "EUR",
        buyer=buyer,
        lines=lines,
        totals=TotalsIn(
            net=invoice.total_net,
            vat=invoice.total_vat,
            gross=invoice.total_gross,
        ),
        notes=invoice.notes,
    )


def fiscalize_saved_invoice(
    db: Session,
    tenant: Tenant,
    invoice: Invoice,
    partner: PartnerAdapter | None = None,
) -> FiscalizeResponse:
    if invoice.status == InvoiceStatus.fiscalized.value:
        return _to_response(invoice)
    req = invoice_to_fiscalize_request(invoice)
    return fiscalize_invoice(db, tenant, req, partner=partner)


def fiscalize_invoices(
    db: Session,
    tenant: Tenant,
    invoice_ids: list[int],
) -> dict[str, int | list[str]]:
    """Masovna fiskalizacija nacrta / pending / failed. Preskače već fiskalizovane."""
    if not invoice_ids:
        return {"ok": 0, "skipped": 0, "failed": 0, "errors": []}
    sources = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.tenant_id == tenant.id, Invoice.id.in_(invoice_ids))
        .all()
    )
    by_id = {inv.id: inv for inv in sources}
    ok = skipped = failed = 0
    errors: list[str] = []
    for iid in invoice_ids:
        inv = by_id.get(iid)
        if not inv:
            skipped += 1
            continue
        if inv.status == InvoiceStatus.fiscalized.value:
            skipped += 1
            continue
        try:
            result = fiscalize_saved_invoice(db, tenant, inv)
            if result.status == InvoiceStatus.fiscalized.value:
                ok += 1
            else:
                failed += 1
                errors.append(f"#{iid}: {result.error_message or result.status}")
        except Exception as exc:
            failed += 1
            errors.append(f"#{iid}: {exc}")
    return {"ok": ok, "skipped": skipped, "failed": failed, "errors": errors}


def copy_invoices(db: Session, tenant: Tenant, invoice_ids: list[int]) -> list[Invoice]:
    """Kopira odabrane fakture kao nove nacrte (bez fiskalizacije)."""
    if not invoice_ids:
        return []
    # Zadrži redoslijed kako je korisnik označio / kako su došle u formi.
    sources = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.tenant_id == tenant.id, Invoice.id.in_(invoice_ids))
        .all()
    )
    by_id = {inv.id: inv for inv in sources}
    now = utcnow()
    created: list[Invoice] = []
    for src_id in invoice_ids:
        src = by_id.get(src_id)
        if not src:
            continue
        token = secrets.token_hex(6)
        copy = Invoice(
            tenant_id=tenant.id,
            external_id=f"copy-{src.id}-{token}",
            status=InvoiceStatus.draft.value,
            invoice_type=src.invoice_type,
            payment_method=src.payment_method,
            currency=src.currency or "EUR",
            issue_datetime=now,
            buyer_pib=src.buyer_pib,
            buyer_name=src.buyer_name,
            buyer_address=src.buyer_address,
            notes=src.notes,
            total_net=src.total_net,
            total_vat=src.total_vat,
            total_gross=src.total_gross,
            payload_json=src.payload_json or "{}",
            type_of_inv=src.type_of_inv,
            inv_type=src.inv_type,
            # Novi broj i fiskalni podaci se dodjeljuju tek pri fiskalizaciji.
            inv_num=None,
            inv_ord_num=None,
            ikof=None,
            jikr=None,
            qr_url=None,
            partner_ref=None,
            fiscalized_at=None,
            error_message=None,
            is_template=False,
        )
        for line in src.lines:
            copy.lines.append(
                InvoiceLine(
                    code=line.code,
                    name=line.name,
                    quantity=line.quantity,
                    unit_price_net=line.unit_price_net,
                    vat_rate=line.vat_rate,
                    total_gross=line.total_gross,
                )
            )
        db.add(copy)
        created.append(copy)
    if created:
        db.commit()
        for inv in created:
            db.refresh(inv)
    return created


def get_invoice_status(db: Session, tenant: Tenant, external_id: str) -> FiscalizeResponse | None:
    invoice = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.external_id == external_id)
        .first()
    )
    if not invoice:
        return None
    return _to_response(invoice)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if day.month == 12:
        end = datetime(day.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        # next day
        from datetime import timedelta

        end = start + timedelta(days=1)
    return start, end


def cash_day_summary(db: Session, tenant: Tenant, day: date | None = None) -> CashDaySummary:
    day = day or utcnow().date()
    start, end = _day_bounds(day)

    deposits = (
        db.query(CashDeposit)
        .filter(
            CashDeposit.tenant_id == tenant.id,
            CashDeposit.change_datetime >= start,
            CashDeposit.change_datetime < end,
            CashDeposit.status == "registered",
        )
        .all()
    )
    initial = Decimal("0")
    withdrawals = Decimal("0")
    has_initial = False
    for d in deposits:
        if d.operation == "INITIAL":
            initial = d.amount
            has_initial = True
        elif d.operation == "WITHDRAW":
            withdrawals += d.amount

    banknote = (
        db.query(func.coalesce(func.sum(Invoice.total_gross), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.payment_method == "BANKNOTE",
            Invoice.issue_datetime >= start,
            Invoice.issue_datetime < end,
        )
        .scalar()
    )
    banknote_sales = Decimal(str(banknote or 0))
    inv_count = (
        db.query(func.count(Invoice.id))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.issue_datetime >= start,
            Invoice.issue_datetime < end,
        )
        .scalar()
    )
    cash_in = initial + banknote_sales - withdrawals
    return CashDaySummary(
        date=day.isoformat(),
        initial=initial,
        banknote_sales=banknote_sales,
        withdrawals=withdrawals,
        cash_in_drawer=cash_in,
        has_initial=has_initial,
        invoice_count=int(inv_count or 0),
    )


def register_cash_deposit(
    db: Session,
    tenant: Tenant,
    request: CashDepositRequest,
    partner: PartnerAdapter | None = None,
) -> CashDepositResponse:
    change_dt = request.change_datetime or utcnow()
    fiscal = load_tenant_fiscal(tenant)
    day = change_dt.date()
    start, end = _day_bounds(day)

    if request.operation == "INITIAL":
        existing = (
            db.query(CashDeposit)
            .filter(
                CashDeposit.tenant_id == tenant.id,
                CashDeposit.operation == "INITIAL",
                CashDeposit.change_datetime >= start,
                CashDeposit.change_datetime < end,
                CashDeposit.status == "registered",
            )
            .first()
        )
        # Dozvoli izmjenu INITIAL dok nema fiskalizovanih računa taj dan
        sold = (
            db.query(func.count(Invoice.id))
            .filter(
                Invoice.tenant_id == tenant.id,
                Invoice.status == InvoiceStatus.fiscalized.value,
                Invoice.issue_datetime >= start,
                Invoice.issue_datetime < end,
            )
            .scalar()
        )
        if existing and int(sold or 0) > 0:
            return CashDepositResponse(
                id=existing.id,
                operation=existing.operation,
                amount=existing.amount,
                status="failed",
                tcr_code=existing.tcr_code,
                change_datetime=existing.change_datetime,
                error_message="INITIAL se ne može mijenjati nakon prvog računa danas",
            )
        if existing:
            existing.status = "superseded"

    adapter = partner or get_partner_adapter()
    result = adapter.register_cash_deposit(tenant, request, change_datetime=change_dt)

    row = CashDeposit(
        tenant_id=tenant.id,
        operation=request.operation,
        amount=request.amount,
        tcr_code=fiscal.tcr_code,
        operator_code=fiscal.operator_code,
        change_datetime=change_dt,
        partner_ref=result.partner_ref,
        status="registered" if result.ok else "failed",
        error_message=result.error_message,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    write_audit(
        db,
        f"cash.{request.operation.lower()}",
        tenant_id=tenant.id,
        entity_type="cash_deposit",
        entity_id=row.id,
        detail=f"{request.operation} {request.amount}",
    )
    db.commit()
    return CashDepositResponse(
        id=row.id,
        operation=row.operation,
        amount=row.amount,
        status=row.status,
        tcr_code=row.tcr_code,
        change_datetime=row.change_datetime,
        partner_ref=row.partner_ref,
        error_message=row.error_message,
    )
