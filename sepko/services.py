from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from sepko.audit import write_audit
from sepko.efi import (
    CASH_PAY_METHODS,
    CREDIT_INV_TYPES,
    INV_TYPES,
    build_inv_num,
    display_inv_num,
    format_display_inv_num,
    load_tenant_fiscal,
)
from sepko.models import (
    CashDeposit,
    CustomerPayment,
    Invoice,
    InvoiceLine,
    InvoiceSchedule,
    InvoiceStatus,
    Tenant,
)
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
    """Sljedeći EFI InvOrdNum unutar godine (mora biti jedinstven u InvNum)."""
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


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def next_local_ord_num(db: Session, tenant: Tenant, when: datetime | None = None) -> int:
    """Sljedeći lokalni rbr u mjesecu (za prikaz 1-MM-###). Ne ide kroz cijelu godinu."""
    when = when or utcnow()
    start, end = _month_bounds(when.year, when.month)
    current = (
        db.query(func.coalesce(func.max(Invoice.local_ord_num), 0))
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.is_template.is_(False),
            Invoice.issue_datetime >= start,
            Invoice.issue_datetime < end,
            Invoice.local_ord_num.isnot(None),
        )
        .scalar()
    )
    return int(current or 0) + 1


def backfill_local_ord_nums(db: Session, tenant_id: int | None = None) -> int:
    """Dodijeli local_ord_num postojećim računima (po mjesecu, redoslijed po inv_ord_num/id)."""
    q = db.query(Invoice).filter(
        Invoice.is_template.is_(False),
        Invoice.local_ord_num.is_(None),
        Invoice.inv_ord_num.isnot(None),
    )
    if tenant_id is not None:
        q = q.filter(Invoice.tenant_id == tenant_id)
    rows = q.order_by(Invoice.tenant_id, Invoice.issue_datetime, Invoice.inv_ord_num, Invoice.id).all()
    counters: dict[tuple[int, int, int], int] = {}
    n = 0
    for inv in rows:
        when = inv.issue_datetime or utcnow()
        key = (inv.tenant_id, when.year, when.month)
        # Nastavi od postojećeg maxa u tom mjesecu
        if key not in counters:
            start, end = _month_bounds(when.year, when.month)
            counters[key] = int(
                db.query(func.coalesce(func.max(Invoice.local_ord_num), 0))
                .filter(
                    Invoice.tenant_id == inv.tenant_id,
                    Invoice.issue_datetime >= start,
                    Invoice.issue_datetime < end,
                    Invoice.local_ord_num.isnot(None),
                )
                .scalar()
                or 0
            )
        counters[key] += 1
        inv.local_ord_num = counters[key]
        n += 1
    if n:
        db.commit()
    return n


def preview_inv_num(db: Session, tenant: Tenant, when: datetime | None = None) -> tuple[str, int]:
    """Sljedeći EFI broj računa: {PJ}/{rbr}/{godina}/{ENU}. Vraća i godišnji rbr."""
    when = when or utcnow()
    fiscal = load_tenant_fiscal(tenant)
    ord_num = next_ord_num(db, tenant, when.year)
    return build_inv_num(fiscal.busin_unit_code, ord_num, when.year, fiscal.tcr_code), ord_num


def preview_local_display_num(db: Session, tenant: Tenant, when: datetime | None = None) -> str:
    """Sljedeći lokalni prikaz 1-MM-### (mjesečni rbr)."""
    when = when or utcnow()
    return format_display_inv_num(next_local_ord_num(db, tenant, when), when)


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
        offline=bool(
            invoice.ikof
            and not invoice.jikr
            and invoice.status == InvoiceStatus.pending.value
        ),
    )


