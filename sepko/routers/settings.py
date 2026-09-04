from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from sepko.auth import get_tenant
from sepko.config import get_settings
from sepko.db import get_db
from sepko.models import ApiKey, Tenant
from sepko.efi import load_tenant_fiscal
from sepko.schemas import FiscalCodesOut, SettingsOut, TenantOut

router = APIRouter(prefix="/v1", tags=["settings"])


@router.get("/me", response_model=TenantOut)
def me(tenant: Tenant = Depends(get_tenant)) -> Tenant:
    return tenant


@router.get("/settings", response_model=SettingsOut)
def settings(
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> SettingsOut:
    keys = (
        db.query(ApiKey)
        .filter(ApiKey.tenant_id == tenant.id, ApiKey.active.is_(True))
        .all()
    )
    fiscal = load_tenant_fiscal(tenant)
    return SettingsOut(
        tenant=TenantOut.model_validate(tenant),
        partner_mode=get_settings().partner_mode,
        webhook_url=tenant.webhook_url,
        api_key_prefixes=[k.key_prefix for k in keys],
        fiscal=FiscalCodesOut(
            busin_unit_code=fiscal.busin_unit_code,
            tcr_code=fiscal.tcr_code,
            soft_code=fiscal.soft_code,
            operator_code=fiscal.operator_code,
            is_issuer_in_vat=fiscal.is_issuer_in_vat,
            token_provider=fiscal.token_provider,
            has_telekom_token=fiscal.has_telekom_token(),
            has_posta_token=fiscal.has_posta_token(),
        ),
    )
