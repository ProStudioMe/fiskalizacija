from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import logging
import time
import uuid

import httpx

from sepko.brand import USER_AGENT
from sepko.config import Settings, get_settings
from sepko.efi import (
    TenantFiscal,
    build_inv_num,
    build_qr_url,
    load_tenant_fiscal,
    to_navira_payload,
)
from sepko.models import Tenant
from sepko.schemas import CashDepositRequest, FiscalizeRequest
from sepko.signing import SigningError, build_iic_plain, load_tenant_pkcs12, sign_iic

log = logging.getLogger("sepko.partner")

_MAX_RETRIES = 3
_RETRY_BACKOFF_S = (0.4, 0.8, 1.6)


@dataclass
class PartnerResult:
    ok: bool
    ikof: str | None = None
    jikr: str | None = None
    qr_url: str | None = None
    partner_ref: str | None = None
    inv_num: str | None = None
    inv_ord_num: int | None = None
    error_message: str | None = None
    navira_payload: dict | None = None


@dataclass
class DepositPartnerResult:
    ok: bool
    partner_ref: str | None = None
    error_message: str | None = None
    navira_payload: dict | None = None


class PartnerAdapter:
    def fiscalize(
        self,
        tenant: Tenant,
        request: FiscalizeRequest,
        *,
        inv_ord_num: int,
    ) -> PartnerResult:
        raise NotImplementedError

    def register_cash_deposit(
        self,
        tenant: Tenant,
        request: CashDepositRequest,
        *,
        change_datetime: datetime,
    ) -> DepositPartnerResult:
        raise NotImplementedError


def _fiscal_context(tenant: Tenant, request: FiscalizeRequest, inv_ord_num: int) -> tuple[TenantFiscal, str]:
    fiscal = load_tenant_fiscal(tenant)
    year = request.issue_datetime.year
    inv_num = build_inv_num(fiscal.busin_unit_code, inv_ord_num, year, fiscal.tcr_code)
    return fiscal, inv_num


def _deposit_payload(
    tenant: Tenant,
    fiscal: TenantFiscal,
    request: CashDepositRequest,
    change_datetime: datetime,
) -> dict:
    return {
        "tin": tenant.pib,
        "tcrCode": fiscal.tcr_code,
        "operatorCode": fiscal.operator_code,
        "operation": request.operation,
        "cashAmt": str(request.amount),
        "changeDateTime": change_datetime.isoformat(timespec="seconds"),
        "mode": tenant.mode,
        "tokenProvider": fiscal.token_provider or None,
        "hasProviderToken": bool(fiscal.active_token_plain()),
    }


def _api_key(settings: Settings) -> str:
    return (settings.navira_api_key or settings.partner_api_key or "").strip()


def _base_url(settings: Settings) -> str:
    return (settings.partner_base_url or settings.navira_base_url or "").rstrip("/")


def _auth_headers(settings: Settings) -> dict[str, str]:
    key = _api_key(settings)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-Api-Key"] = key
    return headers


def _pick(data: dict, *keys: str) -> str | None:
    for k in keys:
        v = data.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _http_post_json(
    settings: Settings,
    path: str,
    payload: dict,
    *,
    timeout: float = 45.0,
) -> tuple[bool, dict | None, str | None, int | None]:
    """POST JSON with retries on 5xx / network errors. Returns (ok, body, error, status)."""
    base = _base_url(settings)
    if not base:
        return False, None, "Navira base URL not configured", None
    url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    last_err: str | None = None
    last_status: int | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=_auth_headers(settings))
            last_status = resp.status_code
            try:
                body = resp.json() if resp.content else {}
            except Exception:
                body = {"raw": (resp.text or "")[:2000]}
            if resp.status_code >= 500:
                last_err = f"Navira HTTP {resp.status_code}: {_pick(body, 'message', 'error', 'detail') or resp.reason_phrase}"
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_S[attempt])
                    continue
                return False, body if isinstance(body, dict) else None, last_err, last_status
            if resp.status_code >= 400:
                msg = _pick(body, "message", "error", "detail", "errorMessage") or f"HTTP {resp.status_code}"
                return False, body if isinstance(body, dict) else None, f"Navira: {msg}", last_status
            return True, body if isinstance(body, dict) else {}, None, last_status
        except httpx.TimeoutException:
            last_err = "Navira timeout"
        except httpx.HTTPError as exc:
            last_err = f"Navira network error: {exc}"
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF_S[attempt])
    return False, None, last_err or "Navira request failed", last_status


