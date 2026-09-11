"""EFI v5 šifrarnici i mapiranje — interni ugovor prije Navire.

Izvor: docs/efi/ (Funkcionalna/Tehnička spec v5, Prilog 2 XML).
SOAP/XAdES ostaje Naviri; ovdje su samo polja i pravila.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from typing import Any

from sepko.models import Tenant
from sepko.crypto import decrypt_secret, encrypt_secret

INV_TYPES = (
    "INVOICE",
    "ADVANCE",
    "CREDIT_NOTE",
    "CORRECTIVE",
    "ERROR_CORRECTIVE",
    "SUMMARY",
    "PERIODICAL",
)

# Predračun nije EFI InvType — čuva se interno, ne šalje se na fiskalizaciju.
DOCUMENT_TYPES = INV_TYPES + ("PROFORMA",)

# Redoslijed na formi — nazivi prate Zakon o PDV-u CG (čl.31) + EFI InvType.
UI_DOCUMENT_TYPES = (
    "INVOICE",
    "ADVANCE",
    "CREDIT_NOTE",
    "CORRECTIVE",
    "ERROR_CORRECTIVE",
    "PROFORMA",
    "SUMMARY",
    "PERIODICAL",
)

DOCUMENT_TYPE_LABELS = {
    "INVOICE": "Račun",
    "ADVANCE": "Avansni račun",
    "CREDIT_NOTE": "Knjižno odobrenje",
    "CORRECTIVE": "Korektivni račun",
    "ERROR_CORRECTIVE": "Ispravka greške",
    "PROFORMA": "Predračun",
    "SUMMARY": "Zbirni račun",
    "PERIODICAL": "Periodični račun",
}

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

# Tipovi sa referencom na original (IICRef) — negativni iznosi dozvoljeni.
CREDIT_INV_TYPES = frozenset({"CREDIT_NOTE", "CORRECTIVE", "ERROR_CORRECTIVE"})

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
    """EFI InvType — bez PROFORMA."""
    v = (value or "INVOICE").strip().upper()
    if v not in INV_TYPES:
        raise ValueError(f"inv_type must be one of {INV_TYPES}")
    return v


def normalize_document_type(value: str | None) -> str:
    """Tip dokumenta na formi / u bazi (uključuje Predračun)."""
    v = (value or "INVOICE").strip().upper()
    if v not in DOCUMENT_TYPES:
        raise ValueError(f"document type must be one of {DOCUMENT_TYPES}")
    return v


def document_type_label(code: str | None) -> str:
    c = (code or "INVOICE").strip().upper()
    return DOCUMENT_TYPE_LABELS.get(c, c)


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
    logo_filename: str = ""


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
    default_opening_cash: str = "0.00"
    allow_opening_cash_override: bool = False
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


def format_display_inv_num(
    ord_num: int,
    when: datetime | None = None,
    *,
    series: str = "1",
) -> str:
    """Lokalni prikaz: 1-{mjesec}-{rbr}/{godina} npr. 1-09-002/2026."""
    when = when or datetime.now(timezone.utc)
    return f"{series}-{when.month:02d}-{int(ord_num):03d}/{when.year}"


def parse_display_inv_num(
    text: str, series: str = "1"
) -> tuple[int, int | None, int | None] | None:
    """Parsira lokalni broj → (ord_num, year|None, month|None).

    Podržava:
    - 1-09-002/2026 (trenutna forma)
    - 1-09-002 (bez godine)
    - 1-1-32/2026 (stara forma; month=None)
    - 32/2026
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # 1-09-002/2026 ili 1-9-2/2026
    m = re.match(
        rf"^{re.escape(series)}-(\d{{1,2}})-(\d{{1,7}})/(\d{{4}})$",
        raw,
        re.I,
    )
    if m:
        month = int(m.group(1))
        ord_num = int(m.group(2))
        year = int(m.group(3))
        if 1 <= month <= 12 and ord_num >= 1:
            return ord_num, year, month
    # 1-09-002 bez godine
    m = re.match(
        rf"^{re.escape(series)}-(\d{{1,2}})-(\d{{1,7}})$",
        raw,
        re.I,
    )
    if m:
        month = int(m.group(1))
        ord_num = int(m.group(2))
        if 1 <= month <= 12 and ord_num >= 1:
            return ord_num, None, month
    legacy_series = f"{series}-1"
    m = re.match(
        rf"^{re.escape(legacy_series)}-(\d+)[/.\-](\d{{4}})$",
        raw,
        re.I,
    )
    if m:
        return int(m.group(1)), int(m.group(2)), None
    m = re.match(r"^(\d+)[/.\-](\d{4})$", raw)
    if m:
        return int(m.group(1)), int(m.group(2)), None
    return None


