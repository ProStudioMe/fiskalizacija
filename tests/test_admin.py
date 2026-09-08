from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.db import Base, get_db
from sepko.i18n import ensure_translations, export_language_map, import_language_map
from sepko.licenses import add_months, default_license_period, license_alert, license_days_left, license_extend_until, license_label
from sepko.main import app
from sepko.models import AdminAuditLog, Invoice, InvoiceStatus, LicenseInvoice, Tenant, TranslationKey, User
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
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    t2 = Tenant(slug="y", name="Y", pib="2", license_until=today + timedelta(days=5))
    assert license_extend_until(t2.license_until, 6, today=today) == add_months(today + timedelta(days=5), 6)


def test_admin_login_and_tenant_list(admin_client):
    client, _ = admin_client
    _admin_login(client)
    r = client.get("/admin/tenanti")
    assert r.status_code == 200
    assert "PHILIA DOO" in r.text
    assert "12345678" in r.text


def test_pwa_install_prompt_skipped_for_admin(admin_client):
    client, Session = admin_client
    _admin_login(client)
    r = client.get("/admin/tenanti")
    assert r.status_code == 200
    assert 'data-skip="1"' in r.text
    assert 'id="btn-pwa-install"' not in r.text

    client.post("/admin/logout", data={"csrf_token": _csrf(r.text)}, follow_redirects=False)
    page = client.get("/login")
    client.post(
        "/login",
        data={"email": "admin@philia.me", "password": "sepko123", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    dash = client.get("/")
    assert dash.status_code == 200
    assert 'data-skip="1"' in dash.text
    assert 'id="btn-pwa-install"' not in dash.text

    db = Session()
    tenant = db.query(Tenant).filter_by(slug="philia").one()
    db.add(
        User(
            tenant_id=tenant.id,
            email="kasir@philia.me",
            password_hash=hash_password("sepko123"),
            full_name="Kasir",
            role="kasir",
            active=True,
        )
    )
    db.commit()
    db.close()

    client.post("/logout", data={"csrf_token": _csrf(dash.text)}, follow_redirects=False)
    page = client.get("/login")
    client.post(
        "/login",
        data={"email": "kasir@philia.me", "password": "sepko123", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    kasir_page = client.get("/")
    assert kasir_page.status_code == 200
    assert 'data-skip="0"' in kasir_page.text
    assert 'id="btn-pwa-install"' in kasir_page.text


def test_unfiscalized_invoice_opens_editor(admin_client):
    client, Session = admin_client
    page = client.get("/login")
    client.post(
        "/login",
        data={"email": "admin@philia.me", "password": "sepko123", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    db = Session()
    tenant = db.query(Tenant).filter_by(slug="philia").one()
    draft = Invoice(
        tenant_id=tenant.id,
        external_id="draft-view-edit",
        status=InvoiceStatus.draft.value,
        invoice_type="NONCASH",
        payment_method="ORDER",
        currency="EUR",
        issue_datetime=datetime.now(timezone.utc),
        total_net=Decimal("10.00"),
        total_vat=Decimal("2.10"),
        total_gross=Decimal("12.10"),
        payload_json="{}",
    )
    done = Invoice(
        tenant_id=tenant.id,
        external_id="fisc-view-keep",
        status=InvoiceStatus.fiscalized.value,
        invoice_type="NONCASH",
        payment_method="ORDER",
        currency="EUR",
        issue_datetime=datetime.now(timezone.utc),
        total_net=Decimal("10.00"),
        total_vat=Decimal("2.10"),
        total_gross=Decimal("12.10"),
        payload_json="{}",
        ikof="IKOF",
        jikr="JIKR",
    )
    db.add_all([draft, done])
    db.commit()
    draft_id, done_id = draft.id, done.id
    db.close()

    to_edit = client.get(f"/racuni/{draft_id}", follow_redirects=False)
    assert to_edit.status_code == 303
    assert to_edit.headers.get("location", "").endswith(f"/racuni/{draft_id}/izmijeni")

    combined = client.get(f"/racuni/{draft_id}/izmijeni")
    assert combined.status_code == 200
    assert "Pregled fakture" in combined.text
    assert "Nefiskalizovan" in combined.text
    assert "Izmjena fakture" not in combined.text
    assert f"/racuni/{draft_id}/stampa" in combined.text
    assert f"/racuni/{draft_id}/pdf" in combined.text
    assert "Fiskalni podaci" in combined.text
    assert 'id="invoice-form"' in combined.text

    keep_view = client.get(f"/racuni/{done_id}", follow_redirects=False)
    assert keep_view.status_code == 200
    assert "Pregled fakture" in keep_view.text

    listing = client.get("/racuni")
    assert listing.status_code == 200
    assert f'data-href="/racuni/{draft_id}/izmijeni"' in listing.text
    assert f'data-href="/racuni/{done_id}" tabindex' in listing.text


def test_login_shows_proracun_and_prostudio(admin_client):
    client, _ = admin_client
    page = client.get("/login")
    assert page.status_code == 200
    assert "ProRačun" in page.text
    assert "prostudio.me" in page.text
    assert "https://moj.proracun.me/login" in page.text


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


def test_tenant_detail_has_license_invoice_form(admin_client):
    client, Session = admin_client
    _admin_login(client)
    db = Session()
    tenant = db.query(Tenant).filter(Tenant.slug == "philia").one()
    tid = tenant.id
    db.close()
    r = client.get(f"/admin/tenanti/{tid}")
    assert r.status_code == 200
    assert "Račun / profaktura za licencu" in r.text
    assert "admin@philia.me" in r.text
    assert 'action="/admin/tenanti/' in r.text


def test_send_license_invoice_extends_and_audits(admin_client, monkeypatch):
    client, Session = admin_client
    monkeypatch.setattr(
        "sepko.license_invoice.send_platform_mail",
        lambda **_kw: {"ok": True, "message": "Poslato", "provider": "resend", "id": "re_test"},
    )
    _admin_login(client)
    db = Session()
    tenant = db.query(Tenant).filter(Tenant.slug == "philia").one()
    tid = tenant.id
    old_until = tenant.license_until
    db.close()
    page = client.get(f"/admin/tenanti/{tid}")
    r = client.post(
        f"/admin/tenanti/{tid}/licenca/racun",
        data={
            "csrf_token": _csrf(page.text),
            "doc_kind": "invoice",
            "to_email": "admin@philia.me",
            "number": "01-26-0001",
            "issue_date": "2026-09-07",
            "due_date": "2026-09-14",
            "months": "6",
            "amount": "90,00",
            "item_name": "ProRačun Basic WEB",
            "payment_method": "Virman",
            "iban": "CKB: 510-1",
            "message": "Poštovani, test.",
            "extend_license": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = Session()
    row = db.query(LicenseInvoice).filter(LicenseInvoice.number == "01-26-0001").one()
    assert row.status == "sent"
    assert row.kind == "invoice"
    assert str(row.to_email) == "admin@philia.me"
    tenant = db.get(Tenant, tid)
    assert tenant.license_until > old_until
    audit = db.query(AdminAuditLog).filter(AdminAuditLog.action == "license.invoice_send").first()
    assert audit is not None
    db.close()


def test_daily_proforma_for_expiring_paid_license(admin_client, monkeypatch):
    from sepko.license_invoice import run_due_license_proformas

    sent: list[dict] = []

    def fake_send(**kw):
        sent.append(kw)
        return {"ok": True, "message": "Poslato", "provider": "resend", "id": "re_pf"}

    monkeypatch.setattr("sepko.license_invoice.send_platform_mail", fake_send)
    _client, Session = admin_client
    db = Session()
    today = date.today()
    tenant = db.query(Tenant).filter(Tenant.slug == "philia").one()
    tenant.license_type = "yearly"
    tenant.license_until = today + timedelta(days=10)
    db.commit()

    first = run_due_license_proformas(db, today=today)
    assert first["ok"] == 1
    assert len(sent) == 1
    assert "Profaktura" in (sent[0].get("subject") or "")
    html = sent[0].get("body_html") or ""
    assert "Profaktura" in html
    assert "PROSTUDIO.ME DOO" in html

    second = run_due_license_proformas(db, today=today)
    assert second["ok"] == 0
    assert len(sent) == 1

    trial = Tenant(
        slug="trial-x",
        name="Trial X",
        pib="11111111",
        status="trial",
        mode="test",
        license_type="trial",
        license_from=today,
        license_until=today + timedelta(days=5),
    )
    db.add(trial)
    db.flush()
    db.add(
        User(
            tenant_id=trial.id,
            email="trial@x.me",
            password_hash=hash_password("sepko12345"),
            full_name="Trial",
            role="admin",
            active=True,
        )
    )
    db.commit()
    third = run_due_license_proformas(db, today=today)
    assert third["ok"] == 0
    assert len(sent) == 1
    db.close()

