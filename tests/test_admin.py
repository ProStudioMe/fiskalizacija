from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.db import Base, get_db
from sepko.i18n import ensure_translations, export_language_map, import_language_map
from sepko.licenses import default_license_period, license_alert, license_days_left, license_label
from sepko.main import app
from sepko.models import AdminAuditLog, Tenant, TranslationKey, User
from sepko.web_auth import hash_password


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf_token missing"
    return m.group(1)


@pytest.fixture()
def admin_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    ensure_translations(db)
    today = date.today()
    tenant = Tenant(
        slug="philia",
        name="PHILIA DOO",
        pib="12345678",
        status="active",
        mode="test",
        license_type="yearly",
        license_from=today,
        license_until=today + timedelta(days=365),
    )
    db.add(tenant)
    db.flush()
    db.add(
        User(
            tenant_id=tenant.id,
            email="admin@philia.me",
            password_hash=hash_password("sepko123"),
            full_name="Philia Admin",
            role="admin",
            active=True,
        )
    )
    db.add(
        User(
            tenant_id=None,
            email="super@sepko.me",
            password_hash=hash_password("sepko-super"),
            full_name="Super",
            role="superadmin",
            active=True,
        )
    )
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
        yield client, TestingSession
    app.dependency_overrides.clear()


def _admin_login(client: TestClient) -> None:
    page = client.get("/admin/login")
    assert page.status_code == 200
    r = client.post(
        "/admin/login",
        data={"email": "super@sepko.me", "password": "sepko-super", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/admin" in r.headers.get("location", "")


def test_license_helpers():
    today = date(2026, 8, 29)
    t = Tenant(slug="x", name="X", pib="1", license_until=today + timedelta(days=10))
    assert license_days_left(t.license_until, today=today) == 10
    assert license_alert(t, today=today) == "expiring"
    t.license_until = today - timedelta(days=1)
    assert license_alert(t, today=today) == "expired"
    t.license_until = today + timedelta(days=90)
    assert license_alert(t, today=today) is None
    assert license_label("yearly") == "Godišnja"
    start, until = default_license_period("trial", today=today)
    assert start == today
    assert until == today + timedelta(days=30)


def test_admin_login_and_tenant_list(admin_client):
    client, _ = admin_client
    _admin_login(client)
    r = client.get("/admin/tenanti")
    assert r.status_code == 200
    assert "PHILIA DOO" in r.text
    assert "12345678" in r.text


def test_tenant_user_cannot_open_admin(admin_client):
    client, _ = admin_client
    page = client.get("/login")
    client.post(
        "/login",
        data={"email": "admin@philia.me", "password": "sepko123", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    r = client.get("/admin/tenanti", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location", "").endswith("/admin/login")


def test_superadmin_cannot_open_tenant_dashboard(admin_client):
    client, _ = admin_client
    _admin_login(client)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    assert loc in ("/login", "/admin")
    if loc == "/login":
        r2 = client.get("/login", follow_redirects=False)
        assert r2.status_code == 303
        assert "/admin" in r2.headers.get("location", "")


def test_create_tenant_and_suspend(admin_client):
    client, Session = admin_client
    _admin_login(client)
    page = client.get("/admin/tenanti/novi")
    token = _csrf(page.text)
    r = client.post(
        "/admin/tenanti",
        data={
            "csrf_token": token,
            "name": "Nova Firma DOO",
            "pib": "03452668",
            "slug": "novafirma",
            "mode": "test",
            "license_type": "monthly",
            "license_from": "2026-08-01",
            "license_until": "2026-09-01",
            "admin_email": "admin@nova.me",
            "admin_password": "lozinka12",
            "admin_name": "Nova Admin",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = Session()
    created = db.query(Tenant).filter(Tenant.slug == "novafirma").first()
    assert created is not None
    assert created.license_type == "monthly"
    admin_u = db.query(User).filter(User.email == "admin@nova.me").first()
    assert admin_u is not None and admin_u.tenant_id == created.id
    audit = db.query(AdminAuditLog).filter(AdminAuditLog.action == "tenant.create").first()
    assert audit is not None
    db.close()

    detail = client.get(f"/admin/tenanti/{created.id}")
    token = _csrf(detail.text)
    r = client.post(
        f"/admin/tenanti/{created.id}/status",
        data={"csrf_token": token, "status": "suspended"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = Session()
    assert db.get(Tenant, created.id).status == "suspended"
    db.close()

    page = client.get("/login")
    r = client.post(
        "/login",
        data={"email": "admin@nova.me", "password": "lozinka12", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location", "").endswith("/login")


def test_translation_save_and_export(admin_client):
    client, Session = admin_client
    _admin_login(client)
    page = client.get("/admin/prevodi?lang=en")
    assert page.status_code == 200
    token = _csrf(page.text)
    r = client.post(
        "/admin/prevodi",
        data={
            "csrf_token": token,
            "lang": "en",
            "q": "",
            "keys": ["nav.pregled"],
            "values": ["Overview-edited"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = Session()
    exported = export_language_map(db, "en")
    assert exported.get("nav.pregled") == "Overview-edited"
    n = import_language_map(db, "en", {"nav.pregled": "Overview-imported"})
    db.commit()
    assert n == 1
    assert db.get(TranslationKey, "nav.pregled") is not None
    db.close()

    exp = client.get("/admin/prevodi/export?lang=en")
    assert exp.status_code == 200
    payload = json.loads(exp.text)
    assert payload["language"] == "en"
    assert "nav.pregled" in payload["translations"]
