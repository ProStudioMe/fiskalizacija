"""Posebna PU fiskalizacija — IKOF spec + XMLDSig + SOAP."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import NoEncryption, pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree

from sepko.config import Settings
from sepko.efi import load_tenant_fiscal
from sepko.models import Tenant
from sepko.pu.adapter import PuPartnerAdapter, cis_service_url
from sepko.pu.iic import build_pu_iic_plain, sign_pu_iic
from sepko.pu.invoice import EFI_NS, build_register_invoice_request, wrap_soap
from sepko.pu.xmldsig import DSIG_NS, sign_enveloped
from sepko.schemas import BuyerIn, FiscalizeRequest, InvoiceLineIn, TotalsIn
from sepko.signing import load_pkcs12

# Tehnička spec v5 §4.3.2 — javni primjer ključa i očekivani IKOF
_SPEC_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA6zOR5ItNYHJNVMxljZtd/KQUyGIozbnIJ8IWqcEesktRV5FF
HviQZsx2DpyeVQTu/Kel9Xh+Z6OZ6t5sADzfYnkwCrsb0FhT+01m2PIHaIUZhVtc
ppn0gxNWfgzW4sTvTyrYk6O1Kxymsx/rck/WRQB1mp68au8mgGMzGukHfL7Wk4jO
U5VD3HlStBx1MjVW+soN5GUL/rWGaYun6Zsn9aYYEujbOhKvKDy8nOtNIS69dqdd
piZAkvdh9sYdF1ElgXZhdmZsGURMm6OcePUPZO/HFKq7RlK6vIxXVI6l9O6tWt+G
uhul8e0x2VTwbTdpwG4FpdfUTqUDK6cswHOhTQIDAQABAoIBAQCqBWJuUqDBmn76
ULMMlYZwjfAUFpkmdikRTIVzew4EltubMIFF7Sr9lMm2sFLoZKOZ8lrOwqalpqcq
GFT8KwTUO4SWDUIC7wbuf7pcE0F1tdmIBE5KhLozUnRQtFlWHkRb9z4OI+Zf3ttG
W0mpHbtnr/hTqHHN30j2wD7+MfvemPbcAvu9JLCYUzUZ06qxUwAjyFgsW7YyLa0a
qFB0QOYc6RsLvoSFXW0M5ghdtgoZvl+ayt4fgz1L3FjAMuXoLEX/778VA92/NZ0Q
mzQdKTT6B4Pm5s8XrY9OhLlsYqKuyR/aoSHC/anSLw0yJ/5Gis2gmCwo3a7+PEYy
LUN7C0yFAoGBAPhgYufTkdod5PqG/SCEE2i6pjk0ZnuIUu9f2cmhxnvyChlig2wk
oDWUSGuXwItNF+X7j3XoZz8FNJcriK7KP2UPDOWP0ZvxZgZEcmwut27x1vVjzjCG
sl0w5fFO363hhtX35Jq2lVZGbN1LpIoEZgCeS/nBs+9DcRjDoXliKWfHAoGBAPJr
qSWLVO3gIG1wikXBWCYZUTSzsO6NWfxcWPHKTnKVrOifBTK23zuZ6ggluNqLz/Ae
64ZwssMoIVIyXE01XMPP8io4QidyVEd2n70pjrVcUVYyr9IwKmchmNBfKFMof05f
NV29P1Am1Jqv2EQi5jE/BbBu9kLifs2YyGBAn/ZLAoGAVsLsqciZAVVCAFWZJHue
gA37NK5eQja7qcyUuj9dozxIVNe5ytP8dtrmdVccNkzm1TqLwYc+UaBS35+gblZN
0NJyEdqsQMoRdo0AX1PuVb369ds4UnEq6yzClgmUTxwhyqp+W6D+B5YwPxlGT8P7
kam6JnOIlEK9xgXIaStmBU8CgYB6RwXVszcOmYuhyC9mygSNix2j6LNpUJFAMtCG
fZYeRBMobvWvRADLznH21Bgu3HDxXJdOg9AXkklkbZSTOURmXKB43VG5Ffke5t3i
C3E5V6yLPxvieHsa9B5hlG4BrB6yyGFhvBCQfFWnBOWgUL4tvu0+tmmvCRIO4G7J
5i8JiwKBgQCQHTfRrGaEsq1BG7zPOQSqo9q5cxL8WzYd0sTs3FDcwCtHqxBEQ3rr
O/l+HvRa+y6ZEH6q4pREewTIymfv9tmGxVe3f8zrKGR5litvN6OnZuWJdq57Y1lN
J1sdpMxTtxQQmexsADif+QByCvdeFKE5C3veMLdgS5I6HTMN9k5laA==
-----END RSA PRIVATE KEY-----
"""


def _spec_material():
    key = serialization.load_pem_private_key(_SPEC_PEM.encode(), password=None)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PU SPEC")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PU SPEC")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    raw = pkcs12.serialize_key_and_certificates(
        name=b"spec", key=key, cert=cert, cas=None, encryption_algorithm=NoEncryption()
    )
    return load_pkcs12(raw, None)


def _self_signed_p12() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PU TEST")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PU TEST")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"pu-test", key=key, cert=cert, cas=None, encryption_algorithm=NoEncryption()
    )


