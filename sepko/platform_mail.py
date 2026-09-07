"""Platform mail — Resend (admin računi za licence), SMTP kao rezervu."""
from __future__ import annotations

import base64
import os
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from sepko.brand import MAIL_FROM_NAME
from sepko.finansije import MailSettings, load_mail_settings, send_firm_mail

ROOT = Path(__file__).resolve().parents[1]
RESEND_URL = "https://api.resend.com/emails"
_EMAIL_MAX = 255


def _load_env() -> None:
    load_dotenv(ROOT / ".env")


def resend_api_key() -> str:
    _load_env()
    return (os.getenv("RESEND_API_KEY") or os.getenv("SEPKO_RESEND_API_KEY") or "").strip()


def platform_from_header() -> str:
    """'Ime <email>' ili samo email — za Resend `from`."""
    _load_env()
    raw = (os.getenv("RESEND_FROM") or os.getenv("SEPKO_MAIL_FROM") or "").strip()
    if raw:
        return raw[:320]
    mail = load_mail_settings(None)
    if mail.smtp_from:
        name = (mail.smtp_from_name or MAIL_FROM_NAME).strip() or MAIL_FROM_NAME
        return f"{name} <{mail.smtp_from}>"
    issuer = (os.getenv("SEPKO_BILLING_NAME") or "PROSTUDIO.ME DOO").strip() or "PROSTUDIO.ME DOO"
    addr = (os.getenv("SEPKO_BILLING_EMAIL") or "finansije@prostudio.me").strip() or "finansije@prostudio.me"
    return f"{issuer} <{addr}>"


def parse_from_email(header: str) -> str:
    _name, addr = parseaddr(header or "")
    return (addr or header or "").strip()


def platform_mail_status() -> dict[str, Any]:
    """Da li admin može slati mail (Resend ili SMTP)."""
    key = resend_api_key()
    if key:
        return {"ok": True, "provider": "resend", "message": "Resend je podešen."}
    mail = load_mail_settings(None)
    if mail.smtp_host and mail.smtp_user and mail.smtp_password:
        return {"ok": True, "provider": "smtp", "message": "SMTP je podešen."}
    return {
        "ok": False,
        "provider": None,
        "message": "Podesi RESEND_API_KEY u .env (ili SMTP_HOST / USER / PASSWORD).",
    }


def send_platform_mail(
    *,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Šalje preko Resend-a ako postoji ključ, inače SMTP iz .env."""
    recipients = [a.strip() for a in to_addrs if a and a.strip() and len(a.strip()) <= _EMAIL_MAX]
    if not recipients:
        return {"ok": False, "message": "Nema adrese primaoca.", "provider": None}

    key = resend_api_key()
    if key:
        return _send_resend(
            api_key=key,
            to_addrs=recipients,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            reply_to=reply_to,
        )

    mail: MailSettings = load_mail_settings(None)
    result = send_firm_mail(
        mail=mail,
        to_addrs=recipients,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )
    result["provider"] = "smtp" if result.get("ok") else result.get("provider") or "smtp"
    return result


def _send_resend(
    *,
    api_key: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None,
    attachments: list[tuple[str, bytes, str]] | None,
    reply_to: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": platform_from_header(),
        "to": to_addrs,
        "subject": (subject or "")[:998],
        "text": body_text or "",
    }
    if body_html:
        payload["html"] = body_html
    reply = (reply_to or "").strip()
    if reply:
        payload["reply_to"] = reply[:255]
    atts = []
    for filename, data, mime in attachments or []:
        if not data:
            continue
        atts.append(
            {
                "filename": (filename or "prilog")[:180],
                "content": base64.b64encode(data).decode("ascii"),
                "content_type": (mime or "application/octet-stream")[:120],
            }
        )
    if atts:
        payload["attachments"] = atts

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                RESEND_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        return {"ok": False, "message": "Resend timeout.", "provider": "resend"}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"Resend mrežna greška: {exc}", "provider": "resend"}

    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code >= 400:
        msg = str(body.get("message") or body.get("error") or resp.reason_phrase or f"HTTP {resp.status_code}")
        return {"ok": False, "message": f"Resend: {msg[:500]}", "provider": "resend"}
    email_id = str(body.get("id") or "")
    return {
        "ok": True,
        "message": f"Poslato na {', '.join(to_addrs)}.",
        "provider": "resend",
        "id": email_id,
    }