class MockPartnerAdapter(PartnerAdapter):
    """Simulira fiskalizacionog partnera — EFI-oblikovan IKOF/JIKR/QR, bez SOAP-a."""

    def fiscalize(
        self,
        tenant: Tenant,
        request: FiscalizeRequest,
        *,
        inv_ord_num: int,
    ) -> PartnerResult:
        if request.totals.gross <= 0:
            return PartnerResult(ok=False, error_message="totals.gross must be > 0")

        fiscal, inv_num = _fiscal_context(tenant, request, inv_ord_num)
        ext = request.external_id or inv_num
        iic_sig: str | None = None
        try:
            material = load_tenant_pkcs12(tenant.slug)
        except SigningError:
            material = None
        if material:
            plain = build_iic_plain(
                tin=tenant.pib,
                issue_datetime=request.issue_datetime,
                inv_num=inv_num,
                busin_unit_code=fiscal.busin_unit_code,
                tcr_code=fiscal.tcr_code,
                soft_code=fiscal.soft_code,
                tot_price=request.totals.gross,
            )
            ikof, iic_sig = sign_iic(material, plain)
        else:
            seed = f"{tenant.slug}:{ext}:{request.totals.gross}:{inv_num}"
            digest = hashlib.sha256(seed.encode()).hexdigest()
            ikof = digest[:32].upper()
        jikr = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant.slug}:{ext}:{inv_num}"))
        qr_url = build_qr_url(
            mode=tenant.mode,
            iic=ikof,
            tin=tenant.pib,
            issue_datetime=request.issue_datetime,
            ord_num=inv_ord_num,
            busin_unit_code=fiscal.busin_unit_code,
            tcr_code=fiscal.tcr_code,
            soft_code=fiscal.soft_code,
            price=request.totals.gross,
        )
        payload = to_navira_payload(
            tenant,
            request,
            fiscal,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            iic=ikof,
            iic_signature=iic_sig,
        )
        return PartnerResult(
            ok=True,
            ikof=ikof,
            jikr=jikr,
            qr_url=qr_url,
            partner_ref=str(uuid.uuid4()),
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            navira_payload=payload,
        )

    def register_cash_deposit(
        self,
        tenant: Tenant,
        request: CashDepositRequest,
        *,
        change_datetime: datetime,
    ) -> DepositPartnerResult:
        if request.amount < 0:
            return DepositPartnerResult(ok=False, error_message="amount must be >= 0")
        fiscal = load_tenant_fiscal(tenant)
        payload = _deposit_payload(tenant, fiscal, request, change_datetime)
        return DepositPartnerResult(
            ok=True,
            partner_ref=str(uuid.uuid4()),
            navira_payload=payload,
        )


