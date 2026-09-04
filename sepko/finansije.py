"""Finansije — kartice, izvodi, mail (port iz Finasije / ProStudio)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.orm import Session

from sepko.config import get_settings
from sepko.crypto import decrypt_secret, encrypt_secret
from sepko.efi import _settings_dict, _write_settings
from sepko.models import (
    BankStatement,
    BankTransaction,
    Customer,
    CustomerPayment,
    Invoice,
    InvoiceMailSeen,
    Tenant,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
IZVODI_DIR = DATA_DIR / "izvodi"
FAKTURE_DIR = DATA_DIR / "fakture"


@dataclass
class MailSettings:
    imap_host: str = "fusion.mxrouting.net"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_sent_folder: str = "Sent"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = "SEPKO"
    smtp_use_tls: bool = True
    mail_since_date: str = "2026-01-01"
    izvod_subjects: str = "Hipotekarna banka - Izvod racuna"
    faktura_subjects: str = "Faktura"
    enabled: bool = False


def load_mail_settings(tenant: Tenant | None = None) -> MailSettings:
    """Env (global) + tenant.settings_json.mail override."""
    load_dotenv(ROOT / ".env")
    s = MailSettings(
        imap_host=os.getenv("IMAP_HOST", "fusion.mxrouting.net"),
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        imap_user=os.getenv("IMAP_USER", ""),
        imap_password=os.getenv("IMAP_PASSWORD", ""),
        imap_folder=os.getenv("IMAP_FOLDER", "INBOX"),
        imap_sent_folder=os.getenv("IMAP_SENT_FOLDER", "Sent"),
        smtp_host=os.getenv("SMTP_HOST", "") or os.getenv("IMAP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", "") or os.getenv("IMAP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", "") or os.getenv("IMAP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", "") or os.getenv("IMAP_USER", ""),
        smtp_from_name=os.getenv("SMTP_FROM_NAME", "SEPKO"),
        smtp_use_tls=os.getenv("SMTP_USE_TLS", "1") not in ("0", "false", "False"),
        mail_since_date=os.getenv("MAIL_SINCE_DATE", "2026-01-01"),
        izvod_subjects=os.getenv(
            "MAIL_IZVOD_SUBJECTS",
            "Hipotekarna banka - Izvod racuna",
        ),
        faktura_subjects=os.getenv("MAIL_FAKTURA_SUBJECTS", "Faktura"),
        enabled=bool(os.getenv("IMAP_USER") and os.getenv("IMAP_PASSWORD")),
    )
    if tenant is not None:
        data = _settings_dict(tenant).get("mail") or {}
        if isinstance(data, dict):
            for k, v in data.items():
                if not hasattr(s, k) or v in (None, ""):
                    continue
                cur = getattr(s, k)
                if isinstance(cur, bool):
                    setattr(s, k, str(v).lower() in ("1", "true", "yes", "on"))
                elif isinstance(cur, int):
                    try:
                        setattr(s, k, int(v))
                    except (TypeError, ValueError):
                        pass
                else:
                    setattr(s, k, v)
            s.imap_password = decrypt_secret(s.imap_password)
            s.smtp_password = decrypt_secret(s.smtp_password)
            if data.get("imap_user") and (data.get("imap_password") or s.imap_password):
                s.enabled = True
    return s


def save_mail_settings(tenant: Tenant, mail: MailSettings) -> None:
    data = _settings_dict(tenant)
    payload = asdict(mail)
    if payload.get("imap_password"):
        payload["imap_password"] = encrypt_secret(str(payload["imap_password"]))
    if payload.get("smtp_password"):
        payload["smtp_password"] = encrypt_secret(str(payload["smtp_password"]))
    data["mail"] = payload
    _write_settings(tenant, data)


def subject_terms(raw: str) -> list[str]:
    out: list[str] = []
    for line in (raw or "").replace(",", "\n").splitlines():
        t = line.strip()
        if t and t not in out:
            out.append(t)
    return out or ["Izvod"]


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def statement_day_from_subject(subject: str) -> str | None:
    m = re.search(r"za dan\s+(\d{2})[./](\d{2})[./](\d{4})", subject or "", re.I)
    if not m:
        m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", subject or "")
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


# —— Finansijska kartica ——

def customer_ledger(db: Session, tenant: Tenant, customer: Customer) -> list[dict[str, Any]]:
    """Fakture (duguje) + uplate (potražuje) + tekući saldo."""
    events: list[dict[str, Any]] = []
    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.buyer_pib == customer.pib,
            Invoice.status == "fiscalized",
        )
        .order_by(Invoice.issue_datetime.asc(), Invoice.id.asc())
        .all()
    )
    for inv in invoices:
        day = inv.issue_datetime.strftime("%Y-%m-%d") if inv.issue_datetime else ""
        amt = float(inv.total_gross or 0)
        doc = inv.inv_num or (f"1-1-{inv.inv_ord_num}/{inv.issue_datetime.year}" if inv.inv_ord_num and inv.issue_datetime else f"#{inv.id}")
        events.append(
            {
                "sort_date": day,
                "sort_key": 0,
                "sort_id": inv.id,
                "datum": day,
                "vrsta": "Faktura",
                "dokument": doc,
                "opis": inv.payment_method or "Račun",
                "duguje": amt,
                "potrazuje": 0.0,
                "invoice_id": inv.id,
            }
        )

    pays = (
        db.query(CustomerPayment)
        .filter(CustomerPayment.tenant_id == tenant.id, CustomerPayment.customer_id == customer.id)
        .order_by(CustomerPayment.paid_at.asc(), CustomerPayment.id.asc())
        .all()
    )
    for p in pays:
        day = p.paid_at or ""
        events.append(
            {
                "sort_date": day,
                "sort_key": 1,
                "sort_id": p.id,
                "datum": day,
                "vrsta": "Uplata",
                "dokument": f"uplata #{p.id}" if not p.bank_tx_id else f"izvod #{p.bank_tx_id}",
                "opis": p.note or "Uplata",
                "duguje": 0.0,
                "potrazuje": float(p.amount or 0),
                "invoice_id": p.invoice_id,
            }
        )

    events.sort(key=lambda e: (e["sort_date"] or "", e["sort_key"], e["sort_id"]))
    saldo = 0.0
    out: list[dict[str, Any]] = []
    for e in events:
        saldo = round(saldo + e["duguje"] - e["potrazuje"], 2)
        out.append(
            {
                "Datum": e["datum"],
                "Vrsta": e["vrsta"],
                "Dokument": e["dokument"],
                "Opis": e["opis"],
                "Duguje": e["duguje"] or None,
                "Potražuje": e["potrazuje"] or None,
                "Saldo": saldo,
                "invoice_id": e.get("invoice_id"),
            }
        )
    return out


def customer_summary(db: Session, tenant: Tenant, customer: Customer) -> dict[str, Any]:
    ledger = customer_ledger(db, tenant, customer)
    saldo = ledger[-1]["Saldo"] if ledger else 0.0
    fakturisano = sum(float(r["Duguje"] or 0) for r in ledger)
    uplaceno = sum(float(r["Potražuje"] or 0) for r in ledger)
    return {
        "customer_id": customer.id,
        "pib": customer.pib,
        "naziv": customer.name,
        "email": customer.email or "",
        "telefon": customer.phone or "",
        "fakturisano": round(fakturisano, 2),
        "uplaceno": round(uplaceno, 2),
        "dug": round(saldo, 2),
        "stavki": len(ledger),
    }


def build_kartica_html(
    *,
    naziv: str,
    pib: str,
    summary: dict[str, Any],
    ledger: list[dict[str, Any]],
    auto_print: bool = False,
) -> str:
    rows = []
    for r in ledger:
        dug = f'{r["Duguje"]:.2f}' if r.get("Duguje") else ""
        pot = f'{r["Potražuje"]:.2f}' if r.get("Potražuje") else ""
        rows.append(
            f"<tr><td>{r.get('Datum') or ''}</td><td>{r.get('Vrsta') or ''}</td>"
            f"<td>{r.get('Dokument') or ''}</td><td>{r.get('Opis') or ''}</td>"
            f"<td class='num'>{dug}</td><td class='num'>{pot}</td>"
            f"<td class='num'><strong>{r.get('Saldo', 0):.2f}</strong></td></tr>"
        )
    print_js = "window.onload=function(){window.print();};" if auto_print else ""
    return f"""<!DOCTYPE html>