def fiscalize_invoice(
    db: Session,
    tenant: Tenant,
    request: FiscalizeRequest,
    partner: PartnerAdapter | None = None,
) -> FiscalizeResponse:
    if request.inv_type not in INV_TYPES:
        return FiscalizeResponse(
            external_id=request.external_id or "",
            status=InvoiceStatus.failed.value,
            error_message="Predračun se ne fiskalizuje — sačuvaj kao nacrt ili promijeni tip u Račun.",
        )

    err = validate_totals(request)
    if err:
        return FiscalizeResponse(
            external_id=request.external_id or "",
            status=InvoiceStatus.failed.value,
            error_message=err,
        )
    if request.inv_type in CREDIT_INV_TYPES and not (request.iic_ref or "").strip():
        return FiscalizeResponse(
            external_id=request.external_id or "",
            status=InvoiceStatus.failed.value,
            error_message="Za knjižno odobrenje / korektivni račun obavezan je IKOF originala (iic_ref).",
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
    local_ord_num = (
        existing.local_ord_num
        if existing and existing.local_ord_num
        else next_local_ord_num(db, tenant, request.issue_datetime)
    )

    reuse_iic = existing.ikof if existing and existing.ikof and not existing.jikr else None
    reuse_sig = (
        existing.iic_signature
        if existing and existing.ikof and not existing.jikr
        else None
    )

    adapter = partner or get_partner_adapter()
    result = adapter.fiscalize(
        tenant,
        request,
        inv_ord_num=inv_ord_num,
        reuse_iic=reuse_iic,
        reuse_iic_signature=reuse_sig,
    )
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
            local_ord_num=local_ord_num,
            type_of_inv=request.invoice_type,
            inv_type=request.inv_type,
            ref_ikof=(request.iic_ref or None),
            ref_invoice_id=request.ref_invoice_id,
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
                    tax_rate_code=line.tax_rate_code,
                    unit=line.unit or "KOM",
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
        if not invoice.local_ord_num:
            invoice.local_ord_num = local_ord_num
        invoice.type_of_inv = request.invoice_type
        invoice.inv_type = request.inv_type
        invoice.external_id = external_id
        invoice.ref_ikof = request.iic_ref or invoice.ref_ikof
        if request.ref_invoice_id:
            invoice.ref_invoice_id = request.ref_invoice_id
        invoice.total_net = request.totals.net
        invoice.total_vat = request.totals.vat
        invoice.total_gross = request.totals.gross

    if result.ok:
        invoice.status = InvoiceStatus.fiscalized.value
        invoice.ikof = result.ikof
        invoice.jikr = result.jikr
        invoice.qr_url = result.qr_url
        invoice.partner_ref = result.partner_ref
        invoice.iic_signature = result.iic_signature or invoice.iic_signature
        invoice.fiscalized_at = utcnow()
        invoice.error_message = None
    elif result.offline and result.ikof:
        # Zakon: račun se može izdati bez JIKR; isti IKOF se šalje ponovo u 48h
        invoice.status = InvoiceStatus.pending.value
        invoice.ikof = result.ikof
        invoice.iic_signature = result.iic_signature
        invoice.qr_url = result.qr_url
        invoice.partner_ref = result.partner_ref
        invoice.jikr = None
        invoice.fiscalized_at = None
        invoice.error_message = result.error_message or (
            "Offline: CIS nedostupan — JIKR u roku od 48h (isti IKOF)."
        )
    else:
        invoice.status = InvoiceStatus.failed.value
        invoice.error_message = result.error_message

    db.commit()
    db.refresh(invoice)
    write_audit(
        db,
        (
            "invoice.fiscalize"
            if result.ok
            else ("invoice.fiscalize_offline" if result.offline else "invoice.fiscalize_failed")
        ),
        tenant_id=tenant.id,
        entity_type="invoice",
        entity_id=invoice.id,
        detail=f"{display_inv_num(invoice)} {invoice.status}",
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
                tax_rate_code=line.tax_rate_code,
                unit=line.unit or "KOM",
            )
        )
    invoice.ref_ikof = request.iic_ref
    invoice.ref_invoice_id = request.ref_invoice_id
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
    # Offline račun sa IKOF bez JIKR — ne mijenjaj (isti IKOF u 48h)
    if invoice.ikof and not invoice.jikr:
        return False
    return invoice.status in EDITABLE_INVOICE_STATUSES


def delete_draft_invoice(db: Session, tenant: Tenant, invoice: Invoice) -> None:
    """Trajno obriši nacrt / nefiskalizovanu fakturu. Fiskalizovane se ne smiju brisati."""
    if invoice.tenant_id != tenant.id:
        raise ValueError("Faktura ne pripada tenant-u.")
    if invoice.ikof:
        raise ValueError("Račun sa IKOF se ne smije brisati (fiskalizovan ili offline 48h).")
    if not invoice_is_editable(invoice):
        raise ValueError("Fiskalizovani račun se ne može obrisati.")

    as_template = (
        db.query(InvoiceSchedule.id)
        .filter(InvoiceSchedule.template_invoice_id == invoice.id)
        .first()
    )
    if as_template:
        raise ValueError(
            "Faktura je šablon za automatsku fakturu — prvo obriši ili zamijeni raspored."
        )

    db.query(InvoiceSchedule).filter(InvoiceSchedule.last_invoice_id == invoice.id).update(
        {InvoiceSchedule.last_invoice_id: None},
        synchronize_session=False,
    )
    db.query(CustomerPayment).filter(CustomerPayment.invoice_id == invoice.id).update(
        {CustomerPayment.invoice_id: None},
        synchronize_session=False,
    )

    label = display_inv_num(invoice)
    if label in ("—", "Nacrt"):
        label = invoice.external_id or str(invoice.id)
    write_audit(
        db,
        "invoice.delete",
        tenant_id=tenant.id,
        entity_type="invoice",
        entity_id=invoice.id,
        detail=f"obrisan nacrt {label} ({invoice.status})",
    )
    db.delete(invoice)