class HttpPartnerAdapter(PartnerAdapter):
    """Navira HTTP — EFI-shaped JSON na /v1/register-invoice i /v1/register-cash-deposit."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def fiscalize(
        self,
        tenant: Tenant,
        request: FiscalizeRequest,
        *,
        inv_ord_num: int,
    ) -> PartnerResult:
        if request.totals.gross <= 0:
            return PartnerResult(ok=False, error_message="totals.gross must be > 0")

        fiscal, inv_num = _fiscal_context(tenant, request, inv_ord_num)
        iic_sig: str | None = None
        iic: str | None = None
        try:
            material = load_tenant_pkcs12(tenant.slug)
        except SigningError:
            material = None
        if material:
            plain = build_iic_plain(
                tin=tenant.pib,
                issue_datetime=request.issue_datetime,
                inv_num=inv_num,
                busin_unit_code=fiscal.busin_unit_code,
                tcr_code=fiscal.tcr_code,
                soft_code=fiscal.soft_code,
                tot_price=request.totals.gross,
            )
            iic, iic_sig = sign_iic(material, plain)

        payload = to_navira_payload(
            tenant,
            request,
            fiscal,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            iic=iic,
            iic_signature=iic_sig,
        )
        payload["mode"] = tenant.mode
        if tenant.partner_account_id:
            payload["partnerAccountId"] = tenant.partner_account_id

        if not _base_url(self.settings):
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=payload,
                error_message="Navira base URL not configured (SEPKO_NAVIRA_BASE_URL)",
            )
        if not _api_key(self.settings):
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=payload,
                error_message="Navira API key not configured (SEPKO_NAVIRA_API_KEY)",
            )

        ok, body, err, status = _http_post_json(
            self.settings, "/v1/register-invoice", payload
        )
        if not ok:
            log.warning("Navira fiscalize failed status=%s err=%s", status, err)
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=payload,
                error_message=err or "Navira fiscalize failed",
            )

        data = body or {}
        # Nested response support: { "result": {...} } or flat
        if isinstance(data.get("result"), dict):
            data = {**data, **data["result"]}
        ikof = _pick(data, "iic", "ikof", "IIC", "IKOF") or iic
        jikr = _pick(data, "fic", "jikr", "FIC", "JIKR")
        qr_url = _pick(data, "qrUrl", "qr_url", "QRUrl", "verificationUrl")
        if not qr_url and ikof:
            qr_url = build_qr_url(
                mode=tenant.mode,
                iic=ikof,
                tin=tenant.pib,
                issue_datetime=request.issue_datetime,
                ord_num=inv_ord_num,
                busin_unit_code=fiscal.busin_unit_code,
                tcr_code=fiscal.tcr_code,
                soft_code=fiscal.soft_code,
                price=request.totals.gross,
            )
        if not jikr:
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                ikof=ikof,
                navira_payload=payload,
                error_message="Navira response missing FIC/JIKR",
            )
        partner_ref = _pick(data, "partnerRef", "partner_ref", "id", "requestId") or str(
            uuid.uuid4()
        )
        return PartnerResult(
            ok=True,
            ikof=ikof,
            jikr=jikr,
            qr_url=qr_url,
            partner_ref=partner_ref,
            inv_num=_pick(data, "invNum", "inv_num") or inv_num,
            inv_ord_num=inv_ord_num,
            navira_payload=payload,
        )

    def register_cash_deposit(
        self,
        tenant: Tenant,
        request: CashDepositRequest,
        *,
        change_datetime: datetime,
    ) -> DepositPartnerResult:
        if request.amount < 0:
            return DepositPartnerResult(ok=False, error_message="amount must be >= 0")
        fiscal = load_tenant_fiscal(tenant)
        payload = _deposit_payload(tenant, fiscal, request, change_datetime)
        if tenant.partner_account_id:
            payload["partnerAccountId"] = tenant.partner_account_id

        if not _base_url(self.settings):
            return DepositPartnerResult(
                ok=False,
                navira_payload=payload,
                error_message="Navira base URL not configured (SEPKO_NAVIRA_BASE_URL)",
            )
        if not _api_key(self.settings):
            return DepositPartnerResult(
                ok=False,
                navira_payload=payload,
                error_message="Navira API key not configured (SEPKO_NAVIRA_API_KEY)",
            )

        ok, body, err, status = _http_post_json(
            self.settings, "/v1/register-cash-deposit", payload
        )
        if not ok:
            log.warning("Navira cash deposit failed status=%s err=%s", status, err)
            return DepositPartnerResult(
                ok=False,
                navira_payload=payload,
                error_message=err or "Navira cash deposit failed",
            )
        data = body or {}
        if isinstance(data.get("result"), dict):
            data = {**data, **data["result"]}
        partner_ref = _pick(data, "partnerRef", "partner_ref", "id", "requestId") or str(
            uuid.uuid4()
        )
        return DepositPartnerResult(
            ok=True,
            partner_ref=partner_ref,
            navira_payload=payload,
        )


def get_partner_adapter() -> PartnerAdapter:
    settings = get_settings()
    if settings.partner_mode in ("http", "navira"):
        return HttpPartnerAdapter(settings)
    return MockPartnerAdapter()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def d(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value))