def display_inv_num(invoice: Any, series: str = "1") -> str:
    """Lokalni prikaz: 1-{mjesec}-{rbr}/{godina}. EFI InvNum ostaje u inv_num.

    rbr je mjesečni (local_ord_num), ne godišnji EFI inv_ord_num.
    """
    when = getattr(invoice, "issue_datetime", None)
    local_ord = getattr(invoice, "local_ord_num", None)
    ord_num = local_ord
    raw = getattr(invoice, "inv_num", None) or getattr(invoice, "external_id", None) or ""
    if not ord_num and raw:
        parts = str(raw).split("/")
        if len(parts) >= 3 and parts[1].isdigit():
            if when is None and parts[2].isdigit() and len(parts[2]) == 4:
                when = datetime(int(parts[2]), 1, 1, tzinfo=timezone.utc)
    if not ord_num:
        status = getattr(invoice, "status", None)
        if status in ("draft", "pending"):
            return "Nacrt"
        return "—"
    return format_display_inv_num(int(ord_num), when, series=series)


def qr_base_url(mode: str) -> str:
    return QR_BASE_PROD if mode == "prod" else QR_BASE_TEST


def exempt_from_vat_code(tax_rate_code: str | None) -> str | None:
    """Map Sepko tax_rate_code (EX26…) → EFI ExemptFromVAT (VAT_CL_26).

    PDV0 (nulta stopa čl.25) nije oslobođenje — šalje se samo vr=0 bez ex.
    """
    code = (tax_rate_code or "").strip().upper()
    if code.startswith("EX") and code[2:].isdigit():
        return f"VAT_CL_{int(code[2:])}"
    return None


def build_same_taxes(lines: list[Any]) -> list[dict[str, Any]]:
    """EFI SameTaxes — agregacija po stopi / oslobođenju."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        vr = f"{Decimal(str(line.vat_rate)):.2f}"
        ex = exempt_from_vat_code(getattr(line, "tax_rate_code", None)) or ""
        key = (vr, ex)
        net = (Decimal(str(line.unit_price_net)) * Decimal(str(line.quantity))).quantize(
            Decimal("0.01")
        )
        gross = Decimal(str(line.total_gross)).quantize(Decimal("0.01"))
        vat = (gross - net).quantize(Decimal("0.01"))
        if key not in buckets:
            buckets[key] = {
                "vatRate": vr,
                "numOfItems": 0,
                "priceBeforeVAT": Decimal("0.00"),
                "vatAmt": Decimal("0.00"),
                "priceAfterVAT": Decimal("0.00"),
            }
            if ex:
                buckets[key]["exemptFromVAT"] = ex
        b = buckets[key]
        b["numOfItems"] += 1
        b["priceBeforeVAT"] += net
        b["vatAmt"] += vat
        b["priceAfterVAT"] += gross
    out: list[dict[str, Any]] = []
    for b in buckets.values():
        row = {
            "vatRate": b["vatRate"],
            "numOfItems": b["numOfItems"],
            "priceBeforeVAT": f"{b['priceBeforeVAT']:.2f}",
            "vatAmt": f"{b['vatAmt']:.2f}",
            "priceAfterVAT": f"{b['priceAfterVAT']:.2f}",
        }
        if b.get("exemptFromVAT"):
            row["exemptFromVAT"] = b["exemptFromVAT"]
        out.append(row)
    return out


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
    items: list[dict[str, Any]] = []
    for line in request.lines:
        item: dict[str, Any] = {
            "n": line.name,
            "c": line.code,
            "u": (getattr(line, "unit", None) or "KOM"),
            "q": str(line.quantity),
            "upb": str(line.unit_price_net),
            "vr": str(line.vat_rate),
            "pa": str(line.total_gross),
        }
        ex = exempt_from_vat_code(getattr(line, "tax_rate_code", None))
        if ex:
            item["ex"] = ex
            item["exemptFromVAT"] = ex
        items.append(item)
    payload: dict[str, Any] = {
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
        "sameTaxes": build_same_taxes(list(request.lines)),
        "totPriceWoVAT": str(request.totals.net),
        "totVATAmt": str(request.totals.vat),
        "totPrice": str(request.totals.gross),
        "note": request.notes,
        "iic": iic,
        "iicSignature": iic_signature,
        "mode": tenant.mode,
    }
    iic_ref = (getattr(request, "iic_ref", None) or "").strip()
    if iic_ref:
        payload["iicRef"] = iic_ref
    return payload
