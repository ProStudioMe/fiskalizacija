from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import uuid

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
    """Navira fiskalizacija — payload je spreman; HTTP kad stignu API docs."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def fiscalize(
        self,
        tenant: Tenant,
        request: FiscalizeRequest,
        *,
        inv_ord_num: int,
    ) -> PartnerResult:
        base = self.settings.partner_base_url or self.settings.navira_base_url
        fiscal, inv_num = _fiscal_context(tenant, request, inv_ord_num)
        payload = to_navira_payload(
            tenant, request, fiscal, inv_num=inv_num, inv_ord_num=inv_ord_num
        )
        if not base:
            return PartnerResult(
                ok=False,
                inv_num=inv_num,
                inv_ord_num=inv_ord_num,
                navira_payload=payload,
                error_message="Navira base URL not configured",
            )
        return PartnerResult(
            ok=False,
            inv_num=inv_num,
            inv_ord_num=inv_ord_num,
            navira_payload=payload,
            error_message="Navira adapter not wired yet — payload is EFI-shaped; set SEPKO_PARTNER_MODE=mock until Navira API docs arrive",
        )

    def register_cash_deposit(
        self,
        tenant: Tenant,
        request: CashDepositRequest,
        *,
        change_datetime: datetime,
    ) -> DepositPartnerResult:
        fiscal = load_tenant_fiscal(tenant)
        payload = _deposit_payload(tenant, fiscal, request, change_datetime)
        return DepositPartnerResult(
            ok=False,
            navira_payload=payload,
            error_message="Navira cash deposit not wired yet — use SEPKO_PARTNER_MODE=mock",
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
