"""Idempotentni tenanti za produkciju (prostudio.me, hotel). Lozinke samo iz env."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from sepko.brand import LICENSE_ITEM
from sepko.catalog import ensure_tax_rates
from sepko.config import get_settings
from sepko.efi import TenantCompany, TenantFiscal, save_tenant_company, save_tenant_fiscal
from sepko.models import Article, Tenant, TenantStatus, User
from sepko.web_auth import hash_password

_DEV_PASSWORD = "sepko123"

# ProStudio usluge (+ licenca) — kopija seed kataloga bez hotelskih soba
_PROSTUDIO_ARTICLES: list[tuple[str, str, str, Decimal, Decimal, str, str]] = [
    ("PR-BASIC", LICENSE_ITEM, "KOM", Decimal("15.00"), Decimal("21"), "#5a32d6", "demo/proracun.svg"),
    ("2", "Izrada web sajta", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/web.svg"),
    ("3", "B2B sistem", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/b2b.svg"),
    ("7", "Cloudflare + konfiguracija", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/cloud.svg"),
    ("8", "Cloud VPS XL + konfiguracija", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/vps.svg"),
    ("10", "Izrada API-a", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/api.svg"),
    ("11", "Dodatni radovi", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/tools.svg"),
    ("16", "Cloudflare pretplata", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/cloud.svg"),
    ("17", "Izrada logotipa", "kom", Decimal("300.00"), Decimal("21"), "#c62828", "demo/logo.svg"),
    ("22", "Mail servis", "kom", Decimal("1.00"), Decimal("21"), "#c62828", "demo/mail.svg"),
    ("27", "Izrada aplikacije", "1", Decimal("5000.00"), Decimal("21"), "#c62828", "demo/app.svg"),
    ("28", "Intranet", "1", Decimal("3305.7851"), Decimal("21"), "#c62828", "demo/intranet.svg"),
    ("30", "Flatera", "Kom", Decimal("0.00"), Decimal("21"), "#c62828", "demo/tools.svg"),
]


def _ensure_articles(
    db: Session,
    tenant: Tenant,
    samples: list[tuple[str, str, str, Decimal, Decimal, str, str]],
) -> None:
    for code, name, unit, price, vat, color, thumb in samples:
        existing = (
            db.query(Article)
            .filter(Article.tenant_id == tenant.id, Article.code == code)
            .first()
        )
        tax_code = "PDV21" if vat == Decimal("21") else "PDV15"
        if not existing:
            db.add(
                Article(
                    tenant_id=tenant.id,
                    code=code,
                    name=name,
                    unit=unit,
                    price_gross=price,
                    vat_rate=vat,
                    tax_rate_code=tax_code,
                    color=color,
                    thumbnail_filename=thumb,
                    active=True,
                )
            )
            continue
        existing.name = name
        existing.unit = unit
        existing.price_gross = price
        existing.vat_rate = vat
        existing.tax_rate_code = tax_code
        existing.color = color or existing.color
        if not existing.thumbnail_filename or (
            str(existing.thumbnail_filename).startswith("demo/")
            and thumb.startswith("demo/")
        ):
            existing.thumbnail_filename = thumb
        existing.active = True


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
    if slug == "prostudio":
        _ensure_articles(db, tenant, _PROSTUDIO_ARTICLES)

    user = db.query(User).filter(User.email == email).first()
    if not user and email == "admin@philiahotel.com":
        # rename legacy seed account
        user = db.query(User).filter(User.email == "admin@hotel.me").first()
        if user:
            user.email = email
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
                email="finansije@prostudio.me",
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
                email="admin@philiahotel.com",
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
