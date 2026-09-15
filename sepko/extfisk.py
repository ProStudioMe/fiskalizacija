"""EXTFISK HTTP XML — RegisterInvoiceRequest (docs/efi/EXTFISK.md)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import logging
import time
import xml.etree.ElementTree as ET

import httpx

from sepko.brand import USER_AGENT
from sepko.config import Settings
from sepko.efi import (
    build_qr_url,
    build_same_taxes,
    exempt_from_vat_code,
    load_tenant_company,
)
from sepko.models import Tenant
from sepko.partner import (
    DepositPartnerResult,
    PartnerAdapter,
    PartnerResult,
    _fiscal_context,
    _totals_gross_allowed,
)
from sepko.schemas import CashDepositRequest, FiscalizeRequest
from sepko.signing import SigningError, build_iic_plain, load_tenant_pkcs12, sign_iic

log = logging.getLogger("sepko.extfisk")

DEFAULT_URL = "http://62.4.59.86:3366/api/extfisk"
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = (0.4, 0.8, 1.6)

_INV_TYPE_XML = {
    "INVOICE": "RACUN",
    "CREDIT_NOTE": "POVRAT",
}

# EXTFISK PayMethod/Type (nema ORDER — to je EFI)
_PAY_TYPE = {
    "BANKNOTE": "BANKNOTE",
    "CARD": "CARD",
    "ACCOUNT": "ACCOUNT",
    "ORDER": "ACCOUNT",
    "BUSINESSCARD": "BUSINESSCARD",
    "OTHER": "OTHER",
    "ADVANCE": "ADVANCE",
    "SVOUCHER": "SVOUCHER",
    "COMPANY": "OTHER",
    "FACTORING": "OTHER",
    "OTHER-CASH": "OTHER",
}

_COUNTRY_TOKENS = frozenset({"crna gora", "montenegro", "mne", "cg", "me"})


def _q(value: Decimal | int | str, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    d = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return f"{d:.{places}f}"


def _qty(value: Decimal | int | str) -> str:
    d = Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = f"{d:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _text(parent: ET.Element, tag: str, value: str | None = "") -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = "" if value is None else str(value)
    return el


def split_street_town(address: str, town_hint: str = "") -> tuple[str, str]:
    hint = (town_hint or "").strip()
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if parts and parts[-1].lower() in _COUNTRY_TOKENS:
        parts = parts[:-1]
    if hint:
        street = ", ".join(p for p in parts if p.lower() != hint.lower()) or (address or "")
        return street, hint
    if len(parts) >= 2:
        return ", ".join(parts[:-1]), parts[-1]
    return (address or "").strip(), hint


def extfisk_pay_type(payment_method: str) -> str:
    key = (payment_method or "").strip().upper()
    return _PAY_TYPE.get(key, "OTHER")


def extfisk_inv_type(inv_type: str) -> str:
    return _INV_TYPE_XML.get((inv_type or "INVOICE").strip().upper(), "RACUN")


def extfisk_environment(settings: Settings, tenant: Tenant) -> str:
    env = (settings.extfisk_environment or "").strip().upper()
    if env in ("TEST", "PROD"):
        return env
    return "PROD" if (tenant.mode or "").lower() == "prod" else "TEST"


def extfisk_request_id(tenant: Tenant, inv_num: str) -> str:
    """Stabilan RequestId — isti račun / retry vraća isti JIKR."""
    digest = hashlib.sha256(f"{tenant.pib}:{inv_num}".encode("utf-8")).hexdigest()
    return str(int(digest[:15], 16))


def _buyer_type(request: FiscalizeRequest) -> str:
    buyer = request.buyer
    if not buyer:
        return "NEMA"
    pib = (buyer.pib or "").strip()
    name = (buyer.name or "").strip()
    if pib:
        return "PRAVNO"
    if name:
        return "FIZICKO"
    return "NEMA"


def build_register_invoice_xml(
    tenant: Tenant,
    request: FiscalizeRequest,
    *,
    api_key: str,
    environment: str,
    request_id: str,
    inv_num: str,
    inv_ord_num: int,
    fiscal_busin_unit: str,
    fiscal_operator: str,
    fiscal_tcr: str,
) -> str:
    company = load_tenant_company(tenant)
    street, town = split_street_town(company.address, company.address2)

    root = ET.Element("RegisterInvoiceRequest")
    _text(root, "ApiKey", api_key)
    _text(root, "Environment", environment)
    _text(root, "RequestId", request_id)
    _text(root, "InvType", extfisk_inv_type(request.inv_type))

    seller = ET.SubElement(root, "Seller")
    _text(seller, "Name", tenant.name or "")
    _text(seller, "IDNum", tenant.pib or "")
    _text(seller, "Address", street)
    _text(seller, "Town", town)

    buyer_el = ET.SubElement(root, "Buyer")
    btype = _buyer_type(request)
    _text(buyer_el, "BuyerType", btype)
    buyer = request.buyer
    b_street, b_town = split_street_town((buyer.address if buyer else "") or "")
    _text(buyer_el, "Name", (buyer.name if buyer else "") or "")
    _text(buyer_el, "IDNum", (buyer.pib if buyer else "") or "")
    _text(buyer_el, "Address", b_street)
    _text(buyer_el, "Town", b_town)
    _text(buyer_el, "Country", "MNE")
    _text(buyer_el, "IDType", "TIN")
    _text(buyer_el, "TIC", "")

    invoice = ET.SubElement(root, "Invoice")
    _text(invoice, "InvNum", inv_num)
    _text(invoice, "InvOrdNum", str(inv_ord_num))
    _text(invoice, "IssueDateTime", request.issue_datetime.isoformat(timespec="seconds"))
    deadline = (request.issue_datetime + timedelta(days=15)).date().isoformat()
    _text(invoice, "PayDeadline", deadline)
    _text(invoice, "TypeOfInv", request.invoice_type)
    _text(invoice, "BusinessUnitCode", fiscal_busin_unit)
    _text(invoice, "OperatorCode", fiscal_operator)
    _text(invoice, "TCRCode", fiscal_tcr)
    _text(invoice, "TaxPeriod", request.issue_datetime.strftime("%m/%Y"))
    _text(invoice, "TotPriceWoVAT", _q(request.totals.net, 2))
    _text(invoice, "TotVATAmt", _q(request.totals.vat, 2))
    _text(invoice, "TotPrice", _q(request.totals.gross, 2))

    items_el = ET.SubElement(invoice, "Items")
    for idx, line in enumerate(request.lines, start=1):
        qty = Decimal(str(line.quantity))
        upb = Decimal(str(line.unit_price_net))
        gross = Decimal(str(line.total_gross))
        pb = (upb * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pa = pb
        va = (gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) - pa).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        item = ET.SubElement(items_el, "I")
        _text(item, "Rbs", str(idx))
        _text(item, "C", line.code)
        _text(item, "N", line.name)
        _text(item, "Q", _qty(qty))
        _text(item, "U", line.unit or "KOM")
        _text(item, "UPB", _q(upb, 4))
        _text(item, "UPA", _q(upb, 4))
        _text(item, "R", _q(Decimal("0"), 4))
        _text(item, "RR", "false")
        _text(item, "PB", _q(pb, 2))
        _text(item, "PA", _q(pa, 2))
        _text(item, "VA", _q(va, 2))
        _text(item, "VR", _q(line.vat_rate, 4))
        _text(item, "EX", exempt_from_vat_code(line.tax_rate_code) or "")

    pays = ET.SubElement(invoice, "PayMethods")
    pay = ET.SubElement(pays, "PayMethod")
    _text(pay, "Amt", _q(request.totals.gross, 2))
    _text(pay, "Type", extfisk_pay_type(request.payment_method))
    _text(pay, "AdvIIC", "")

    taxes = ET.SubElement(invoice, "SameTaxes")
    for row in build_same_taxes(list(request.lines)):
        st = ET.SubElement(taxes, "SameTax")
        _text(st, "NumOfItems", str(row["numOfItems"]))
        _text(st, "PriceBefVAT", row["priceBeforeVAT"])
        _text(st, "VATRate", row["vatRate"])
        _text(st, "VATAmt", row["vatAmt"])

    xml_body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>' + xml_body


def _pick(data: dict, *keys: str) -> str | None:
    for k in keys:
        v = data.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _local_iic(
    tenant: Tenant,
    request: FiscalizeRequest,
    *,
    inv_num: str,
    fiscal,
    reuse_iic: str | None,
    reuse_iic_signature: str | None,
) -> tuple[str | None, str | None]:
    iic_sig = reuse_iic_signature
    iic = (reuse_iic or "").strip().upper() or None
    if iic:
        return iic, iic_sig
    try:
        material = load_tenant_pkcs12(tenant.slug)
    except SigningError:
        material = None
    if not material:
        seed = f"{tenant.slug}:{inv_num}:{request.totals.gross}"
        digest = hashlib.sha256(seed.encode()).hexdigest()
        return digest[:32].upper(), None
    plain = build_iic_plain(
        tin=tenant.pib,
        issue_datetime=request.issue_datetime,
        inv_num=inv_num,
        busin_unit_code=fiscal.busin_unit_code,
        tcr_code=fiscal.tcr_code,
        soft_code=fiscal.soft_code,
        tot_price=request.totals.gross,
    )
    return sign_iic(material, plain)


def _post_xml(
    url: str,
    xml: str,
    *,
    timeout: float = 45.0,
) -> tuple[bool, dict | None, str | None, int | None]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/xml",
        "User-Agent": USER_AGENT,
    }
    last_err: str | None = None
    last_status: int | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, content=xml.encode("utf-8"), headers=headers)
            last_status = resp.status_code
            try:
                body = resp.json() if resp.content else {}
            except Exception:
                body = {"raw": (resp.text or "")[:2000]}
            if resp.status_code >= 500:
                last_err = (
                    f"EXTFISK HTTP {resp.status_code}: "
                    f"{_pick(body, 'poruka', 'message', 'error', 'detail') or resp.reason_phrase}"
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_S[attempt])
                    continue
                return False, body if isinstance(body, dict) else None, last_err, last_status
            if resp.status_code >= 400:
                msg = _pick(body, "poruka", "message", "error", "detail") or f"HTTP {resp.status_code}"
                return False, body if isinstance(body, dict) else None, f"EXTFISK: {msg}", last_status
            return True, body if isinstance(body, dict) else {}, None, last_status
        except httpx.TimeoutException:
            last_err = "EXTFISK timeout"
        except httpx.HTTPError as exc:
            last_err = f"EXTFISK network error: {exc}"
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF_S[attempt])
    return False, None, last_err or "EXTFISK request failed", last_status


class ExtfiskPartnerAdapter(PartnerAdapter):
    """POST RegisterInvoiceRequest XML; JIKR='TEST' je uspjeh dok PU putanja nije spremna."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def fiscalize(
        self,
        tenant: Tenant,
        request: FiscalizeRequest,
        *,
        inv_ord_num: int,
        reuse_iic: str | None = None,
        reuse_iic_signature: str | None = None,
    ) -> PartnerResult:
        if not _totals_gross_allowed(request):
            return PartnerResult(ok=False, error_message="totals.gross must be non-zero")

        fiscal, inv_num = _fiscal_context(tenant, request, inv_ord_num)
        iic, iic_sig = _local_iic(
            tenant,
            request,
            inv_num=inv_num,
            fiscal=fiscal,
            reuse_iic=reuse_iic,
            reuse_iic_signature=reuse_iic_signature,
        )
        # ApiKey po firmi (PIB); env je samo fallback za single-tenant / lokalni test
        api_key = (fiscal.extfisk_api_key_plain() or self.settings.extfisk_api_key or "").strip()
        url = (self.settings.extfisk_url or DEFAULT_URL).strip() or DEFAULT_URL
        request_id = extfisk_request_id(tenant, inv_num)
        environment = extfisk_environment(self.settings, tenant)
        xml = build_register_invoice_xml(
            tenant,
            request,
            api_key=api_key,
            environment=environment,
            request_id=request_id,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            fiscal_busin_unit=fiscal.busin_unit_code,
            fiscal_operator=fiscal.operator_code,
            fiscal_tcr=fiscal.tcr_code,
        )
        meta = {
            "extfiskXml": xml,
            "requestId": request_id,
            "environment": environment,
            "url": url,
        }

        def _offline(msg: str) -> PartnerResult:
            if not iic:
                return PartnerResult(
                    ok=False,
                    inv_num=inv_num,
                    inv_ord_num=inv_ord_num,
                    navira_payload=meta,
                    error_message=msg,
                )
            qr_url = build_qr_url(
                mode=tenant.mode,
                iic=iic,
                tin=tenant.pib,
                issue_datetime=request.issue_datetime,
                ord_num=inv_ord_num,
                busin_unit_code=fiscal.busin_unit_code,
                tcr_code=fiscal.tcr_code,
                soft_code=fiscal.soft_code,
                price=request.totals.gross,
            )
            return PartnerResult(
                ok=False,
                offline=True,
                ikof=iic,
                iic_signature=iic_sig,
                qr_url=qr_url,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=meta,
                error_message=msg
                or "Offline: EXTFISK nedostupan — JIKR u roku od 48h (isti IKOF).",
            )

        if not api_key:
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=meta,
                error_message="EXTFISK ApiKey nije podešen za ovu firmu (Admin → EFI / EXTFISK)",
            )

        http_ok, body, err, status = _post_xml(url, xml)
        if not http_ok:
            log.warning("EXTFISK fiscalize failed status=%s err=%s", status, err)
            if status is None or (status is not None and status >= 500):
                return _offline(
                    err or "Offline: EXTFISK nedostupan — JIKR u roku od 48h (isti IKOF)."
                )
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                ikof=iic,
                iic_signature=iic_sig,
                navira_payload=meta,
                error_message=err or "EXTFISK fiscalize failed",
            )

        data = body or {}
        biz = str(data.get("status") or "").strip().upper()
        poruka = _pick(data, "poruka", "message", "error", "detail")
        if biz and biz != "OK":
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                ikof=iic,
                iic_signature=iic_sig,
                navira_payload={**meta, "response": data},
                error_message=f"EXTFISK: {poruka or biz}",
            )

        # TEST je važeći JIKR dok drugi dio (putanja ka PU) nije spreman.
        jikr = _pick(data, "jikr", "JIKR", "fic", "FIC") or "TEST"
        qr_url = _pick(data, "qrUrl", "qr_url", "QRUrl", "verificationUrl")
        if not qr_url and iic:
            qr_url = build_qr_url(
                mode=tenant.mode,
                iic=iic,
                tin=tenant.pib,
                issue_datetime=request.issue_datetime,
                ord_num=inv_ord_num,
                busin_unit_code=fiscal.busin_unit_code,
                tcr_code=fiscal.tcr_code,
                soft_code=fiscal.soft_code,
                price=request.totals.gross,
            )
        partner_ref = (
            _pick(data, "idReq", "idDok", "requestId", "partnerRef") or request_id
        )
        return PartnerResult(
            ok=True,
            ikof=iic,
            jikr=jikr,
            qr_url=qr_url,
            partner_ref=str(partner_ref),
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            navira_payload={**meta, "response": data},
            iic_signature=iic_sig,
        )

    def register_cash_deposit(
        self,
        tenant: Tenant,
        request: CashDepositRequest,
        *,
        change_datetime: datetime,
    ) -> DepositPartnerResult:
        return DepositPartnerResult(
            ok=False,
            error_message="EXTFISK ne podržava RegisterCashDeposit",
        )
