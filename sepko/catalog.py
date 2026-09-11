"""Šifrarnici artikala — kategorije i poreske stope (CG/EFI).

Stope i oslobođenja prate Zakon o porezu na dodatu vrijednost Crne Gore:
- čl.24 opšta 21%
- čl.24a snižene 7% i 15%
- čl.25 nulta stopa (0%)
- čl.20 ispravka poreske osnovice
- čl.26–30 oslobođenja
- čl.44 poseban postupak (putničke agencije)
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from sepko.models import Article, TaxRate, Tenant

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

# code, name, rate%, is_default, sort_order, active
DEFAULT_TAX_RATES: list[tuple[str, str, str, bool, int, bool]] = [
    ("PDV21", "PDV 21% — opšta stopa (čl.24)", "21", True, 1, True),
    ("PDV15", "PDV 15% — snižena stopa (čl.24a)", "15", False, 2, True),
    ("PDV7", "PDV 7% — snižena stopa (čl.24a)", "7", False, 3, True),
    ("PDV0", "PDV 0% — nulta stopa (čl.25)", "0", False, 4, True),
    ("EX20", "Poreska osnovica i ispravka poreske osnovice (čl.20)", "0", False, 5, True),
    ("EX26", "Oslobođeno od javnog interesa (čl.26)", "0", False, 6, True),
    ("EX27", "Ostala oslobođenja (čl.27)", "0", False, 7, True),
    ("EX28", "Oslobođenja kod uvoza proizvoda (čl.28)", "0", False, 8, True),
    ("EX29", "Oslobođenja kod privremenog uvoza proizvoda (čl.29)", "0", False, 9, True),
    ("EX30", "Posebna oslobođenja (čl.30)", "0", False, 10, True),
    ("EX44", "Poseban postupak oporezivanja — putničke agencije (čl.44)", "0", False, 11, True),
    # Zastarjelo: čl.17 je mjesto prometa usluga, ne oslobođenje; čl.25 ≠ javni interes.
    ("EX17", "Zastarjelo — nije oslobođenje po čl.17", "0", False, 90, False),
    ("EX25", "Zastarjelo — koristi PDV0 (čl.25) ili EX26 (čl.26)", "0", False, 91, False),
]


def _migrate_legacy_ex25(db: Session, tenant: Tenant) -> bool:
    """Stari EX25 je bio pogrešno označen kao javni interes (to je čl.26)."""
    changed = False
    n = (
        db.query(Article)
        .filter(Article.tenant_id == tenant.id, Article.tax_rate_code == "EX25")
        .update({"tax_rate_code": "EX26"}, synchronize_session=False)
    )
    if n:
        changed = True
    return changed


def ensure_tax_rates(db: Session, tenant: Tenant) -> list[TaxRate]:
    existing = {
        t.code: t
        for t in db.query(TaxRate).filter(TaxRate.tenant_id == tenant.id).all()
    }
    changed = _migrate_legacy_ex25(db, tenant)

    for code, name, rate, is_default, sort_order, active in DEFAULT_TAX_RATES:
        row = existing.get(code)
        if row is None:
            db.add(
                TaxRate(
                    tenant_id=tenant.id,
                    code=code,
                    name=name,
                    rate=Decimal(rate),
                    is_default=is_default,
                    sort_order=sort_order,
                    active=active,
                )
            )
            changed = True
            continue
        # Uskladi nazive/redoslijed sa važećim zakonom (ne diraj ručni is_default osim seed defaulta).
        if (
            row.name != name
            or row.rate != Decimal(rate)
            or row.sort_order != sort_order
            or bool(row.active) != active
        ):
            row.name = name
            row.rate = Decimal(rate)
            row.sort_order = sort_order
            row.active = active
            changed = True
        if is_default and not row.is_default:
            # Samo ako nijedna druga nije default — inače ostavi korisnički izbor.
            has_default = any(t.is_default for t in existing.values() if t.code != code)
            if not has_default:
                row.is_default = True
                changed = True

    if changed:
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
