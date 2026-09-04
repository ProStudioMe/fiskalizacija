"""Šifrarnici artikala — kategorije i poreske stope (CG/EFI)."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from sepko.models import TaxRate, Tenant

ARTICLE_COLORS = [
    "#e65100",
    "#5d4037",
    "#ef6c00",
    "#ffab91",
    "#42a5f5",
    "#66bb6a",
    "#fdd835",
    "#e53935",
    "#8e24aa",
]

DEFAULT_TAX_RATES: list[tuple[str, str, str, bool, int]] = [
    ("PDV21", "PDV 21%", "21", True, 1),
    ("PDV15", "PDV 15%", "15", False, 2),
    ("PDV7", "PDV 7%", "7", False, 3),
    ("PDV0", "PDV 0%", "0", False, 4),
    ("EX17", "Oslobođeno PDV-a (čl.17)", "0", False, 5),
    ("EX20", "Poreska osnovica i ispravka poreske osnovice (čl.20)", "0", False, 6),
    ("EX25", "Oslobođeno od javnog interesa (čl.25)", "0", False, 7),
    ("EX27", "Ostala oslobođenja (čl.27)", "0", False, 8),
    ("EX28", "Oslobođenja kod uvoza proizvoda (čl.28)", "0", False, 9),
    ("EX29", "Oslobođenja kod privremenog uvoza proizvoda (čl.29)", "0", False, 10),
    ("EX30", "Posebna oslobođenja (čl.30)", "0", False, 11),
    ("EX44", "Poseban postupak oporezivanja (čl.44)", "0", False, 12),
]


def ensure_tax_rates(db: Session, tenant: Tenant) -> list[TaxRate]:
    existing = {
        t.code: t
        for t in db.query(TaxRate).filter(TaxRate.tenant_id == tenant.id).all()
    }
    created = False
    for code, name, rate, is_default, sort_order in DEFAULT_TAX_RATES:
        if code in existing:
            continue
        db.add(
            TaxRate(
                tenant_id=tenant.id,
                code=code,
                name=name,
                rate=Decimal(rate),
                is_default=is_default,
                sort_order=sort_order,
                active=True,
            )
        )
        created = True
    if created:
        db.commit()
    return (
        db.query(TaxRate)
        .filter(TaxRate.tenant_id == tenant.id, TaxRate.active.is_(True))
        .order_by(TaxRate.sort_order, TaxRate.id)
        .all()
    )


def resolve_vat(db: Session, tenant: Tenant, tax_rate_code: str | None, fallback: str = "21") -> tuple[Decimal, str | None]:
    if tax_rate_code:
        row = (
            db.query(TaxRate)
            .filter(TaxRate.tenant_id == tenant.id, TaxRate.code == tax_rate_code)
            .first()
        )
        if row:
            return row.rate, row.code
    default = (
        db.query(TaxRate)
        .filter(TaxRate.tenant_id == tenant.id, TaxRate.is_default.is_(True))
        .first()
    )
    if default:
        return default.rate, default.code
    return Decimal(fallback), None
