"""Idempotentni tenanti za produkciju (prostudio.me, hotel). Lozinke samo iz env."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from sepko.catalog import ensure_tax_rates
from sepko.config import get_settings
from sepko.efi import TenantCompany, TenantFiscal, save_tenant_company, save_tenant_fiscal
from sepko.models import Tenant, TenantStatus, User
from sepko.web_auth import hash_password

_DEV_PASSWORD = "sepko123"


def _password(env_value: str, *, production: bool) -> str | None:
    value = (env_value or "").strip()
    if value:
        return value
    if production:
        return None
    return _DEV_PASSWORD


def _ensure_tenant(
    db: Session,
    *,
    slug: str,
    name: str,
    pib: str,
    email: str,
    full_name: str,
    password: str,
    fiscal: TenantFiscal,
    company: TenantCompany,
) -> None:
    today = date.today()
    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not tenant:
        tenant = Tenant(
            slug=slug,
            name=name,
            pib=pib,
            status=TenantStatus.active.value,
            mode="test",
            license_type="yearly",
            license_from=today,
            license_until=today + timedelta(days=365),
        )
        db.add(tenant)
        db.flush()
    save_tenant_fiscal(tenant, fiscal)
    save_tenant_company(tenant, company)
    ensure_tax_rates(db, tenant)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        db.add(
            User(
                tenant_id=tenant.id,
                email=email,
                password_hash=hash_password(password),
                full_name=full_name,
                role="admin",
                active=True,
            )
        )
        return
    user.tenant_id = tenant.id
    user.role = "admin"
    user.active = True
    if not user.full_name:
        user.full_name = full_name


def ensure_client_tenants(db: Session) -> None:
    """Kreira prostudio i hotel ako nedostaju. Postojeće lozinke ne dira."""
    settings = get_settings()
    production = settings.env == "production"

    specs = [
        (
            _password(settings.prostudio_admin_password, production=production),
            dict(
                slug="prostudio",
                name="PROSTUDIO.ME DOO",
                pib="03452668",
                email="admin@prostudio.me",
                full_name="ProStudio Admin",
                fiscal=TenantFiscal(
                    busin_unit_code="ps000bu001",
                    tcr_code="ps000cr001",
                    soft_code="sepko00001",
                    operator_code="op00000011",
                    is_issuer_in_vat=True,
                ),
                company=TenantCompany(
                    address="Bulevar 21. maj 24, Podgorica, Crna Gora",
                    address2="",
                    pdv_number="",
                    bank_account="",
                    website="https://prostudio.me",
                ),
            ),
        ),
        (
            _password(settings.hotel_admin_password, production=production),
            dict(
                slug="hotel",
                name="Hotel",
                pib="TBD-HOTEL-PIB",
                email="admin@hotel.me",
                full_name="Hotel Admin",
                fiscal=TenantFiscal(
                    busin_unit_code="ht000bu001",
                    tcr_code="ht000cr001",
                    soft_code="sepko00001",
                    operator_code="op00000012",
                    is_issuer_in_vat=True,
                ),
                company=TenantCompany(
                    address="Podgorica, Crna Gora",
                    address2="",
                    pdv_number="",
                    bank_account="",
                    website="",
                ),
            ),
        ),
    ]
    for password, kwargs in specs:
        if not password:
            continue
        _ensure_tenant(db, password=password, **kwargs)