def _tenant() -> Tenant:
    import json

    return Tenant(
        id=1,
        slug="prostudio",
        name="PROSTUDIO.ME DOO",
        pib="03462668",
        status="active",
        mode="test",
        settings_json=json.dumps(
            {
                "busin_unit_code": "ps000bu001",
                "tcr_code": "ps000cr001",
                "soft_code": "sepko00001",
                "operator_code": "op00000011",
                "is_issuer_in_vat": True,
                "fiscal_channel": "poreska",
            }
        ),
    )


def _req() -> FiscalizeRequest:
    return FiscalizeRequest(
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


def _settings() -> Settings:
    return Settings(
        secret_key="test-secret-key-for-unit",
        partner_mode="poreska",
        cis_test_url="https://efitest.example/fs-v1/FiscalizationService",
        cis_prod_url="https://efi.example/fs-v1/FiscalizationService",
    )


def test_pu_iic_plain_matches_spec_join():
    when = datetime.fromisoformat("2019-06-12T17:05:43+02:00")
    plain = build_pu_iic_plain(
        tin="12345678",
        issue_datetime=when,
        inv_ord_num=9952,
        busin_unit_code="bb123bb123",
        tcr_code="cc123cc123",
        soft_code="ss123ss123",
        tot_price=Decimal("99.01"),
    )
    assert plain == "12345678|2019-06-12T17:05:43+02:00|9952|bb123bb123|cc123cc123|ss123ss123|99.01"


def test_pu_iic_md5_of_rsa_matches_spec():
    material = _spec_material()
    plain = "12345678|2019-06-12T17:05:43+02:00|9952|bb123bb123|cc123cc123|ss123ss123|99.01"
    iic, sig = sign_pu_iic(material, plain)
    assert iic == "E4033D471FEEA47A3C664B15C669C709"
    assert sig.isupper()
    assert all(c in "0123456789ABCDEF" for c in sig)


def test_cis_url_follows_tenant_mode():
    settings = _settings()
    assert "efitest" in cis_service_url(settings, _tenant())
    prod = _tenant()
    prod.mode = "prod"
    assert "efi.example" in cis_service_url(settings, prod)


def test_register_invoice_and_xmldsig():
    tenant = _tenant()
    fiscal = load_tenant_fiscal(tenant)
    material = load_pkcs12(_self_signed_p12(), None)
    el = build_register_invoice_request(
        tenant,
        _req(),
        inv_num="ps000bu001/1/2026/ps000cr001",
        inv_ord_num=1,
        fiscal=fiscal,
        iic="4AD5A215BEAF85B0416235736A6DACAB",
        iic_signature="AA" * 256,
        request_uuid="8d216f9a-55bb-445a-be32-30137f11b964",
    )
    assert etree.QName(el).localname == "RegisterInvoiceRequest"
    assert el.get("Id") == "Request"
    inv = el.find(f"{{{EFI_NS}}}Invoice")
    assert inv is not None
    assert inv.get("TypeOfInv") == "NONCASH"
    assert inv.get("IICSignature") == "AA" * 256
    pay = inv.find(f"{{{EFI_NS}}}PayMethods/{{{EFI_NS}}}PayMethod")
    assert pay.get("Type") == "ORDER"
    sign_enveloped(el, material)
    sig = el.find(f"{{{DSIG_NS}}}Signature")
    assert sig is not None
    assert sig.find(f"{{{DSIG_NS}}}SignedInfo/{{{DSIG_NS}}}Reference") is not None
    assert sig.find(f"{{{DSIG_NS}}}KeyInfo/{{{DSIG_NS}}}X509Data/{{{DSIG_NS}}}X509Certificate") is not None
    soap = wrap_soap(el)
    assert "SOAP-ENV:Envelope" in soap or "Envelope" in soap
    assert "Signature" in soap


def test_pu_fiscalize_requires_cert():
    adapter = PuPartnerAdapter(_settings())
    result = adapter.fiscalize(_tenant(), _req(), inv_ord_num=1)
    assert not result.ok
    assert "certifikat" in (result.error_message or "").lower() or "CERT" in (result.error_message or "")


def test_pu_fiscalize_reads_fic():
    adapter = PuPartnerAdapter(_settings())
    soap = """<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <ns2:RegisterInvoiceResponse Id="Response" Version="1" xmlns:ns2="https://efi.tax.gov.me/fs/schema">
      <ns2:Header UUID="resp-1"/>
      <ns2:FIC>a592e7ec-9517-4f02-8d54-ac965f679a8c</ns2:FIC>
    </ns2:RegisterInvoiceResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = soap
    mock_resp.content = soap.encode()

    with patch("sepko.pu.adapter.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = mock_resp
        client_cls.return_value = client
        with patch(
            "sepko.pu.adapter.require_tenant_cert",
            return_value=load_pkcs12(_self_signed_p12(), None),
        ):
            result = adapter.fiscalize(_tenant(), _req(), inv_ord_num=1)

    assert result.ok
    assert result.jikr == "a592e7ec-9517-4f02-8d54-ac965f679a8c"
    assert result.ikof
    posted = client.post.call_args.kwargs["content"].decode("utf-8")
    assert "RegisterInvoiceRequest" in posted
    assert "Signature" in posted
    assert "IICSignature" in posted
