"""EXTFISK XML + adapter (mock HTTP). JIKR TEST = uspjeh."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from sepko.config import Settings
from sepko.extfisk import (
    ExtfiskPartnerAdapter,
    build_register_invoice_xml,
    extfisk_pay_type,
    extfisk_request_id,
)
from sepko.models import Tenant
from sepko.schemas import BuyerIn, FiscalizeRequest, InvoiceLineIn, TotalsIn


def _tenant(**settings_extra) -> Tenant:
    data = {
        "busin_unit_code": "ps000bu001",
        "tcr_code": "ps000cr001",
        "soft_code": "sepko00001",
        "operator_code": "op00000011",
        "is_issuer_in_vat": True,
        "company": {"address": "Bulevar 21. maj 24, Podgorica, Crna Gora"},
    }
    data.update(settings_extra)
    import json

    return Tenant(
        id=1,
        slug="prostudio",
        name="PROSTUDIO.ME DOO",
        pib="03452668",
        status="active",
        mode="test",
        settings_json=json.dumps(data, ensure_ascii=False),
    )


def _req(**kwargs) -> FiscalizeRequest:
    base = dict(
        external_id="test-1",
        invoice_type="NONCASH",
        payment_method="ORDER",
        issue_datetime=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        buyer=BuyerIn(pib="02260620", name="Kupac DOO", address="Adresa, Niksic"),
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
    base.update(kwargs)
    return FiscalizeRequest(**base)


def _settings(**kwargs) -> Settings:
    values = dict(
        secret_key="test-secret-key-for-unit",
        partner_mode="extfisk",
        extfisk_url="http://extfisk.test/api/extfisk",
        extfisk_api_key="test-api-key",
        extfisk_environment="TEST",
    )
    values.update(kwargs)
    return Settings(**values)


def _xml() -> ET.Element:
    raw = build_register_invoice_xml(
        _tenant(),
        _req(),
        api_key="test-api-key",
        environment="TEST",
        request_id="1000001",
        inv_num="ps000bu001/1/2026/ps000cr001",
        inv_ord_num=1,
        fiscal_busin_unit="ps000bu001",
        fiscal_operator="op00000011",
        fiscal_tcr="ps000cr001",
    )
    assert raw.startswith("<?xml version")
    return ET.fromstring(raw.split("?>", 1)[-1])


def test_xml_api_key_inside_body_not_header():
    root = _xml()
    assert root.findtext("ApiKey") == "test-api-key"
    assert root.findtext("Environment") == "TEST"
    assert root.findtext("InvType") == "RACUN"
    assert root.findtext("Seller/IDNum") == "03452668"
    assert root.findtext("Seller/Town") == "Podgorica"
    assert root.findtext("Buyer/BuyerType") == "PRAVNO"
    assert root.findtext("Invoice/PayMethods/PayMethod/Type") == "ACCOUNT"
    assert extfisk_pay_type("ORDER") == "ACCOUNT"
    # Oracle TO_DATE — dd.MM.yyyy (ne ISO); vrijeme u lokalnoj zoni
    issue = root.findtext("Invoice/IssueDateTime") or ""
    assert issue.endswith("2026") or ".2026 " in issue
    assert issue.count(".") >= 2 and "T" not in issue and "+" not in issue
    assert root.findtext("Invoice/PayDeadline") == "24.09.2026"
    assert root.findtext("Invoice/TaxPeriod") == "09/2026"


def test_request_id_stable_for_retry():
    t = _tenant()
    a = extfisk_request_id(t, "ps000bu001/1/2026/ps000cr001")
    b = extfisk_request_id(t, "ps000bu001/1/2026/ps000cr001")
    c = extfisk_request_id(t, "ps000bu001/2/2026/ps000cr001")
    assert a == b
    assert a != c


def test_jikr_test_is_success():
    adapter = ExtfiskPartnerAdapter(_settings())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"status":"OK","jikr":"TEST","poruka":"Racun uspjesno fiskalizovan"}'
    mock_resp.json.return_value = {
        "status": "OK",
        "code": "1042",
        "poruka": "Racun uspjesno fiskalizovan",
        "jikr": "TEST",
        "idReq": 1042,
    }

    with patch("sepko.extfisk.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.fiscalize(_tenant(), _req(), inv_ord_num=1)

    assert result.ok
    assert result.jikr == "TEST"
    assert result.ikof
    assert not result.offline
    call_kw = client.post.call_args
    assert call_kw[0][0] == "http://extfisk.test/api/extfisk"
    headers = call_kw.kwargs["headers"]
    assert headers["Content-Type"] == "application/xml"
    assert "Authorization" not in headers
    assert "X-Api-Key" not in headers
    xml = call_kw.kwargs["content"].decode("utf-8")
    assert "<ApiKey>test-api-key</ApiKey>" in xml


def test_business_error_http_200():
    adapter = ExtfiskPartnerAdapter(_settings())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"status":"ERROR","code":"-20016","poruka":"Racun nema stavki"}'
    mock_resp.json.return_value = {
        "status": "ERROR",
        "code": "-20016",
        "poruka": "Racun nema stavki",
    }

    with patch("sepko.extfisk.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.fiscalize(_tenant(), _req(), inv_ord_num=1)

    assert not result.ok
    assert not result.offline
    assert "nema stavki" in (result.error_message or "")


def test_missing_api_key():
    adapter = ExtfiskPartnerAdapter(_settings(extfisk_api_key=""))
    result = adapter.fiscalize(_tenant(), _req(), inv_ord_num=1)
    assert not result.ok
    assert "ApiKey" in (result.error_message or "") or "firmu" in (result.error_message or "")


def test_tenant_api_key_preferred_over_env():
    adapter = ExtfiskPartnerAdapter(_settings(extfisk_api_key="env-fallback-key"))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"status":"OK","jikr":"TEST"}'
    mock_resp.json.return_value = {"status": "OK", "jikr": "TEST"}

    with patch("sepko.extfisk.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.fiscalize(
            _tenant(extfisk_api_key="tenant-pib-key"),
            _req(),
            inv_ord_num=1,
        )

    assert result.ok
    xml = client.post.call_args.kwargs["content"].decode("utf-8")
    assert "<ApiKey>tenant-pib-key</ApiKey>" in xml
    assert "env-fallback-key" not in xml


def test_http_500_offline_when_ikof_exists():
    adapter = ExtfiskPartnerAdapter(_settings())
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.content = b'{"status":"ERROR","poruka":"ORA-00904"}'
    mock_resp.reason_phrase = "Internal Server Error"
    mock_resp.json.return_value = {"status": "ERROR", "poruka": "ORA-00904"}

    with patch("sepko.extfisk.httpx.Client") as client_cls, patch("sepko.extfisk.time.sleep"):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        result = adapter.fiscalize(
            _tenant(),
            _req(),
            inv_ord_num=1,
            reuse_iic="AABBCCDDEEFF00112233445566778899",
            reuse_iic_signature="sig",
        )

    assert not result.ok
    assert result.offline
    assert result.ikof == "AABBCCDDEEFF00112233445566778899"
    assert client.post.call_count == 3
