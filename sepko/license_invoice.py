"""Račun za produženje licence — HTML mail (VG stil) + PDF prilog."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sepko.brand import (
    COMPANY_ADDRESS,
    COMPANY_BILLING_EMAIL,
    COMPANY_DOMAIN,
    COMPANY_NAME,
    COMPANY_PHONE,
    COMPANY_PIB,
    LICENSE_ITEM,
)
from sepko.finansije import safe_filename
from sepko.licenses import LICENSE_WARN_DAYS, license_days_left, license_extend_until
from sepko.models import AdminAuditLog, LicenseInvoice, Tenant, TenantStatus, User
from sepko.money import format_amount, parse_nonneg_money
from sepko.platform_mail import parse_from_email, platform_from_header, send_platform_mail
from sepko.web_templates import templates

ROOT = Path(__file__).resolve().parents[1]
_NUM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.][^@\s]{0,253}\.[A-Za-z]{2,64}$")
_MONTHS_MAX = 120
_DEFAULT_MONTHLY = Decimal("15.00")


@dataclass
class PlatformBilling:
    issuer_name: str = COMPANY_NAME
    issuer_address: str = COMPANY_ADDRESS
    issuer_email: str = COMPANY_BILLING_EMAIL
    issuer_pib: str = COMPANY_PIB
    issuer_iban: str = ""
    issuer_phone: str = COMPANY_PHONE
    signer_name: str = ""
    signer_title: str = "Licence i pretplata"
    item_name: str = LICENSE_ITEM
    monthly_price: Decimal = _DEFAULT_MONTHLY
    payment_method: str = "Transakcioni Račun, Virman"
    pdf_font: str = ""


@dataclass
class LicenseInvoiceDraft:
    number: str
    buyer_name: str
    to_email: str
    issue_date: date
    due_date: date
    amount: Decimal
    qty: Decimal
    qty_unit: str
    item_name: str
    payment_method: str
    message: str
    iban: str
    issuer_name: str
    issuer_address: str
    issuer_email: str
    issuer_pib: str
    issuer_phone: str
    kind: str = "invoice"  # invoice | proforma

    @property
    def doc_label(self) -> str:
        return "Profaktura" if self.kind == "proforma" else "Račun"


def load_platform_billing() -> PlatformBilling:
    load_dotenv(ROOT / ".env")
    price = parse_nonneg_money(os.getenv("SEPKO_LICENSE_MONTHLY_PRICE") or "15") or _DEFAULT_MONTHLY
    from_email = parse_from_email(platform_from_header())
    issuer_email = (os.getenv("SEPKO_BILLING_EMAIL") or from_email or COMPANY_BILLING_EMAIL).strip()[:255]
    return PlatformBilling(
        issuer_name=(os.getenv("SEPKO_BILLING_NAME") or COMPANY_NAME).strip()[:255] or COMPANY_NAME,
        issuer_address=(os.getenv("SEPKO_BILLING_ADDRESS") or COMPANY_ADDRESS).strip()[:255],
        issuer_email=issuer_email or COMPANY_BILLING_EMAIL,
        issuer_pib=(os.getenv("SEPKO_BILLING_PIB") or COMPANY_PIB).strip()[:32] or COMPANY_PIB,
        issuer_iban=(os.getenv("SEPKO_BILLING_IBAN") or "").strip()[:512],
        issuer_phone=(os.getenv("SEPKO_BILLING_PHONE") or COMPANY_PHONE).strip()[:64],
        signer_name=(os.getenv("SEPKO_BILLING_SIGNER_NAME") or "").strip()[:255],
        signer_title=(os.getenv("SEPKO_BILLING_SIGNER_TITLE") or "Licence i pretplata").strip()[:128]
        or "Licence i pretplata",
        item_name=(os.getenv("SEPKO_LICENSE_ITEM_NAME") or LICENSE_ITEM).strip()[:255]
        or LICENSE_ITEM,
        monthly_price=price,
        payment_method=(os.getenv("SEPKO_BILLING_PAYMENT_METHOD") or "Transakcioni Račun, Virman").strip()[:128]
        or "Transakcioni Račun, Virman",
        pdf_font=(os.getenv("SEPKO_PDF_FONT") or "").strip(),
    )


def fmt_date(value: date | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y")


def default_months_for_license(license_type: str | None) -> int:
    return 12 if (license_type or "") == "yearly" else 6


def default_personal_message(*, signer_name: str, billing: PlatformBilling, kind: str = "invoice") -> str:
    if kind == "proforma":
        body = (
            "Poštovani,\n"
            "u prilogu je profaktura za produžetak licence.\n"
            "Molimo uplatu da licenca ostane aktivna."
        )
    else:
        body = "Poštovani,\nu prilogu je račun za produžetak licence."
    lines = [
        body,
        "Srdačan pozdrav,",
        signer_name.strip() or billing.signer_name or billing.issuer_name,
        billing.signer_title,
        billing.issuer_name,
    ]
    if billing.issuer_address:
        lines.append(billing.issuer_address)
    if billing.issuer_phone:
        lines.append(f"Tel: {billing.issuer_phone}")
    if billing.issuer_email:
        lines.append(f"e-mail: {billing.issuer_email}")
    return "\n".join(line for line in lines if line).strip()


def next_license_invoice_number(db: Session, *, today: date | None = None, kind: str = "invoice") -> str:
    today = today or date.today()
    yy = today.year % 100
    prefix = f"PF-{yy:02d}-" if kind == "proforma" else f"01-{yy:02d}-"
    last = (
        db.query(LicenseInvoice)
        .filter(LicenseInvoice.number.like(f"{prefix}%"))
        .order_by(LicenseInvoice.id.desc())
        .first()
    )
    seq = 1
    if last:
        tail = (last.number or "").rsplit("-", 1)[-1]
        try:
            seq = int(tail) + 1
        except ValueError:
            seq = 1
    if seq > 9999:
        seq = 1
    return f"{prefix}{seq:04d}"


def validate_invoice_number(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if not _NUM_RE.match(text):
        return None
    return text


def validate_email_list(raw: str) -> list[str]:
    parts = [x.strip() for x in (raw or "").replace(";", ",").split(",") if x.strip()]
    out: list[str] = []
    for addr in parts:
        if len(addr) > 255 or not _EMAIL_RE.match(addr):
            return []
        out.append(addr)
    return out


def parse_months(raw: Any) -> int | None:
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        n = int(Decimal(text))
    except Exception:
        return None
    if n < 1 or n > _MONTHS_MAX:
        return None
    return n


def draft_from_row(row: LicenseInvoice, billing: PlatformBilling) -> LicenseInvoiceDraft:
    return LicenseInvoiceDraft(
        number=row.number,
        buyer_name=row.buyer_name,
        to_email=row.to_email,
        issue_date=row.issue_date,
        due_date=row.due_date,
        amount=row.amount,
        qty=row.qty,
        qty_unit=row.qty_unit or "mj",
        item_name=row.item_name,
        payment_method=row.payment_method,
        message=row.message or "",
        iban=row.iban or billing.issuer_iban,
        issuer_name=billing.issuer_name,
        issuer_address=billing.issuer_address,
        issuer_email=billing.issuer_email,
        issuer_pib=billing.issuer_pib,
        issuer_phone=billing.issuer_phone,
        kind=(row.kind or "invoice"),
    )


def render_license_invoice_html(draft: LicenseInvoiceDraft) -> str:
    amount_txt = format_amount(draft.amount)
    qty_txt = format_amount(draft.qty)
    return templates.env.get_template("emails/license_invoice.html").render(
        draft=draft,
        doc_label=draft.doc_label,
        amount_txt=amount_txt,
        qty_txt=qty_txt,
        issue_txt=fmt_date(draft.issue_date),
        due_txt=fmt_date(draft.due_date),
        message_lines=[ln for ln in (draft.message or "").splitlines()],
    )


def render_license_invoice_text(draft: LicenseInvoiceDraft) -> str:
    amount_txt = format_amount(draft.amount)
    qty_txt = format_amount(draft.qty)
    lines = [
        f"{draft.issuer_name}",
        COMPANY_DOMAIN,
        "",
        f"{draft.doc_label} {draft.number}",
        f"{fmt_date(draft.issue_date)} · Datum valute {fmt_date(draft.due_date)}",
        "",
        f"Poštovani/a {draft.buyer_name},",
        f"U prilogu je {draft.doc_label} {draft.number} od {draft.issuer_name}.",
        "",
    ]
    if draft.message:
        lines.extend([draft.message, ""])
    lines.extend(
        [
            f"Za platiti: {amount_txt} €",
            f"Datum valute: {fmt_date(draft.due_date)}",
            f"Način plaćanja: {draft.payment_method}",
            "",
            "Stavke",
            f"{draft.item_name}  {qty_txt} {draft.qty_unit}  {amount_txt}€",
            f"Ukupno  {amount_txt}€",
            "",
            "Kako platiti",
            f"Primalac: {draft.issuer_name}",
            f"IBAN: {draft.iban or '—'}",
            f"Svrha / poziv na broj: {draft.number}",
            "",
            draft.issuer_name,
            draft.issuer_address,
            draft.issuer_email,
            f"PIB: {draft.issuer_pib}" if draft.issuer_pib else "",
            "",
            f"Odgovorite na ovaj mejl ako imate pitanje — stići će {draft.issuer_name}.",
            "",
            "U prilogu je PDF.",
        ]
    )
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _find_pdf_font(explicit: str = "") -> tuple[str, str] | None:
    """(regular, bold) TTF putanje, ili None."""
    candidates: list[tuple[str, str]] = []
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return (str(p), str(p))
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates.extend(
        [
            (str(windir / "Fonts" / "arial.ttf"), str(windir / "Fonts" / "arialbd.ttf")),
            (str(windir / "Fonts" / "calibri.ttf"), str(windir / "Fonts" / "calibrib.ttf")),
        ]
    )
    candidates.extend(
        [
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ),
        ]
    )
    for regular, bold in candidates:
        if Path(regular).is_file():
            bold_path = bold if Path(bold).is_file() else regular
            return regular, bold_path
    return None


def render_license_invoice_pdf(draft: LicenseInvoiceDraft, *, font_path: str = "") -> bytes | None:
    """A4 PDF. None ako nema TTF, fpdf2, ili layout ne uspije — mail ide i bez PDF-a."""
    fonts = _find_pdf_font(font_path)
    if fonts is None:
        return None
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    try:
        regular, bold = fonts
        pdf = FPDF(format="A4", unit="mm")
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.add_page()
        pdf.add_font("Inv", "", regular)
        pdf.add_font("Inv", "B", bold)
        amount_txt = format_amount(draft.amount)
        qty_txt = format_amount(draft.qty)
        nxt = {"new_x": "LMARGIN", "new_y": "NEXT"}

        pdf.set_font("Inv", "B", 14)
        pdf.cell(0, 8, draft.issuer_name, **nxt)
        pdf.set_font("Inv", "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(0, 5, COMPANY_DOMAIN, **nxt)
        if draft.issuer_address:
            pdf.cell(0, 5, draft.issuer_address, **nxt)
        pdf.set_text_color(17, 17, 17)
        pdf.ln(2)
        pdf.set_font("Inv", "B", 18)
        pdf.cell(0, 10, f"{draft.doc_label} {draft.number}", **nxt)
        pdf.set_font("Inv", "", 10)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(
            0,
            6,
            f"{fmt_date(draft.issue_date)}  ·  Datum valute {fmt_date(draft.due_date)}",
            **nxt,
        )
        pdf.set_text_color(17, 17, 17)
        pdf.ln(4)
        pdf.set_font("Inv", "", 11)
        pdf.multi_cell(0, 6, f"Poštovani/a {draft.buyer_name},", **nxt)
        pdf.multi_cell(
            0,
            6,
            f"U prilogu je {draft.doc_label} {draft.number} od {draft.issuer_name}.",
            **nxt,
        )
        if draft.message:
            pdf.ln(2)
            pdf.set_fill_color(245, 245, 245)
            pdf.multi_cell(0, 6, draft.message, fill=True, **nxt)
        pdf.ln(4)
        pdf.set_font("Inv", "B", 14)
        col = 58
        pdf.cell(col, 8, f"{amount_txt} €")
        pdf.cell(col, 8, fmt_date(draft.due_date))
        pdf.multi_cell(0, 8, draft.payment_method, **nxt)
        pdf.set_font("Inv", "", 8)
        pdf.set_text_color(110, 110, 110)
        pdf.cell(col, 5, "Za platiti")
        pdf.cell(col, 5, "Datum valute")
        pdf.cell(0, 5, "Način plaćanja", **nxt)
        pdf.set_text_color(17, 17, 17)
        pdf.ln(6)
        pdf.set_font("Inv", "B", 12)
        pdf.cell(0, 8, "Stavke", **nxt)
        pdf.set_font("Inv", "B", 9)
        pdf.cell(110, 7, "Opis")
        pdf.cell(30, 7, "Kol")
        pdf.cell(0, 7, "Iznos", **nxt)
        pdf.set_draw_color(210, 210, 210)
        y = pdf.get_y()
        pdf.line(10, y, 200, y)
        pdf.set_font("Inv", "", 10)
        pdf.cell(110, 8, (draft.item_name or "")[:60])
        pdf.cell(30, 8, f"{qty_txt} {draft.qty_unit}")
        pdf.cell(0, 8, f"{amount_txt}€", **nxt)
        pdf.set_font("Inv", "B", 11)
        pdf.cell(140, 8, "Ukupno")
        pdf.cell(0, 8, f"{amount_txt}€", **nxt)
        pdf.ln(4)
        pdf.set_font("Inv", "B", 12)
        pdf.cell(0, 8, "Kako platiti", **nxt)
        pdf.set_fill_color(245, 245, 245)
        pdf.set_font("Inv", "", 10)
        pay = (
            f"Primalac: {draft.issuer_name}\n"
            f"IBAN: {draft.iban or '—'}\n"
            f"Svrha / poziv na broj: {draft.number}"
        )
        pdf.multi_cell(0, 6, pay, fill=True, **nxt)
        pdf.ln(8)
        pdf.set_font("Inv", "", 9)
        pdf.set_text_color(80, 80, 80)
        footer = [draft.issuer_name]
        if draft.issuer_address:
            footer.append(draft.issuer_address)
        if draft.issuer_email:
            footer.append(draft.issuer_email)
        if draft.issuer_pib:
            footer.append(f"PIB: {draft.issuer_pib}")
        pdf.multi_cell(0, 5, "\n".join(footer), **nxt)
        buf = BytesIO()
        pdf.output(buf)
        return buf.getvalue()
    except Exception:
        return None


def apply_license_extension(tenant: Tenant, months: int, *, today: date | None = None) -> date:
    today = today or date.today()
    until = license_extend_until(tenant.license_until, months, today=today)
    tenant.license_until = until
    if tenant.license_from is None:
        tenant.license_from = today
    tenant.license_type = "yearly" if months >= 12 else "monthly"
    if tenant.status == TenantStatus.trial.value:
        tenant.status = TenantStatus.active.value
    return until


def send_license_invoice_mail(
    draft: LicenseInvoiceDraft,
    *,
    billing: PlatformBilling | None = None,
) -> dict[str, Any]:
    billing = billing or load_platform_billing()
    html = render_license_invoice_html(draft)
    text = render_license_invoice_text(draft)
    amount_txt = format_amount(draft.amount)
    subject = f"{draft.doc_label} {draft.number} · {amount_txt} € · valuta {fmt_date(draft.due_date)}"
    attachments: list[tuple[str, bytes, str]] = []
    slug = safe_filename(draft.number)
    prefix = "Profaktura" if draft.kind == "proforma" else "Racun"
    pdf = render_license_invoice_pdf(draft, font_path=billing.pdf_font)
    if pdf:
        attachments.append((f"{prefix}-{slug}.pdf", pdf, "application/pdf"))
    else:
        attachments.append((f"{prefix}-{slug}.html", html.encode("utf-8"), "text/html"))
    return send_platform_mail(
        to_addrs=[draft.to_email],
        subject=subject,
        body_text=text,
        body_html=html,
        attachments=attachments,
        reply_to=draft.issuer_email or None,
    )


def persist_and_send(
    db: Session,
    tenant: Tenant,
    draft: LicenseInvoiceDraft,
    *,
    extend: bool,
    months: int,
    billing: PlatformBilling | None = None,
    today: date | None = None,
    covers_until: date | None = None,
) -> tuple[LicenseInvoice, dict[str, Any], date | None]:
    today = today or date.today()
    billing = billing or load_platform_billing()
    kind = draft.kind if draft.kind == "proforma" else "invoice"
    if kind == "proforma":
        extend = False
    row = LicenseInvoice(
        number=draft.number,
        tenant_id=tenant.id,
        to_email=draft.to_email,
        buyer_name=draft.buyer_name[:255],
        issue_date=draft.issue_date,
        due_date=draft.due_date,
        amount=draft.amount,
        qty=draft.qty,
        qty_unit=draft.qty_unit[:16],
        item_name=draft.item_name[:255],
        payment_method=draft.payment_method[:128],
        message=(draft.message or "")[:4000] or None,
        iban=(draft.iban or "")[:512] or None,
        kind=kind,
        covers_until=covers_until,
        status="failed",
    )
    db.add(row)
    db.flush()
    until: date | None = None
    if extend:
        until = apply_license_extension(tenant, months, today=today)
        row.covers_until = until
    elif row.covers_until is None:
        row.covers_until = tenant.license_until
    result = send_license_invoice_mail(draft, billing=billing)
    if result.get("ok"):
        row.status = "sent"
        row.sent_at = datetime.now(timezone.utc)
        row.provider_id = (str(result.get("id") or "")[:128] or None)
        row.error = None
    else:
        row.status = "failed"
        row.error = str(result.get("message") or "Slanje nije uspjelo")[:2000]
    db.flush()
    return row, result, until


def tenant_notify_email(db: Session, tenant: Tenant) -> str:
    rows = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.active.is_(True))
        .order_by(User.id)
        .all()
    )
    for user in rows:
        if (user.role or "") == "admin" and (user.email or "").strip():
            return user.email.strip().lower()
    for user in rows:
        if (user.email or "").strip():
            return user.email.strip().lower()
    return ""


def proforma_already_sent(db: Session, tenant_id: int, covers_until: date | None) -> bool:
    if covers_until is None:
        return False
    return (
        db.query(LicenseInvoice)
        .filter(
            LicenseInvoice.tenant_id == tenant_id,
            LicenseInvoice.kind == "proforma",
            LicenseInvoice.covers_until == covers_until,
            LicenseInvoice.status == "sent",
        )
        .first()
        is not None
    )


def build_renewal_draft(
    *,
    tenant: Tenant,
    billing: PlatformBilling,
    to_email: str,
    number: str,
    months: int,
    amount: Decimal,
    kind: str,
    issue_date: date,
    due_date: date,
    item_name: str | None = None,
    payment_method: str | None = None,
    iban: str | None = None,
    message: str | None = None,
    signer_name: str = "",
) -> LicenseInvoiceDraft:
    kind_c = "proforma" if kind == "proforma" else "invoice"
    return LicenseInvoiceDraft(
        number=number,
        buyer_name=(tenant.name or "")[:255],
        to_email=to_email,
        issue_date=issue_date,
        due_date=due_date,
        amount=amount,
        qty=Decimal(months),
        qty_unit="mj",
        item_name=(item_name or billing.item_name)[:255],
        payment_method=(payment_method or billing.payment_method)[:128],
        message=(
            message
            if message is not None
            else default_personal_message(signer_name=signer_name, billing=billing, kind=kind_c)
        ),
        iban=(iban if iban is not None else billing.issuer_iban)[:512],
        issuer_name=billing.issuer_name,
        issuer_address=billing.issuer_address,
        issuer_email=billing.issuer_email,
        issuer_pib=billing.issuer_pib,
        issuer_phone=billing.issuer_phone,
        kind=kind_c,
    )


def run_due_license_proformas(db: Session, *, today: date | None = None) -> dict[str, Any]:
    """Jednom dnevno: plaćenim tenantima čija licenca ističe (< 30 dana) pošalji profakturu."""
    today = today or date.today()
    billing = load_platform_billing()
    tenants = (
        db.query(Tenant)
        .filter(Tenant.license_until.isnot(None), Tenant.status != TenantStatus.suspended.value)
        .order_by(Tenant.id)
        .all()
    )
    sent = skipped = failed = 0
    messages: list[str] = []
    for tenant in tenants:
        if (tenant.license_type or "trial") == "trial":
            continue
        days = license_days_left(tenant.license_until, today=today)
        if days is None or days >= LICENSE_WARN_DAYS:
            continue
        if proforma_already_sent(db, tenant.id, tenant.license_until):
            skipped += 1
            continue
        to_email = tenant_notify_email(db, tenant)
        if not to_email:
            skipped += 1
            messages.append(f"{tenant.slug}: nema email admina")
            continue
        months = default_months_for_license(tenant.license_type)
        amount = (billing.monthly_price * Decimal(months)).quantize(Decimal("0.01"))
        due = tenant.license_until if tenant.license_until and tenant.license_until >= today else today
        number = next_license_invoice_number(db, today=today, kind="proforma")
        draft = build_renewal_draft(
            tenant=tenant,
            billing=billing,
            to_email=to_email,
            number=number,
            months=months,
            amount=amount,
            kind="proforma",
            issue_date=today,
            due_date=due,
        )
        try:
            row, result, _until = persist_and_send(
                db,
                tenant,
                draft,
                extend=False,
                months=months,
                billing=billing,
                today=today,
                covers_until=tenant.license_until,
            )
            db.add(
                AdminAuditLog(
                    actor_user_id=None,
                    actor_email="cron",
                    tenant_id=tenant.id,
                    action="license.proforma",
                    detail=f"{row.number} → {to_email} (ističe {tenant.license_until})",
                )
            )
            db.commit()
            if result.get("ok"):
                sent += 1
                messages.append(f"{tenant.slug}: {row.number} → {to_email}")
            else:
                failed += 1
                messages.append(f"{tenant.slug}: {result.get('message')}")
        except IntegrityError:
            db.rollback()
            failed += 1
            messages.append(f"{tenant.slug}: broj profakture već postoji")
        except Exception as exc:
            db.rollback()
            failed += 1
            messages.append(f"{tenant.slug}: {exc}")
    return {
        "ok": sent,
        "skipped": skipped,
        "failed": failed,
        "messages": messages,
        "day": today.isoformat(),
    }
