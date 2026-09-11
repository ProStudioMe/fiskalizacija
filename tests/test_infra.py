from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import NoEncryption, pkcs12
from cryptography.x509.oid import NameOID

from sepko.db import async_database_url, sync_database_url
from sepko.escpos import EscPosOptions, build_receipt
from sepko.signing import (
    build_iic_plain,
    canonical_json,
    iic_from_plain,
    inspect_pkcs12,
    load_pkcs12,
    sign_iic,
    sign_json,
    sign_xml,
)


def test_database_url_psycopg_to_asyncpg():
    assert (
        async_database_url("postgresql+psycopg://sepko:sepko@localhost:5433/sepko")
        == "postgresql+asyncpg://sepko:sepko@localhost:5433/sepko"
    )


def test_database_url_asyncpg_to_psycopg():
    assert (
        sync_database_url("postgresql+asyncpg://sepko:sepko@localhost:5433/sepko")
        == "postgresql+psycopg://sepko:sepko@localhost:5433/sepko"
    )


def test_sqlite_async_url():
    assert async_database_url("sqlite:///./data/sepko.db") == "sqlite+aiosqlite:///./data/sepko.db"
    assert sync_database_url("sqlite+aiosqlite:///./data/sepko.db") == "sqlite:///./data/sepko.db"


def _self_signed_p12() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SEPKO TEST")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"sepko-test",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=NoEncryption(),
    )


def test_pkcs12_sign_json_and_iic():
    raw = _self_signed_p12()
    meta = inspect_pkcs12(raw, None)
    assert "SEPKO TEST" in meta["subject"]
    material = load_pkcs12(raw, None)
    signed = sign_json(material, {"tin": "123", "tot": "10.00"})
    assert signed["signature"]
    assert signed["cert_fingerprint"] == material.fingerprint_sha256
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    when = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    plain = build_iic_plain(
        tin="02712345",
        issue_datetime=when,
        inv_num="bu1/1/2026/cr1",
        busin_unit_code="bu1",
        tcr_code="cr1",
        soft_code="sw1",
        tot_price=Decimal("12.50"),
    )
    iic, sig = sign_iic(material, plain)
    assert iic == iic_from_plain(plain)
    assert len(iic) == 32
    assert sig
    xml = sign_xml(material, "<Invoice></Invoice>")
    assert xml["digest_sha256"]


def test_escpos_receipt_contains_ikof():
    raw = build_receipt(
        seller_name="PHILIA DOO",
        seller_pib="12345678",
        inv_num="bu/1/2026/cr",
        issue_dt="01.03.2026 12:00:00",
        buyer_name="Kupac",
        lines=[{"name": "Usluga", "qty": "1", "gross": "10,00"}],
        total_gross=Decimal("10.00"),
        pay_label="Gotovina",
        ikof="ABC123",
        jikr="jid-1",
        qr_url="https://example.test/qr",
        options=EscPosOptions(paper_cut="partial", open_drawer="cash", is_cash=True),
    )
    assert b"PHILIA DOO" in raw
    assert b"IKOF: ABC123" in raw
    assert b"\x1dV\x01" in raw


def test_fernet_roundtrip(monkeypatch):
    monkeypatch.setenv("SEPKO_SECRET_KEY", "unit-test-secret-key-not-for-prod")
    from sepko.config import get_settings
    from sepko.crypto import decrypt_secret, encrypt_secret, is_encrypted

    get_settings.cache_clear()
    token = encrypt_secret("imap-pin-123")
    assert is_encrypted(token)
    assert decrypt_secret(token) == "imap-pin-123"
    get_settings.cache_clear()


def test_sql_ident_rejects_injection():
    import pytest

    from sepko.sqlsafe import safe_ident

    assert safe_ident("inv_num") == "inv_num"
    with pytest.raises(ValueError):
        safe_ident("inv_num; DROP TABLE invoices")
    with pytest.raises(ValueError):
        safe_ident("hello world")


_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_tenant_company_keeps_logo_filename():
    from sepko.efi import TenantCompany, load_tenant_company, save_tenant_company
    from sepko.models import Tenant

    tenant = Tenant(slug="x", name="Firma", pib="12345678")
    save_tenant_company(tenant, TenantCompany(address="Ulica 1", logo_filename="logo-ab.png"))
    loaded = load_tenant_company(tenant)
    assert loaded.logo_filename == "logo-ab.png"
    assert loaded.address == "Ulica 1"


def test_company_logo_data_uri(tmp_path, monkeypatch):
    monkeypatch.setattr("sepko.uploads.uploads_root", lambda: tmp_path)
    from sepko.uploads import company_logo_dir, image_data_uri, resolve_company_logo

    fname = "logo-test.png"
    (company_logo_dir(1) / fname).write_bytes(_PNG_1X1)
    path = resolve_company_logo(1, fname)
    assert path is not None
    uri = image_data_uri(path)
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
