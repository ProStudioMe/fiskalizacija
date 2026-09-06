"""Crnogorski format iznosa (EUR): 1.234,56

Prikaz: tačka = hiljade, zarez = decimala.
Unos: prihvata CG (1.234,56 / 12,50) i US (1234.56) stil.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

_MAX_MONEY = Decimal("10000000")
# Dozvoljeni oblici: 1234 | 1.234,56 | 1234,56 | 1234.56 | 1.234.567,89
_AMOUNT_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:[,.]\d+)?$|^[+-]?\d*[,.]\d+$")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def format_amount(value: Any, ndigits: int = 2) -> str:
    """Formatiraj broj za CG prikaz: 1234.5 → '1.234,50'."""
    d = _to_decimal(value)
    if d is None:
        d = Decimal("0")
    ndigits = max(0, int(ndigits))
    quant = Decimal("1").scaleb(-ndigits) if ndigits else Decimal("1")
    d = d.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    d = abs(d)
    raw = f"{d:.{ndigits}f}" if ndigits else f"{int(d)}"
    if ndigits:
        int_part, frac = raw.split(".")
    else:
        int_part, frac = raw, None
    groups: list[str] = []
    while int_part:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    body = ".".join(groups) if groups else "0"
    if frac is None:
        return f"{sign}{body}"
    return f"{sign}{body},{frac}"


def parse_amount(raw: Any, *, quantize: str | Decimal | None = "0.01") -> Decimal | None:
    """Parsira iznos (CG ili US). Neispravan unos → None.

    Pravila:
    - ``1.234,56`` → hiljade + decimala (CG)
    - ``12,50`` → decimala zarezom
    - ``12.50`` / ``1.2345`` → decimala tačkom (cijene / API)
    - ``1.234.567`` → samo hiljade
    """
    if raw is None:
        return None
    if isinstance(raw, (Decimal, int, float)) and not isinstance(raw, bool):
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
    else:
        t = str(raw).strip().replace("\u00a0", "").replace(" ", "")
        if not t:
            return None
        t = re.sub(r"(?i)(?:€|eur)$", "", t).strip()
        if not t or not _AMOUNT_RE.match(t):
            return None
        if "," in t and "." in t:
            t = t.replace(".", "").replace(",", ".")
        elif "," in t:
            t = t.replace(",", ".")
        elif t.count(".") > 1:
            t = t.replace(".", "")
        try:
            value = Decimal(t)
        except (InvalidOperation, ValueError):
            return None
    if quantize is not None:
        try:
            value = value.quantize(Decimal(str(quantize)), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return None
    return value


def parse_nonneg_money(
    raw: Any,
    *,
    max_value: Decimal = _MAX_MONEY,
    quantize: str | Decimal | None = None,
) -> Decimal | None:
    """Validacija nenegativnog iznosa (cijena, uplata, depozit…)."""
    value = parse_amount(raw, quantize=quantize)
    if value is None:
        return None
    if value < 0 or value > max_value:
        return None
    return value


def parse_discount_pct(raw: Any) -> Decimal:
    """Popust 0–100 %. Neispravan unos → 0."""
    value = parse_amount(raw, quantize="0.01")
    if value is None:
        return Decimal("0")
    if value < 0:
        return Decimal("0")
    if value > 100:
        return Decimal("100")
    return value


def money_filter(value: Any, ndigits: int = 2) -> str:
    """Jinja filter: {{ x|money }} ili {{ x|money(4) }}."""
    return format_amount(value, ndigits=ndigits)


def format_qty(value: Any, max_digits: int = 4) -> str:
    """Količina bez suvišnih nula: 1 → '1', 1.5 → '1,5', 1.2500 → '1,25'."""
    formatted = format_amount(value, ndigits=max_digits)
    if "," not in formatted:
        return formatted
    int_part, frac = formatted.rsplit(",", 1)
    frac = frac.rstrip("0")
    return int_part if not frac else f"{int_part},{frac}"


def qty_filter(value: Any, max_digits: int = 4) -> str:
    """Jinja filter: {{ x|qty }}."""
    return format_qty(value, max_digits=max_digits)