def delete_draft_invoices(db: Session, tenant: Tenant, invoice_ids: list[int]) -> dict:
    """Obriši više nacrta. Vraća broj obrisanih i listu grešaka."""
    ok = 0
    skipped = 0
    errors: list[str] = []
    invoices = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.id.in_(invoice_ids))
        .all()
    )
    by_id = {inv.id: inv for inv in invoices}
    for iid in invoice_ids:
        inv = by_id.get(iid)
        if not inv:
            skipped += 1
            continue
        try:
            delete_draft_invoice(db, tenant, inv)
            ok += 1
        except ValueError as exc:
            errors.append(f"#{iid}: {exc}")
            skipped += 1
    if ok:
        db.commit()
    return {"ok": ok, "skipped": skipped, "errors": errors}


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
    invoice.ref_ikof = request.iic_ref
    invoice.ref_invoice_id = request.ref_invoice_id
    if is_template is not None:
        invoice.is_template = bool(is_template)
    # Nacrt nema fiskalne brojeve / identifikatore
    invoice.ikof = None
    invoice.jikr = None
    invoice.qr_url = None
    invoice.partner_ref = None
    invoice.iic_signature = None
    invoice.inv_num = None
    invoice.inv_ord_num = None
    invoice.local_ord_num = None
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
                tax_rate_code=line.tax_rate_code,
                unit=line.unit or "KOM",
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
    keep_dt = bool(invoice.ikof and not invoice.jikr)
    if invoice.payload_json:
        try:
            req = FiscalizeRequest.model_validate_json(invoice.payload_json)
            updates: dict = {
                "external_id": invoice.external_id,
                "iic_ref": invoice.ref_ikof or req.iic_ref,
                "ref_invoice_id": invoice.ref_invoice_id or req.ref_invoice_id,
            }
            if not keep_dt:
                updates["issue_datetime"] = utcnow()
            else:
                updates["issue_datetime"] = invoice.issue_datetime
                if invoice.inv_ord_num:
                    updates["inv_ord_num"] = invoice.inv_ord_num
            return req.model_copy(update=updates)
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
            tax_rate_code=ln.tax_rate_code,
            unit=ln.unit or "KOM",
        )
        for ln in invoice.lines
    ]
    if not lines:
        raise ValueError("Faktura nema stavki")
    return FiscalizeRequest(
        external_id=invoice.external_id,
        issue_datetime=invoice.issue_datetime if keep_dt else utcnow(),
        invoice_type=invoice.type_of_inv or invoice.invoice_type or "NONCASH",
        payment_method=invoice.payment_method or "ORDER",
        inv_type=invoice.inv_type or "INVOICE",
        inv_ord_num=invoice.inv_ord_num if keep_dt else None,
        currency=invoice.currency or "EUR",
        buyer=buyer,
        lines=lines,
        totals=TotalsIn(
            net=invoice.total_net,
            vat=invoice.total_vat,
            gross=invoice.total_gross,
        ),
        notes=invoice.notes,
        iic_ref=invoice.ref_ikof,
        ref_invoice_id=invoice.ref_invoice_id,
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


def retry_offline_invoices(
    db: Session,
    *,
    tenant_id: int | None = None,
    within_hours: int = 48,
) -> dict[str, int | list[str]]:
    """Ponovi RegisterInvoice za offline račune (IKOF bez JIKR) unutar 48h."""
    cutoff = utcnow() - timedelta(hours=within_hours)
    q = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(
            Invoice.status == InvoiceStatus.pending.value,
            Invoice.ikof.isnot(None),
            Invoice.jikr.is_(None),
            Invoice.is_template.is_(False),
            Invoice.issue_datetime >= cutoff,
        )
    )
    if tenant_id is not None:
        q = q.filter(Invoice.tenant_id == tenant_id)
    rows = q.order_by(Invoice.id).limit(200).all()
    ok = failed = skipped = 0
    errors: list[str] = []
    for inv in rows:
        tenant = db.get(Tenant, inv.tenant_id)
        if not tenant:
            skipped += 1
            continue
        try:
            result = fiscalize_saved_invoice(db, tenant, inv)
            if result.status == InvoiceStatus.fiscalized.value:
                ok += 1
            elif result.offline:
                skipped += 1
            else:
                failed += 1
                if result.error_message:
                    errors.append(f"#{inv.id}: {result.error_message}")
        except Exception as exc:
            failed += 1
            errors.append(f"#{inv.id}: {exc}")
    return {"ok": ok, "failed": failed, "skipped": skipped, "errors": errors[:20]}


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
                    tax_rate_code=line.tax_rate_code,
                    unit=line.unit or "KOM",
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
