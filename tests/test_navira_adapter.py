"""Unit/contract tests for Navira HttpPartnerAdapter (mocked HTTP)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from sepko.config import Settings
from sepko.models import Tenant
from sepko.partner import HttpPartnerAdapter, MockPartnerAdapter
from sepko.schemas import (
    BuyerIn,
    CashDepositRequest,
    FiscalizeRequest,
    InvoiceLineIn,
    TotalsIn,
)


def _tenant() -> Tenant:
    return Tenant(
        id=1,
        slug="philia",
        name="PHILIA DOO",
        pib="12345678",
        status="active",
        mode="test",
        settings_json='{"fiscal":{"busin_unit_code":"ph000bu001","tcr_code":"ph000cr001","soft_code":"ss12345678","operator_code":"op12345678","is_issuer_in_vat":true}}',
    )


def _fiscalize_req() -> FiscalizeRequest:
    return FiscalizeRequest(
        external_id="test-1",
        invoice_type="NONCASH",
        payment_method="ORDER",
        issue_datetime=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        buyer=BuyerIn(pib="87654321", name="Kupac DOO"),
        lines=[
            InvoiceLineIn(
                code="A1",
                name="Usluga",
                quantity=Decimal("1"),
                unit_price_net=Decimal("100"),
                vat_rate=Decimal("21"),
                total_gross=Decimal("121"),
            )
        ],
        totals=TotalsIn(net=Decimal("100"), vat=Decimal("21"), gross=Decimal("121")),
    )


def test_mock_fiscalize_ok():
    adapter = MockPartnerAdapter()
    result = adapter.fiscalize(_tenant(), _fiscalize_req(), inv_ord_num=1)
    assert result.ok
    assert result.ikof
    assert result.jikr
    assert result.qr_url
    assert "tax.gov.me" in (result.qr_url or "")


def test_http_missing_base_url():
    settings = Settings(
        secret_key="test-secret-key-for-unit",
        partner_mode="navira",
        navira_base_url="",
        navira_api_key="key",
    )
    adapter = HttpPartnerAdapter(settings)
    result = adapter.fiscalize(_tenant(), _fiscalize_req(), inv_ord_num=1)
    assert not result.ok
    assert "base URL" in (result.error_message or "")
    assert result.navira_payload


def test_http_fiscalize_success():
    settings = Settings(
        secret_key="test-secret-key-for-unit",
        partner_mode="navira",
        navira_base_url="https://navira.test",
        navira_api_key="secret-key",
    )
    adapter = HttpPartnerAdapter(settings)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"iic":"ABC123","fic":"jikr-uuid","qrUrl":"https://efitest.tax.gov.me/ic/#/verify?iic=ABC123"}'
    mock_resp.json.return_value = {
        "iic": "ABC123",
        "fic": "jikr-uuid",
        "qrUrl": "https://efitest.tax.gov.me/ic/#/verify?iic=ABC123",
        "partnerRef": "ref-1",
    }

    with patch("sepko.partner.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.fiscalize(_tenant(), _fiscalize_req(), inv_ord_num=7)

    assert result.ok
    assert result.ikof == "ABC123"
    assert result.jikr == "jikr-uuid"
    assert result.partner_ref == "ref-1"
    assert result.inv_ord_num == 7
    client.post.assert_called_once()
    call_kw = client.post.call_args
    assert "/v1/register-invoice" in call_kw[0][0]


def test_http_fiscalize_server_error_retries():
    settings = Settings(
        secret_key="test-secret-key-for-unit",
        partner_mode="navira",
        navira_base_url="https://navira.test",
        navira_api_key="secret-key",
    )
    adapter = HttpPartnerAdapter(settings)
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.content = b'{"message":"busy"}'
    mock_resp.reason_phrase = "Service Unavailable"
    mock_resp.json.return_value = {"message": "busy"}

    with patch("sepko.partner.httpx.Client") as client_cls, patch("sepko.partner.time.sleep"):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.fiscalize(_tenant(), _fiscalize_req(), inv_ord_num=1)

    assert not result.ok
    assert "503" in (result.error_message or "") or "busy" in (result.error_message or "")
    assert client.post.call_count == 3


def test_http_cash_deposit_success():
    settings = Settings(
        secret_key="test-secret-key-for-unit",
        partner_mode="navira",
        navira_base_url="https://navira.test",
        navira_api_key="secret-key",
    )
    adapter = HttpPartnerAdapter(settings)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"id":"dep-9"}'
    mock_resp.json.return_value = {"id": "dep-9"}

    with patch("sepko.partner.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.register_cash_deposit(
            _tenant(),
            CashDepositRequest(operation="INITIAL", amount=Decimal("50")),
            change_datetime=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
        )

    assert result.ok
    assert result.partner_ref == "dep-9"
