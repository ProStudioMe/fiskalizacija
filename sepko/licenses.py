"""Licenca tenanta — tip, rok, upozorenje (VG stil, < 30 dana)."""
from __future__ import annotations

from datetime import date, timedelta

from sepko.models import Tenant

LICENSE_TYPES = ("trial", "monthly", "yearly")
LICENSE_WARN_DAYS = 30

LICENSE_LABELS = {
    "trial": "Proba",
    "monthly": "Mjesečna",
    "yearly": "Godišnja",
}


def license_label(license_type: str | None) -> str:
    code = (license_type or "trial").strip() or "trial"
    return LICENSE_LABELS.get(code, code)


def license_days_left(until: date | None, *, today: date | None = None) -> int | None:
    if until is None:
        return None
    return (until - (today or date.today())).days


def license_alert(tenant: Tenant, *, today: date | None = None) -> str | None:
    """'expired' | 'expiring' | None."""
    days = license_days_left(tenant.license_until, today=today)
    if days is None:
        return None
    if days < 0:
        return "expired"
    if days < LICENSE_WARN_DAYS:
        return "expiring"
    return None


def default_license_period(license_type: str, *, today: date | None = None) -> tuple[date, date]:
    start = today or date.today()
    kind = (license_type or "trial").strip() or "trial"
    if kind == "yearly":
        delta = timedelta(days=365)
    elif kind == "monthly":
        delta = timedelta(days=31)
    else:
        delta = timedelta(days=30)
    return start, start + delta
