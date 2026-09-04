"""EFI v5 šifrarnici i mapiranje — interni ugovor prije Navire.

Izvor: docs/efi/ (Funkcionalna/Tehnička spec v5, Prilog 2 XML).
SOAP/XAdES ostaje Naviri; ovdje su samo polja i pravila.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode
from typing import Any

from sepko.models import Tenant
from sepko.crypto import decrypt_secret, encrypt_secret

INV_TYPES = ("INVOICE", "CORRECTIVE", "SUMMARY", "PERIODICAL", "ADVANCE", "CREDIT_NOTE")

TYPE_OF_INV = ("CASH", "NONCASH")

PAY_METHODS = (
    "BANKNOTE",
    "CARD",
    "BUSINESSCARD",
    "SVOUCHER",
    "COMPANY",
    "ORDER",
    "ADVANCE",
    "ACCOUNT",
    "FACTORING",
    "OTHER",
    "OTHER-CASH",
)

CASH_PAY_METHODS = frozenset({"BANKNOTE", "CARD", "OTHER-CASH"})
NONCASH_PAY_METHODS = frozenset(PAY_METHODS) - CASH_PAY_METHODS

TYPE_OF_INV_ALIASES = {
    "cash": "CASH",
    "CASH": "CASH",
    "non_cash": "NONCASH",
    "noncash": "NONCASH",
    "NONCASH": "NONCASH",
    "non-cash": "NONCASH",
}

PAY_METHOD_ALIASES = {
    "cash": "BANKNOTE",
    "BANKNOTE": "BANKNOTE",
    "card": "CARD",
    "CARD": "CARD",
    "transfer": "ORDER",
    "virman": "ORDER",
    "ORDER": "ORDER",
    "account": "ACCOUNT",
    "ACCOUNT": "ACCOUNT",
    "other": "OTHER",
    "OTHER": "OTHER",
    "businesscard": "BUSINESSCARD",
    "BUSINESSCARD": "BUSINESSCARD",
    "svoucher": "SVOUCHER",
    "SVOUCHER": "SVOUCHER",
    "company": "COMPANY",
    "COMPANY": "COMPANY",
    "advance": "ADVANCE",
    "ADVANCE": "ADVANCE",
    "factoring": "FACTORING",
    "FACTORING": "FACTORING",
    "other-cash": "OTHER-CASH",
    "OTHER-CASH": "OTHER-CASH",
}

QR_BASE_TEST = "https://efitest.tax.gov.me/ic/#/verify"
QR_BASE_PROD = "https://mapr.tax.gov.me/ic/#/verify"

# Token / potpis: PKCS#12 fajl, ili token Telekoma / Pošte Crne Gore
FISCAL_TOKEN_PROVIDERS = ("", "pkcs12", "telekom", "posta")
FISCAL_TOKEN_LABELS = {
    "": "— nije izabrano —",
    "pkcs12": "Certifikat (PKCS#12 / USB)",
    "telekom": "Crnogorski Telekom (token)",
    "posta": "Pošta Crne Gore (token)",
}


@dataclass
class TenantFiscal:
    busin_unit_code: str = ""
    tcr_code: str = ""
    soft_code: str = ""
    operator_code: str = ""
    is_issuer_in_vat: bool = True
    # Aktivan kanal potpisa / partner tokena
    token_provider: str = ""
    # Enkriptovani tokeni (Fernet); u UI se ne prikazuju u plainu
    telekom_token: str = ""
    posta_token: str = ""

    def as_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def has_telekom_token(self) -> bool:
        return bool((self.telekom_token or "").strip())

    def has_posta_token(self) -> bool:
        return bool((self.posta_token or "").strip())

    def active_token_plain(self) -> str:
        """Plain token aktivnog providera (Telekom/Pošta); inače prazno."""
        if self.token_provider == "telekom":
            return decrypt_secret(self.telekom_token)
        if self.token_provider == "posta":
            return decrypt_secret(self.posta_token)
        return ""


def normalize_type_of_inv(value: str) -> str:
    key = (value or "").strip()
    mapped = TYPE_OF_INV_ALIASES.get(key) or TYPE_OF_INV_ALIASES.get(key.upper())
    if mapped:
        return mapped
    raise ValueError(f"invoice_type must be CASH or NONCASH (got {value!r})")


def normalize_pay_method(value: str) -> str:
    key = (value or "").strip()
    mapped = PAY_METHOD_ALIASES.get(key) or PAY_METHOD_ALIASES.get(key.upper())
    if mapped:
        return mapped
    raise ValueError(f"payment_method must be an EFI PayMethod (got {value!r})")


def normalize_inv_type(value: str | None) -> str:
    v = (value or "INVOICE").strip().upper()
    if v not in INV_TYPES:
        raise ValueError(f"inv_type must be one of {INV_TYPES}")
    return v


def _settings_dict(tenant: Tenant) -> dict[str, Any]:
    raw = tenant.settings_json
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_settings(tenant: Tenant, data: dict[str, Any]) -> None:
    tenant.settings_json = json.dumps(data, ensure_ascii=False)


def load_tenant_fiscal(tenant: Tenant) -> TenantFiscal:
    data = _settings_dict(tenant)
    provider = str(data.get("token_provider") or "").strip().lower()
    if provider not in FISCAL_TOKEN_PROVIDERS:
        provider = ""
    return TenantFiscal(
        busin_unit_code=str(data.get("busin_unit_code") or ""),
        tcr_code=str(data.get("tcr_code") or ""),
        soft_code=str(data.get("soft_code") or ""),
        operator_code=str(data.get("operator_code") or ""),
        is_issuer_in_vat=bool(data.get("is_issuer_in_vat", True)),
        token_provider=provider,
        telekom_token=str(data.get("telekom_token") or ""),
        posta_token=str(data.get("posta_token") or ""),
    )


def save_tenant_fiscal(
    tenant: Tenant,
    fiscal: TenantFiscal,
    *,
    telekom_token_new: str | None = None,
    posta_token_new: str | None = None,
    clear_telekom_token: bool = False,
    clear_posta_token: bool = False,
) -> None:
    """Čuva EFI kodove + tokene. Prazan novi token = zadrži postojeći."""
    data = _settings_dict(tenant)
    provider = (fiscal.token_provider or "").strip().lower()
    if provider not in FISCAL_TOKEN_PROVIDERS:
        provider = ""

    telekom = str(data.get("telekom_token") or "")
    posta = str(data.get("posta_token") or "")
    if clear_telekom_token:
        telekom = ""
    elif telekom_token_new is not None and telekom_token_new.strip():
        telekom = encrypt_secret(telekom_token_new.strip())
    if clear_posta_token:
        posta = ""
    elif posta_token_new is not None and posta_token_new.strip():
        posta = encrypt_secret(posta_token_new.strip())

    data.update(
        {
            "busin_unit_code": fiscal.busin_unit_code,
            "tcr_code": fiscal.tcr_code,
            "soft_code": fiscal.soft_code,
            "operator_code": fiscal.operator_code,
            "is_issuer_in_vat": fiscal.is_issuer_in_vat,
            "token_provider": provider,
            "telekom_token": telekom,
            "posta_token": posta,
        }
    )
    _write_settings(tenant, data)


@dataclass
class TenantCompany:
    """Podaci firme za A4 račun (VG-like zaglavlje)."""

    address: str = ""
    address2: str = ""
    pdv_number: str = ""
    bank_account: str = ""
    website: str = ""


def load_tenant_company(tenant: Tenant) -> TenantCompany:
    data = _settings_dict(tenant).get("company") or {}
    if not isinstance(data, dict):
        data = {}
    defaults = asdict(TenantCompany())
    defaults.update({k: str(data[k]) for k in defaults if k in data and data[k] is not None})
    return TenantCompany(**defaults)


def save_tenant_company(tenant: Tenant, company: TenantCompany) -> None:
    data = _settings_dict(tenant)
    data["company"] = asdict(company)
    _write_settings(tenant, data)


@dataclass
class TenantUiSettings:
    """Osnovna podešavanja + štampa (VG-like), u settings_json pored EFI kodova."""

    language: str = "cnr"
    auto_login: bool = False
    max_invoice_amount: str = "1000000.00"
    pin_length: int = 4
    pin_only_login: bool = False
    a4_item_name_own_line: bool = False
    a4_signature_lines: bool = True
    qty_no_decimals: bool = False
    # štampa
    print_enabled: bool = True
    printer_number: str = "1"
    printer_name: str = ""
    printer_type: str = ""
    receipt_width_px: str = "576"
    text_size: str = "12"
    qr_width_px: str = "180"
    blank_lines_start: str = "0"
    blank_lines_end: str = "2"
    max_print_height: str = ""
    paper_cut: str = "partial"
    open_drawer: str = "never"
    print_pause_sec: str = "0"
    print_agent_url: str = "ws://127.0.0.1:17890/ws"


# VG eFiskal: CG lat/ćir, SR lat/ćir, AL, TR, RU, UK, EN
UI_LANGUAGES: list[tuple[str, str]] = [
    ("cnr", "Crnogorski (latinica)"),
    ("cnr-cyrl", "Crnogorski (ćirilica)"),
    ("sr", "Srpski (latinica)"),
    ("sr-cyrl", "Srpski (ćirilica)"),
    ("sq", "Albanski"),
    ("tr", "Turski"),
    ("ru", "Ruski"),
    ("uk", "Ukrajinski"),
    ("en", "English"),
]
UI_LANGUAGE_CODES = frozenset(code for code, _ in UI_LANGUAGES)


def normalize_ui_language(code: str | None) -> str:
    c = (code or "").strip()
    return c if c in UI_LANGUAGE_CODES else "cnr"


def load_tenant_ui(tenant: Tenant) -> TenantUiSettings:
    data = _settings_dict(tenant).get("ui") or {}
    if not isinstance(data, dict):
        data = {}
    defaults = asdict(TenantUiSettings())
    defaults.update({k: data[k] for k in defaults if k in data})
    return TenantUiSettings(**defaults)


def save_tenant_ui(tenant: Tenant, ui: TenantUiSettings) -> None:
    data = _settings_dict(tenant)
    data["ui"] = asdict(ui)
    _write_settings(tenant, data)


def build_inv_num(busin_unit_code: str, ord_num: int, year: int, tcr_code: str) -> str:
    bu = busin_unit_code or "bu00000000"
    tcr = tcr_code or "cr00000000"
    return f"{bu}/{ord_num}/{year}/{tcr}"


def parse_display_inv_num(text: str, series: str = "1-1") -> tuple[int, int] | None:
    """Parsira lokalni broj 1-1-{rbr}/{godina} → (ord_num, year)."""
    raw = (text or "").strip()
    if not raw:
        return None
    # 1-1-32/2026 ili 1-1-32-2026
    m = re.match(
        rf"^{re.escape(series)}-(\d+)[/.\-](\d{{4}})$",
        raw,
        re.I,
    )
    if m:
        return int(m.group(1)), int(m.group(2))
    # samo 32/2026
    m = re.match(r"^(\d+)[/.\-](\d{4})$", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def display_inv_num(invoice: Any, series: str = "1-1") -> str:
    """Lokalni VG-stil prikaz: 1-1-{rbr}/{godina}. EFI InvNum ostaje u inv_num."""
    ord_num = getattr(invoice, "inv_ord_num", None)
    when = getattr(invoice, "issue_datetime", None)
    year = when.year if when is not None else None
    raw = getattr(invoice, "inv_num", None) or getattr(invoice, "external_id", None) or ""
    if not ord_num and raw:
        parts = str(raw).split("/")
        if len(parts) >= 3 and parts[1].isdigit():
            ord_num = int(parts[1])
            if year is None and parts[2].isdigit() and len(parts[2]) == 4:
                year = int(parts[2])
    if year is None:
        year = datetime.now().year
    if not ord_num:
        status = getattr(invoice, "status", None)
        if status in ("draft", "pending"):
            return "Nacrt"
        return "—"
    return f"{series}-{ord_num}/{year}"


def qr_base_url(mode: str) -> str:
    return QR_BASE_PROD if mode == "prod" else QR_BASE_TEST


def build_qr_url(
    *,
    mode: str,
    iic: str,
    tin: str,
    issue_datetime: datetime,
    ord_num: int,
    busin_unit_code: str,
    tcr_code: str,
    soft_code: str,
    price: Decimal,
) -> str:
    crtd = issue_datetime.isoformat(timespec="seconds")
    query = urlencode(
        {
            "iic": iic,
            "tin": tin,
            "crtd": crtd,
            "ord": str(ord_num),
            "bu": busin_unit_code or "bu00000000",
            "cr": tcr_code or "cr00000000",
            "sw": soft_code or "sw00000000",
            "prc": f"{price:.2f}",
        }
    )
    return f"{qr_base_url(mode)}?{query}"


def to_navira_payload(
    tenant: Tenant,
    request: Any,
    fiscal: TenantFiscal,
    *,
    inv_num: str,
    inv_ord_num: int,
    iic: str | None = None,
    iic_signature: str | None = None,
) -> dict[str, Any]:
    """EFI-oblikovan JSON koji Navira adapter kasnije šalje 1:1 (imena polja kao RegisterInvoice)."""
    buyer = None
    if request.buyer:
        buyer = {
            "idType": "TIN",
            "idNum": request.buyer.pib,
            "name": request.buyer.name,
            "address": request.buyer.address,
        }
    items = [
        {
            "n": line.name,
            "c": line.code,
            "u": "KOM",
            "q": str(line.quantity),
            "upb": str(line.unit_price_net),
            "vr": str(line.vat_rate),
            "pa": str(line.total_gross),
        }
        for line in request.lines
    ]
    return {
        "tin": tenant.pib,
        "businUnitCode": fiscal.busin_unit_code,
        "tcrCode": fiscal.tcr_code,
        "softCode": fiscal.soft_code,
        "operatorCode": fiscal.operator_code,
        "isIssuerInVAT": fiscal.is_issuer_in_vat,
        "tokenProvider": fiscal.token_provider or None,
        "hasProviderToken": bool(fiscal.active_token_plain()),
        "invType": request.inv_type,
        "typeOfInv": request.invoice_type,
        "invNum": inv_num,
        "invOrdNum": inv_ord_num,
        "issueDateTime": request.issue_datetime.isoformat(timespec="seconds"),
        "currency": request.currency,
        "payMethods": [{"type": request.payment_method, "amt": str(request.totals.gross)}],
        "buyer": buyer,
        "items": items,
        "totPriceWoVAT": str(request.totals.net),
        "totVATAmt": str(request.totals.vat),
        "totPrice": str(request.totals.gross),
        "note": request.notes,
        "iic": iic,
        "iicSignature": iic_signature,
        "mode": tenant.mode,
    }
