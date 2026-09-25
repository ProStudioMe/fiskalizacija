"""Partner adapter — samo Poreska uprava CIS SOAP."""
from __future__ import annotations

from datetime import datetime
import logging
import time

from lxml import etree
import httpx

from sepko.brand import USER_AGENT
from sepko.config import Settings
from sepko.efi import build_qr_url, to_navira_payload
from sepko.models import Tenant
from sepko.partner import (
    DepositPartnerResult,
    PartnerAdapter,
    PartnerResult,
    _fiscal_context,
    _totals_gross_allowed,
)
from sepko.pu.iic import build_pu_iic_plain, require_tenant_cert, sign_pu_iic
from sepko.pu.invoice import build_register_invoice_request, wrap_soap
from sepko.pu.xmldsig import sign_enveloped
from sepko.schemas import CashDepositRequest, FiscalizeRequest
from sepko.signing import SigningError

log = logging.getLogger("sepko.pu")

_MAX_RETRIES = 3
_RETRY_BACKOFF_S = (0.4, 0.8, 1.6)


def cis_service_url(settings: Settings, tenant: Tenant) -> str:
    if (tenant.mode or "").lower() == "prod":
        return (settings.cis_prod_url or "").rstrip("/")
    return (settings.cis_test_url or "").rstrip("/")


def _pick_xml_text(root: etree._Element, *local_names: str) -> str | None:
    wanted = {n.lower() for n in local_names}
    for el in root.iter():
        tag = etree.QName(el).localname
        if tag.lower() in wanted and (el.text or "").strip():
            return el.text.strip()
    return None


def _post_soap(
    url: str,
    xml: str,
    *,
    timeout: float = 45.0,
) -> tuple[bool, etree._Element | None, str | None, int | None]:
    headers = {
        "Accept": "text/xml, application/xml, application/soap+xml",
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "",
        "User-Agent": USER_AGENT,
    }
    last_err: str | None = None
    last_status: int | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, content=xml.encode("utf-8"), headers=headers)
            last_status = resp.status_code
            text = resp.text or ""
            try:
                root = etree.fromstring(text.encode("utf-8")) if text.strip() else None
            except etree.XMLSyntaxError:
                root = None
            if resp.status_code >= 500:
                last_err = f"CIS HTTP {resp.status_code}: {text[:400] or resp.reason_phrase}"
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_S[attempt])
                    continue
                return False, root, last_err, last_status
            if resp.status_code >= 400:
                fault = _pick_xml_text(root, "faultstring", "Fault", "message") if root is not None else None
                return False, root, fault or f"CIS HTTP {resp.status_code}", last_status
            return True, root, None, last_status
        except httpx.TimeoutException:
            last_err = "CIS timeout"
        except httpx.HTTPError as exc:
            last_err = f"CIS network error: {exc}"
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF_S[attempt])
    return False, None, last_err or "CIS request failed", last_status


class PuPartnerAdapter(PartnerAdapter):
    """Direktni CIS prema Poreskoj — IKOF + XMLDSig + SOAP. Nije Navira ni EXTFISK."""

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
        try:
            material = require_tenant_cert(tenant.slug)
        except SigningError as exc:
            return PartnerResult(ok=False, error_message=str(exc))

        iic = (reuse_iic or "").strip().upper() or None
        iic_sig = (reuse_iic_signature or "").strip() or None
        if not iic or not iic_sig:
            plain = build_pu_iic_plain(
                tin=tenant.pib,
                issue_datetime=request.issue_datetime,
                inv_ord_num=inv_ord_num,
                busin_unit_code=fiscal.busin_unit_code,
                tcr_code=fiscal.tcr_code,
                soft_code=fiscal.soft_code,
                tot_price=request.totals.gross,
            )
            iic, iic_sig = sign_pu_iic(material, plain)

        payload = to_navira_payload(
            tenant,
            request,
            fiscal,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            iic=iic,
            iic_signature=iic_sig,
        )
        payload["channel"] = "poreska"

        def _offline(msg: str) -> PartnerResult:
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
                navira_payload=payload,
                error_message=msg
                or "Offline: CIS nedostupan — JIKR u roku od 48h (isti IKOF).",
            )

        url = cis_service_url(self.settings, tenant)
        if not url:
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=payload,
                error_message="CIS URL nije podešen (SEPKO_CIS_TEST_URL / SEPKO_CIS_PROD_URL)",
            )

        req_el = build_register_invoice_request(
            tenant,
            request,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            fiscal=fiscal,
            iic=iic,
            iic_signature=iic_sig,
        )
        sign_enveloped(req_el, material)
        soap = wrap_soap(req_el)
        payload["cisSoap"] = soap[:4000]
        ok, root, err, status = _post_soap(url, soap)
        if not ok:
            log.warning("PU CIS fiscalize failed status=%s err=%s", status, err)
            if status is None or (status is not None and status >= 500):
                return _offline(err or "Offline: CIS nedostupan — JIKR u roku od 48h (isti IKOF).")
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                ikof=iic,
                iic_signature=iic_sig,
                navira_payload=payload,
                error_message=err or "CIS fiscalize failed",
            )

        fault = _pick_xml_text(root, "faultstring") if root is not None else None
        if fault:
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                ikof=iic,
                iic_signature=iic_sig,
                navira_payload=payload,
                error_message=f"CIS: {fault}",
            )
        jikr = _pick_xml_text(root, "FIC", "fic", "JIKR", "jikr") if root is not None else None
        if not jikr:
            return _offline("Offline: CIS nije vratio JIKR/FIC — ponovi u roku od 48h.")
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
            ok=True,
            ikof=iic,
            jikr=jikr,
            qr_url=qr_url,
            partner_ref=_pick_xml_text(root, "UUID") if root is not None else None,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            navira_payload=payload,
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
            error_message="CIS RegisterCashDeposit još nije uključen na PU kanalu",
        )
