"""Mjesečni raspored faktura — kopija šablona na odabrane dane + mail."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from sepko.efi import display_inv_num
from sepko.finansije import load_mail_settings, safe_filename, send_firm_mail
from sepko.models import Customer, Invoice, InvoiceSchedule, InvoiceStatus, Tenant
from sepko.money import format_amount
from sepko.partner import utcnow
from sepko.services import copy_invoices, fiscalize_saved_invoice


MONTH_NAMES_CNR = (
    "januar",
    "februar",
    "mart",
    "april",
    "maj",
    "jun",
    "jul",
    "avgust",
    "septembar",
    "oktobar",
    "novembar",
    "decembar",
)


def parse_days_of_month(raw: str | None) -> list[int]:
    days: list[int] = []
    for part in (raw or "1").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            d = int(part)
        except ValueError:
            continue
        if 1 <= d <= 31 and d not in days:
            days.append(d)
    return sorted(days) or [1]


def format_days_of_month(days: list[int]) -> str:
    return ",".join(str(d) for d in sorted(set(days)) if 1 <= d <= 31) or "1"


def normalize_period_mode(raw: str | None) -> str:
    mode = (raw or "previous").strip().lower()
    return "current" if mode == "current" else "previous"


def _months_last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def billing_period(today: date, period_mode: str | None = "previous") -> dict[str, Any]:
    """Period fakturisanja: prethodni ili tekući mjesec."""
    mode = normalize_period_mode(period_mode)
    if mode == "current":
        year, month = today.year, today.month
    else:
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
    start = date(year, month, 1)
    end = date(year, month, _months_last_day(year, month))
    label = f"{MONTH_NAMES_CNR[month - 1]} {year}"
    range_label = f"{start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}"
    return {
        "mode": mode,
        "start": start,
        "end": end,
        "label": label,
        "range_label": range_label,
    }


def schedule_due_today(schedule: InvoiceSchedule, today: date | None = None) -> bool:
    today = today or utcnow().date()
    days = parse_days_of_month(schedule.days_of_month)
    last = _months_last_day(today.year, today.month)
    # Ako je zakazano 31. a mjesec ima 30, pokreni zadnji dan mjeseca
    effective = set()
    for d in days:
        effective.add(min(d, last))
    if today.day not in effective:
        return False
    run_key = today.isoformat()
    return schedule.last_run_key != run_key


def _upsert_note_line(notes: str | None, prefix: str, value: str) -> str:
    """Zamijeni ili dodaj liniju koja počinje sa prefix (case-insensitive)."""
    prefix_l = prefix.lower()
    lines: list[str] = []
    replaced = False
    for line in (notes or "").splitlines():
        if line.strip().lower().startswith(prefix_l):
            lines.append(f"{prefix}{value}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"{prefix}{value}")
    return "\n".join(lines).strip()


def _refresh_due_note(notes: str | None, due: date) -> str:
    return _upsert_note_line(notes, "Rok plaćanja: ", due.isoformat())


def _apply_schedule_meta(
    notes: str | None,
    *,
    contract_number: str | None,
    period: dict[str, Any],
    schedule_name: str,
    run_key: str,
) -> str:
    text = notes or ""
    if contract_number:
        text = _upsert_note_line(text, "Broj ugovora: ", contract_number)
    text = _upsert_note_line(text, "Period: ", f"{period['label']} ({period['range_label']})")
    tag = f"Automatska faktura: {schedule_name} ({run_key})"
    if tag not in text:
        text = (text.rstrip() + "\n" + tag) if text.strip() else tag
    return text.strip()


def _invoice_email_html(
    invoice: Invoice,
    *,
    contract_number: str | None = None,
    period_label: str | None = None,
) -> str:
    when = (
        invoice.issue_datetime.strftime("%d.%m.%Y %H:%M")
        if invoice.issue_datetime
        else "—"
    )
    extra = ""
    if contract_number:
        extra += f"<p>Ugovor: <strong>{contract_number}</strong></p>"
    if period_label:
        extra += f"<p>Period: <strong>{period_label}</strong></p>"
    return (
        "<!DOCTYPE html><html><body style='font-family:Segoe UI,sans-serif;color:#111'>"
        f"<h2>Faktura {display_inv_num(invoice)}</h2>"
        f"<p>{invoice.buyer_name or ''} · PIB {invoice.buyer_pib or '—'}</p>"
        f"<p>Datum: {when}</p>"
        f"{extra}"
        f"<p><strong>Za uplatu: {format_amount(invoice.total_gross)} €</strong></p>"
        f"<p>Status: {invoice.status}</p>"
        f"<p>IKOF: {invoice.ikof or '—'}<br>JIKR: {invoice.jikr or '—'}</p>"
        f"<p><a href='{invoice.qr_url or '#'}'>Verifikacija QR</a></p>"
        "</body></html>"
    )


def run_schedule(
    db: Session,
    tenant: Tenant,
    schedule: InvoiceSchedule,
    *,
    today: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    today = today or utcnow().date()
    if not schedule.active and not force:
        return {"ok": False, "skipped": True, "message": "Automatska faktura nije aktivna."}
    if not force and not schedule_due_today(schedule, today):
        return {"ok": False, "skipped": True, "message": "Danas nije dan za ovu automatsku fakturu."}

    run_key = today.isoformat()
    if not force and schedule.last_run_key == run_key:
        return {"ok": False, "skipped": True, "message": "Već pokrenuto danas."}

    template = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == schedule.template_invoice_id)
        .first()
    )
    if not template:
        schedule.last_error = "Šablon fakture nije pronađen."
        db.commit()
        return {"ok": False, "message": schedule.last_error}

    created = copy_invoices(db, tenant, [template.id])
    if not created:
        schedule.last_error = "Kopija fakture nije uspjela."
        db.commit()
        return {"ok": False, "message": schedule.last_error}

    inv = created[0]
    due = today + timedelta(days=15)
    period = billing_period(today, getattr(schedule, "period_mode", None) or "previous")
    contract = (getattr(schedule, "contract_number", None) or "").strip() or None

    inv.notes = _refresh_due_note(inv.notes, due)
    inv.notes = _apply_schedule_meta(
        inv.notes,
        contract_number=contract,
        period=period,
        schedule_name=schedule.name or str(schedule.id),
        run_key=run_key,
    )
    db.commit()
    db.refresh(inv)

    fiscal_ok = None
    if schedule.auto_fiscalize:
        try:
            result = fiscalize_saved_invoice(db, tenant, inv)
            fiscal_ok = result.status == InvoiceStatus.fiscalized.value
            if not fiscal_ok:
                schedule.last_error = result.error_message or "Fiskalizacija nije uspjela."
            db.refresh(inv)
        except Exception as exc:
            fiscal_ok = False
            schedule.last_error = f"Fiskalizacija: {exc}"

    mail_ok = None
    mail_msg = ""
    if schedule.auto_email:
        customer = None
        if schedule.customer_id:
            customer = (
                db.query(Customer)
                .filter(Customer.tenant_id == tenant.id, Customer.id == schedule.customer_id)
                .first()
            )
        elif inv.buyer_pib:
            customer = (
                db.query(Customer)
                .filter(Customer.tenant_id == tenant.id, Customer.pib == inv.buyer_pib)
                .first()
            )
        to_raw = (schedule.email_to or (customer.email if customer else "") or "").strip()
        to_addrs = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
        mail = load_mail_settings(tenant)
        period_bit = period["label"]
        subject_bits = [f"Faktura {display_inv_num(inv)}"]
        if contract:
            subject_bits.append(f"ugovor {contract}")
        subject_bits.append(period_bit)
        subject = f"{' — '.join(subject_bits)} — {tenant.name}"
        body_lines = [
            "Poštovani,",
            "",
            f"u prilogu je faktura {display_inv_num(inv)}",
            f"za {inv.buyer_name or 'vas'}",
            f"({format_amount(inv.total_gross)} €).",
        ]
        if contract:
            body_lines.append(f"Broj ugovora: {contract}")
        body_lines.append(f"Period: {period_bit} ({period['range_label']})")
        body_lines.extend(["", f"Srdačan pozdrav,", tenant.name, ""])
        body = "\n".join(body_lines)
        html = _invoice_email_html(
            inv, contract_number=contract, period_label=f"{period_bit} ({period['range_label']})"
        )
        fname = safe_filename(display_inv_num(inv)) + ".html"
        result = send_firm_mail(
            mail=mail,
            to_addrs=to_addrs,
            subject=subject,
            body_text=body,
            body_html=body.replace("\n", "<br>"),
            attachments=[(fname, html.encode("utf-8"), "text/html")],
        )
        mail_ok = bool(result.get("ok"))
        mail_msg = str(result.get("message") or "")
        if not mail_ok:
            schedule.last_error = mail_msg

    schedule.last_run_key = run_key
    schedule.last_invoice_id = inv.id
    if fiscal_ok is not False and mail_ok is not False:
        schedule.last_error = None
    schedule.updated_at = datetime.now(timezone.utc)
    db.commit()

    parts = [f"Kreirana faktura #{inv.id} ({display_inv_num(inv)})"]
    if contract:
        parts.append(f"ugovor {contract}")
    parts.append(f"period {period['label']}")
    if schedule.auto_fiscalize:
        parts.append("fiskalizovano" if fiscal_ok else "fiskalizacija nije uspjela")
    if schedule.auto_email:
        parts.append(mail_msg or ("poslato" if mail_ok else "mail nije poslat"))
    return {
        "ok": True,
        "invoice_id": inv.id,
        "message": "; ".join(parts),
        "fiscal_ok": fiscal_ok,
        "mail_ok": mail_ok,
        "period": period["label"],
        "contract_number": contract,
    }


def run_due_schedules(
    db: Session,
    *,
    today: date | None = None,
    tenant_id: int | None = None,
) -> dict[str, Any]:
    today = today or utcnow().date()
    q = (
        db.query(InvoiceSchedule)
        .options(joinedload(InvoiceSchedule.tenant))
        .filter(InvoiceSchedule.active.is_(True))
    )
    if tenant_id is not None:
        q = q.filter(InvoiceSchedule.tenant_id == tenant_id)
    schedules = q.all()
    ran = skipped = failed = 0
    messages: list[str] = []
    for sch in schedules:
        if not schedule_due_today(sch, today):
            skipped += 1
            continue
        tenant = sch.tenant
        try:
            result = run_schedule(db, tenant, sch, today=today, force=False)
            if result.get("skipped"):
                skipped += 1
            elif result.get("ok"):
                ran += 1
                messages.append(f"#{sch.id}: {result.get('message')}")
            else:
                failed += 1
                messages.append(f"#{sch.id}: {result.get('message')}")
        except Exception as exc:
            failed += 1
            sch.last_error = str(exc)
            db.commit()
            messages.append(f"#{sch.id}: {exc}")
    return {"ok": ran, "skipped": skipped, "failed": failed, "messages": messages, "day": today.isoformat()}
