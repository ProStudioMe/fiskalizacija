"""IKOF / IIC prema Tehničkoj spec v5 §4.3.2 — samo PU CIS.

Ulaz: PIB|IssueDateTime|InvOrdNum|BusinUnitCode|TCRCode|SoftCode|TotPrice
Potpis: RSASSA-PKCS1-v1_5 + SHA256
IICSignature: hex (velika slova) potpisa
IIC: MD5(bajtovi potpisa) hex
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib

from sepko.signing import Pkcs12Material, SigningError, rsa_sign

_PODGORICA = timezone(timedelta(hours=1))


def pu_datetime(dt: datetime) -> str:
    """ISO sa offsetom — isti string ide u Invoice i u IKOF ulaz."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_PODGORICA)
    return dt.isoformat(timespec="seconds")


def build_pu_iic_plain(
    *,
    tin: str,
    issue_datetime: datetime,
    inv_ord_num: int,
    busin_unit_code: str,
    tcr_code: str,
    soft_code: str,
    tot_price: Decimal | str | int,
) -> str:
    price = Decimal(str(tot_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    parts = [
        (tin or "").strip(),
        pu_datetime(issue_datetime),
        str(inv_ord_num),
        (busin_unit_code or "").strip(),
        (tcr_code or "").strip(),
        (soft_code or "").strip(),
        f"{price:.2f}",
    ]
    return "|".join(parts)


def sign_pu_iic(material: Pkcs12Material, plain: str) -> tuple[str, str]:
    """Vrati (IIC hex32, IICSignature hex) po PU algoritmu."""
    raw = rsa_sign(material, plain.encode("utf-8"))
    signature_hex = raw.hex().upper()
    iic = hashlib.md5(raw).hexdigest().upper()
    return iic, signature_hex


def require_tenant_cert(slug: str) -> Pkcs12Material:
    from sepko.signing import load_tenant_pkcs12

    try:
        material = load_tenant_pkcs12(slug)
    except SigningError:
        raise
    if material is None:
        raise SigningError(
            "PU CIS zahtijeva certifikat firme: SEPKO_CERT_DIR/{slug}.p12 (SEP / Poreska)"
        )
    return material
