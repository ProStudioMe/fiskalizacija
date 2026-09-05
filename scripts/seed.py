"""Seed demo tenant PHILIA, API key, web user, sample articles."""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sepko.auth import generate_api_key
from sepko.db import SessionLocal, init_db
from sepko.efi import TenantCompany, TenantFiscal, save_tenant_company, save_tenant_fiscal
from sepko.models import ApiKey, Article, Customer, Tenant, User
from sepko.web_auth import ensure_superadmin, hash_password


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-key", action="store_true")
    parser.add_argument("--password", default="sepko123")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        super_user = ensure_superadmin(db)
        if super_user:
            print("Platform admin: super@sepko.me  (SEPKO_SUPERADMIN_PASSWORD ili sepko-super)")

        tenant = db.query(Tenant).filter(Tenant.slug == "philia").first()
        if not tenant:
            from datetime import date, timedelta

            today = date.today()
            tenant = Tenant(
                slug="philia",
                name="PHILIA DOO",
                pib="TBD-PHILIA-PIB",
                status="active",
                mode="test",
                license_type="yearly",
                license_from=today,
                license_until=today + timedelta(days=365),
            )
            db.add(tenant)
            db.flush()

        save_tenant_fiscal(
            tenant,
            TenantFiscal(
                busin_unit_code="ph000bu001",
                tcr_code="ph000cr001",
                soft_code="sepko00001",
                operator_code="op00000001",
                is_issuer_in_vat=True,
            ),
        )
        save_tenant_company(
            tenant,
            TenantCompany(
                address="Podgorica, Crna Gora",
                address2="",
                pdv_number="",
                bank_account="",
                website="",
            ),
        )

        from sepko.catalog import ensure_tax_rates

        ensure_tax_rates(db, tenant)

        user = db.query(User).filter(User.email == "admin@philia.me").first()
        if not user:
            db.add(
                User(
                    tenant_id=tenant.id,
                    email="admin@philia.me",
                    password_hash=hash_password(args.password),
                    full_name="PHILIA Admin",
                    role="admin",
                    active=True,
                )
            )
            print(f"Web login: admin@philia.me / {args.password}")
        else:
            print("Web user admin@philia.me already exists.")

        samples = [
            # code, name, unit, price, vat, color, demo_thumb
            ("GROUP-DBL", "GROUP - DOUBLE", "KOM", Decimal("79.00"), Decimal("15"), "#c62828", "demo/intranet.svg"),
            ("GROUP-DBU", "GROUP - DOUBLE SU", "KOM", Decimal("69.00"), Decimal("15"), "#c62828", "demo/intranet.svg"),
            ("GROUP-SIN", "GROUP - SINGLE", "KOM", Decimal("59.00"), Decimal("15"), "#c62828", "demo/intranet.svg"),
            ("10000", "Quadruple room", "KOM", Decimal("139.00"), Decimal("21"), "#c62828", "demo/intranet.svg"),
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
        for code, name, unit, price, vat, color, thumb in samples:
            existing = db.query(Article).filter(Article.tenant_id == tenant.id, Article.code == code).first()
            if not existing:
                db.add(
                    Article(
                        tenant_id=tenant.id,
                        code=code,
                        name=name,
                        unit=unit,
                        price_gross=price,
                        vat_rate=vat,
                        tax_rate_code="PDV21" if vat == Decimal("21") else "PDV15",
                        color=color,
                        thumbnail_filename=thumb,
                        active=True,
                    )
                )
            else:
                # dopuni cijenu/naziv ako je placeholder
                existing.name = name
                existing.unit = unit
                existing.price_gross = price
                existing.vat_rate = vat
                existing.tax_rate_code = "PDV21" if vat == Decimal("21") else "PDV15"
                existing.color = color or existing.color
                if not existing.thumbnail_filename:
                    existing.thumbnail_filename = thumb
                existing.active = True

        if not db.query(Customer).filter(Customer.tenant_id == tenant.id, Customer.pib == "03010864").first():
            db.add(
                Customer(
                    tenant_id=tenant.id,
                    pib="03010864",
                    pdv_number="30/31-03010864",
                    name="Demo kupac CG",
                    street="Hercegovacka 2",
                    city="Podgorica",
                    country="Crna Gora",
                    email="demo@kupac.me",
                    phone="+382 20 100 200",
                    address="Hercegovacka 2, Podgorica, Crna Gora",
                    active=True,
                )
            )
        if not db.query(Customer).filter(Customer.tenant_id == tenant.id, Customer.pib == "MNE02-02235-7").first():
            db.add(
                Customer(
                    tenant_id=tenant.id,
                    pib="MNE02-02235-7",
                    name="Montenegro tourist service",
                    street="Moskovska 105",
                    city="Podgorica",
                    country="Crna Gora",
                    email="info@mts.me",
                    phone="+382 20 300 400",
                    address="Moskovska 105, Podgorica, Crna Gora",
                    active=True,
                )
            )

        existing_key = (
            db.query(ApiKey).filter(ApiKey.tenant_id == tenant.id, ApiKey.active.is_(True)).first()
        )
        if existing_key and not args.force_key:
            db.commit()
            print(f"Tenant philia id={tenant.id}. Use --force-key for new API key.")
            return

        if existing_key and args.force_key:
            existing_key.active = False

        raw, key_hash, prefix = generate_api_key()
        db.add(
            ApiKey(
                tenant_id=tenant.id,
                name="dev",
                key_hash=key_hash,
                key_prefix=prefix,
                active=True,
            )
        )
        db.commit()
        print("Sepko seed OK (PostgreSQL)")
        print(f"  tenant: philia ({tenant.name})")
        print("  API key (save once):")
        print(f"  {raw}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