<html lang="sr"><head><meta charset="utf-8">
<title>Kartica — {naziv}</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;color:#111;margin:24px;}}
h1{{font-size:1.35rem;margin:0 0 .35rem}}
.meta{{color:#555;margin-bottom:1.25rem;font-size:.92rem}}
table{{width:100%;border-collapse:collapse;font-size:.88rem}}
th,td{{border-bottom:1px solid #ddd;padding:.45rem .35rem;text-align:left}}
th{{font-size:.72rem;text-transform:uppercase;color:#666}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.totals{{margin-top:1rem;display:flex;gap:2rem;font-size:.95rem}}
@media print{{body{{margin:12px}}}}
</style></head><body>
<h1>Finansijska kartica</h1>
<div class="meta"><strong>{naziv}</strong> · PIB {pib}<br>
Fakturisano {summary.get('fakturisano',0):.2f} € · Uplaćeno {summary.get('uplaceno',0):.2f} € ·
<strong>Dug {summary.get('dug',0):.2f} €</strong></div>
<table><thead><tr>
<th>Datum</th><th>Vrsta</th><th>Dokument</th><th>Opis</th>
<th class="num">Duguje</th><th class="num">Potražuje</th><th class="num">Saldo</th>
</tr></thead><tbody>
{''.join(rows) or '<tr><td colspan="7">Nema stavki</td></tr>'}
</tbody></table>
<script>{print_js}</script>
</body></html>"""


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", (name or "firma").strip())[:60] or "firma"


# —— SMTP ——

def send_firm_mail(
    *,
    mail: MailSettings,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> dict[str, Any]:
    """attachments: (filename, data, mime)."""
    if not mail.smtp_host or not mail.smtp_user or not mail.smtp_password:
        return {"ok": False, "message": "SMTP nije podešen (SMTP_HOST / USER / PASSWORD)."}
    recipients = [a.strip() for a in to_addrs if a and a.strip()]
    if not recipients:
        return {"ok": False, "message": "Nema adrese primaoca."}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((mail.smtp_from_name or "SEPKO", mail.smtp_from or mail.smtp_user))
    msg["To"] = ", ".join(recipients)
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    for filename, data, mime in attachments or []:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)

    try:
        if mail.smtp_use_tls:
            with smtplib.SMTP(mail.smtp_host, mail.smtp_port, timeout=60) as smtp:
                smtp.starttls()
                smtp.login(mail.smtp_user, mail.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(mail.smtp_host, mail.smtp_port, timeout=60) as smtp:
                smtp.login(mail.smtp_user, mail.smtp_password)
                smtp.send_message(msg)
        return {"ok": True, "message": f"Poslato na {', '.join(recipients)}."}
    except Exception as exc:
        return {"ok": False, "message": f"SMTP greška: {exc}"}


# —— IMAP izvodi (pojednostavljeno) ——

def fetch_izvodi_from_imap(
    db: Session,
    tenant: Tenant,
    *,
    since: datetime | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    import email
    import imaplib
    from email import policy
    from email.header import decode_header, make_header

    mail = load_mail_settings(tenant)
    if not mail.enabled or not mail.imap_user or not mail.imap_password:
        return {"ok": False, "message": "IMAP nije podešen. Unesi podatke u Podešavanja → Mail ili .env.", "imported": []}

    if since is None:
        try:
            since = datetime.strptime(mail.mail_since_date[:10], "%Y-%m-%d")
        except ValueError:
            since = datetime(2026, 1, 1)

    terms = subject_terms(mail.izvod_subjects)
    since_str = since.strftime("%d-%b-%Y")
    IZVODI_DIR.mkdir(parents=True, exist_ok=True)

    def _decode_subj(raw: str | None) -> str:
        if not raw:
            return ""
        try:
            return str(make_header(decode_header(raw)))
        except Exception:
            return raw or ""

    def _extract_pdfs(raw: bytes) -> list[tuple[str, bytes]]:
        msg = email.message_from_bytes(raw, policy=policy.default)
        out: list[tuple[str, bytes]] = []
        for part in msg.walk():
            fn = part.get_filename() or ""
            try:
                fn = str(make_header(decode_header(fn))) if fn else ""
            except Exception:
                pass
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            ctype = part.get_content_type() or ""
            if fn.lower().endswith(".pdf") or ctype == "application/pdf":
                out.append((fn or "izvod.pdf", bytes(payload)))
        return out

    seen = {
        str(r.message_id)
        for r in db.query(InvoiceMailSeen)
        .filter(InvoiceMailSeen.tenant_id == tenant.id, InvoiceMailSeen.kind == "izvod")
        .all()
    }

    imported: list[dict[str, Any]] = []
    skipped = 0
    found = 0

    imap = imaplib.IMAP4_SSL(mail.imap_host, mail.imap_port)
    try:
        imap.login(mail.imap_user, mail.imap_password)
        imap.select(mail.imap_folder, readonly=True)
        id_set: list[bytes] = []
        seen_ids: set[bytes] = set()
        for term in terms:
            safe = term.replace('"', "")
            typ, data = imap.search(None, f'(SINCE {since_str} SUBJECT "{safe}")')
            if typ != "OK" or not data or not data[0]:
                continue
            for mid in data[0].split():
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    id_set.append(mid)
        if not id_set:
            typ, data = imap.search(None, f"(SINCE {since_str})")
            if typ == "OK" and data and data[0]:
                id_set = data[0].split()

        for num in list(reversed(id_set))[:limit]:
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            raw_b = bytes(raw)
            msg = email.message_from_bytes(raw_b, policy=policy.default)
            subject = _decode_subj(msg.get("Subject"))
            sl = subject.lower()
            if not any(t.lower() in sl for t in terms) and "izvod" not in sl:
                continue
            found += 1
            mid = (msg.get("Message-ID") or msg.get("Message-Id") or f"{subject}|{msg.get('Date')}")[:200]
            if mid in seen:
                skipped += 1
                continue
            day = statement_day_from_subject(subject) or datetime.utcnow().strftime("%Y-%m-%d")
            digest = file_sha256(raw_b)
            existing = (
                db.query(BankStatement)
                .filter(BankStatement.tenant_id == tenant.id, BankStatement.file_hash == digest)
                .first()
            )
            if existing:
                skipped += 1
                db.add(InvoiceMailSeen(tenant_id=tenant.id, message_id=mid, kind="izvod", matched=f"dup:{day}"))
                continue

            pdfs = _extract_pdfs(raw_b)
            path: Path | None = None
            if pdfs:
                path = IZVODI_DIR / f"izvod_{tenant.id}_{day}.pdf"
                path.write_bytes(pdfs[0][1])
            else:
                path = IZVODI_DIR / f"izvod_{tenant.id}_{day}.eml"
                path.write_bytes(raw_b)

            stmt = BankStatement(
                tenant_id=tenant.id,
                statement_day=day,
                file_hash=digest,
                file_path=str(path),
                subject=subject[:500],
                source="mail",
                tx_count=0,
            )
            db.add(stmt)
            db.flush()
            # Jednostavna stavka-placeholder (puni parse kasnije / ručno)
            db.add(
                BankTransaction(
                    tenant_id=tenant.id,
                    statement_id=stmt.id,
                    tx_date=day,
                    amount=Decimal("0"),
                    description=f"Izvod {day} — pregledaj PDF / uvezi detalje",
                    tx_type="other",
                    status="needs_review",
                    raw_text=subject,
                )
            )
            stmt.tx_count = 1
            db.add(InvoiceMailSeen(tenant_id=tenant.id, message_id=mid, kind="izvod", matched=day))
            imported.append({"day": day, "subject": subject, "path": path.name if path else None})
            seen.add(mid)
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "message": f"IMAP greška: {exc}", "imported": []}
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return {
        "ok": True,
        "found": found,
        "imported": imported,
        "skipped": skipped,
        "message": (
            f"Od {since.strftime('%d.%m.%Y')}: pronađeno {found} izvoda, "
            f"novo {len(imported)}, preskočeno {skipped}."
        ),
    }


def customers_with_balance(db: Session, tenant: Tenant) -> list[dict[str, Any]]:
    customers = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.active.is_(True))
        .order_by(Customer.name)
        .all()
    )
    out = []
    for c in customers:
        s = customer_summary(db, tenant, c)
        if s["stavki"] or s["dug"]:
            out.append(s)
    out.sort(key=lambda x: (-abs(x["dug"]), x["naziv"]))
    return out
