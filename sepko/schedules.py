"""Mjesečni raspored faktura — kopija šablona na odabrane dane + mail."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from sepko.efi import display_inv_num
from sepko.finansije import load_mail_settings, safe_filename, send_firm_mail
from sepko.models import Customer, Invoice, InvoiceSchedule, InvoiceStatus, Tenant
from sepko.partner import utcnow
from sepko.services import copy_invoices, fiscalize_saved_invoice


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


def _months_last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


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


def _refresh_due_note(notes: str | None, due: date) -> str:
    lines = []
    replaced = False
    for line in (notes or "").splitlines():
        if line.strip().lower().startswith("rok plaćanja:"):
            lines.append(f"Rok plaćanja: {due.isoformat()}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.insert(0, f"Rok plaćanja: {due.isoformat()}")
    return "\n".join(lines)


def _invoice_email_html(invoice: Invoice) -> str:
    when = (
        invoice.issue_datetime.strftime("%d.%m.%Y %H:%M")
        if invoice.issue_datetime
        else "—"
    )
    return (
        "<!DOCTYPE html><html><body style='font-family:Segoe UI,sans-serif;color:#111'>"
        f"<h2>Faktura {display_inv_num(invoice)}</h2>"
        f"<p>{invoice.buyer_name or ''} · PIB {invoice.buyer_pib or '—'}</p>"
        f"<p>Datum: {when}</p>"
        f"<p><strong>Za uplatu: {float(invoice.total_gross):.2f} {invoice.currency or 'EUR'}</strong></p>"
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
        return {"ok": False, "skipped": True, "message": "Raspored nije aktivan."}
    if not force and not schedule_due_today(schedule, today):
        return {"ok": False, "skipped": True, "message": "Danas nije dan za ovaj raspored."}

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
    inv.notes = _refresh_due_note(inv.notes, due)
    # Označi da je iz rasporeda
    tag = f"Raspored: {schedule.name or schedule.id} ({run_key})"
    if inv.notes:
        if tag not in inv.notes:
            inv.notes = inv.notes.rstrip() + "\n" + tag
    else:
        inv.notes = tag
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
        subject = f"Faktura {display_inv_num(inv)} — {tenant.name}"
        body = (
            f"Poštovani,\n\n"
            f"u prilogu je faktura {display_inv_num(inv)} "
            f"za {inv.buyer_name or 'vas'} "
            f"({float(inv.total_gross):.2f} {inv.currency or 'EUR'}).\n\n"
            f"Srdačan pozdrav,\n{tenant.name}\n"
        )
        html = _invoice_email_html(inv)
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
