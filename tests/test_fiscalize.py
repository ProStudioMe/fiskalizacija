from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.auth import generate_api_key
from sepko.db import Base, get_db
from sepko.main import app
from sepko.models import ApiKey, Tenant

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "philia_26-010-000354.json"


@pytest.fixture()
def client_and_key():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    db = TestingSession()
    tenant = Tenant(slug="philia", name="PHILIA DOO", pib="TBD", status="active", mode="test")
    db.add(tenant)
    db.flush()
    raw, key_hash, prefix = generate_api_key()
    db.add(ApiKey(tenant_id=tenant.id, name="test", key_hash=key_hash, key_prefix=prefix))
    db.commit()
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client, raw
    app.dependency_overrides.clear()


def test_health(client_and_key):
    client, _ = client_and_key
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "sepko"


def test_fiscalize_philia_fixture(client_and_key):
    client, api_key = client_and_key
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    r = client.post(
        "/v1/invoices/fiscalize",
        json=payload,
        headers={"X-Api-Key": api_key},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["external_id"] == "26-010-000354"
    assert data["status"] == "fiscalized"
    assert data["jikr"]
    assert data["ikof"]
    assert data["qr_url"]


def test_fiscalize_idempotent(client_and_key):
    client, api_key = client_and_key
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    headers = {"X-Api-Key": api_key}
    first = client.post("/v1/invoices/fiscalize", json=payload, headers=headers).json()
    second = client.post("/v1/invoices/fiscalize", json=payload, headers=headers).json()
    assert first["jikr"] == second["jikr"]
    assert first["ikof"] == second["ikof"]


def test_settings(client_and_key):
    client, api_key = client_and_key
    r = client.get("/v1/settings", headers={"X-Api-Key": api_key})
    assert r.status_code == 200
    assert r.json()["tenant"]["slug"] == "philia"
    assert "fiscal" in r.json()


def test_fiscalize_qr_is_efi_shaped(client_and_key):
    from urllib.parse import parse_qs, urlparse

    client, api_key = client_and_key
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    r = client.post(
        "/v1/invoices/fiscalize",
        json=payload,
        headers={"X-Api-Key": api_key},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    parsed = urlparse(data["qr_url"])
    assert parsed.path.endswith("/ic/") or "/ic/" in data["qr_url"]
    assert "efitest.tax.gov.me" in data["qr_url"]
    fragment = data["qr_url"].split("#", 1)[-1]
    qs = parse_qs(fragment.split("?", 1)[-1])
    assert qs["iic"][0] == data["ikof"]
    assert "tin" in qs
    assert "ord" in qs
    assert "prc" in qs
    assert data["inv_num"]
    assert data["inv_ord_num"] == 1
