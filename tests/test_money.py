"""Testovi CG formata i validacije iznosa."""
from __future__ import annotations

from decimal import Decimal

import pytest

from sepko.money import (
    format_amount,
    parse_amount,
    parse_discount_pct,
    parse_nonneg_money,
)


@pytest.mark.parametrize(
    "value,ndigits,expected",
    [
        (0, 2, "0,00"),
        (12.5, 2, "12,50"),
        (1234.56, 2, "1.234,56"),
        (1234567.89, 2, "1.234.567,89"),
        (-99.1, 2, "-99,10"),
        (1.2345, 4, "1,2345"),
        (1000, 0, "1.000"),
        (Decimal("0.005"), 2, "0,01"),  # ROUND_HALF_UP
    ],
)
def test_format_amount(value, ndigits, expected):
    assert format_amount(value, ndigits) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.234,56", Decimal("1234.56")),
        ("12,50", Decimal("12.50")),
        ("12.50", Decimal("12.50")),
        ("1.2345", Decimal("1.23")),  # quantize 0.01
        ("1.234.567,89", Decimal("1234567.89")),
        ("  99,9 € ", Decimal("99.90")),
        ("1234", Decimal("1234.00")),
        ("abc", None),
        ("", None),
        (None, None),
        ("-10,5", Decimal("-10.50")),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_parse_amount_no_quantize():
    assert parse_amount("1,2345", quantize=None) == Decimal("1.2345")


def test_parse_nonneg_money():
    assert parse_nonneg_money("12,50") == Decimal("12.5")
    assert parse_nonneg_money("-1") is None
    assert parse_nonneg_money("10000001") is None
    assert parse_nonneg_money("xx") is None


def test_parse_discount_pct():
    assert parse_discount_pct("12,5") == Decimal("12.50")
    assert parse_discount_pct("-3") == Decimal("0")
    assert parse_discount_pct("150") == Decimal("100")
    assert parse_discount_pct("x") == Decimal("0")
