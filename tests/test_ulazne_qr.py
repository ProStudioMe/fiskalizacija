"""QR import + bank statement parse unit tests."""
from __future__ import annotations

from decimal import Decimal

from sepko.finansije import parse_tx_lines_from_text
from sepko.qr_import import parse_verify_url


def test_parse_verify_url_hash_router():
    url = (
        "https://mapr.tax.gov.me/ic/#/verify?iic=ABCDEF0123456789ABCDEF0123456789"
        "&tin=12345678&crtd=2026-03-01T12:00:00&ord=3&bu=ph000bu001&cr=ph000cr001"
        "&sw=ss12345678&prc=121.00"
    )
    draft = parse_verify_url(url)
    assert draft.ikof == "ABCDEF0123456789ABCDEF0123456789"
    assert draft.supplier_pib == "12345678"
    assert draft.total_gross == Decimal("121.00")
    assert draft.ord_num == 3
    assert draft.issue_date is not None
    assert "ph000bu001" in draft.number


def test_parse_verify_url_query_only():
    raw = "iic=AAA&tin=999&prc=10,50&ord=1&crtd=2026-01-15T10:00:00"
    draft = parse_verify_url(raw)
    assert draft.ikof == "AAA"
    assert draft.supplier_pib == "999"
    assert draft.total_gross == Decimal("10.50")


def test_parse_bank_lines():
    text = """
    01.03.2026 Uplata od kupca DOO +1.250,00
    02.03.2026 Isplata dobavljac ABC -87,50
    """
    txs = parse_tx_lines_from_text(text, default_day="2026-03-01")
    assert len(txs) >= 2
    credits = [t for t in txs if t["tx_type"] == "credit"]
    debits = [t for t in txs if t["tx_type"] == "debit"]
    assert credits
    assert debits
    assert abs(credits[0]["amount"]) == Decimal("1250.00")
