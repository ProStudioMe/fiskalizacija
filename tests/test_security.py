from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sepko.config import Settings
from sepko.security_middleware import SecurityHeadersMiddleware
from sepko.web_auth import hash_password, verify_password
from sepko.web_security import ensure_csrf, validate_csrf


class DummySession(dict):
    pass


class DummyRequest:
    def __init__(self, session: DummySession | None = None):
        self.session = session or DummySession()


def test_production_rejects_default_secret_key():
    with pytest.raises(ValidationError):
        Settings(env="production", secret_key="change-me-in-production")


def test_password_hash_roundtrip():
    stored = hash_password("sepko123")
    assert verify_password("sepko123", stored)
    assert not verify_password("wrong", stored)


def test_csrf_validation():
    req = DummyRequest()
    token = ensure_csrf(req)  # type: ignore[arg-type]
    assert validate_csrf(req, token)  # type: ignore[arg-type]
    assert not validate_csrf(req, "bad-token")  # type: ignore[arg-type]


async def _ok(_request):
    return PlainTextResponse("ok")


def test_security_headers_middleware():
    app = Starlette(
        routes=[Route("/", _ok)],
        middleware=[Middleware(SecurityHeadersMiddleware, env="development")],
    )
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "Content-Security-Policy" in response.headers
    assert "frame-ancestors 'self'" in response.headers.get("Content-Security-Policy", "")


def test_negative_price_rejected_on_normal_invoice():
    from datetime import datetime, timezone
    from decimal import Decimal

    from pydantic import ValidationError

    from sepko.schemas import BuyerIn, FiscalizeRequest, InvoiceLineIn, TotalsIn

    with pytest.raises(ValidationError):
        FiscalizeRequest(
            issue_datetime=datetime.now(timezone.utc),
            invoice_type="CASH",
            payment_method="BANKNOTE",
            lines=[
                InvoiceLineIn(
                    code="X",
                    name="X",
                    quantity=Decimal("1"),
                    unit_price_net=Decimal("-10"),
                    vat_rate=Decimal("21"),
                    total_gross=Decimal("-10"),
                )
            ],
            totals=TotalsIn(net=Decimal("-10"), vat=Decimal("0"), gross=Decimal("-10")),
        )


def test_credit_note_allows_negative_amount():
    from datetime import datetime, timezone
    from decimal import Decimal

    from sepko.schemas import FiscalizeRequest, InvoiceLineIn, TotalsIn

    req = FiscalizeRequest(
        issue_datetime=datetime.now(timezone.utc),
        invoice_type="CASH",
        payment_method="BANKNOTE",
        inv_type="CREDIT_NOTE",
        lines=[
            InvoiceLineIn(
                code="X",
                name="Storno",
                quantity=Decimal("-1"),
                unit_price_net=Decimal("10"),
                vat_rate=Decimal("21"),
                total_gross=Decimal("-12.10"),
            )
        ],
        totals=TotalsIn(net=Decimal("-10"), vat=Decimal("-2.10"), gross=Decimal("-12.10")),
    )
    assert req.inv_type == "CREDIT_NOTE"


def test_csrf_origin_blocks_cross_site_post():
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from sepko.security_middleware import CsrfOriginMiddleware

    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/save", ok, methods=["POST"])],
        middleware=[Middleware(CsrfOriginMiddleware, enabled=True)],
    )
    client = TestClient(app)
    blocked = client.post("/save", headers={"origin": "https://evil.example", "host": "sepko.local"})
    assert blocked.status_code == 403
    allowed = client.post("/save", headers={"origin": "https://sepko.local", "host": "sepko.local"})
    assert allowed.status_code == 200


def test_normalize_imap_host():
    from sepko.finansije import normalize_imap_host

    assert normalize_imap_host("Mail.Example.COM") == "mail.example.com"
    assert normalize_imap_host("https://mail.example.com") is None
    assert normalize_imap_host("mail.example.com:993") is None
    assert normalize_imap_host("mail.example.com/inbox") is None


def test_imap_host_blocks_private(monkeypatch):
    from sepko.finansije import imap_host_is_safe

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(None, None, None, None, ("10.0.0.5", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    ok, msg = imap_host_is_safe("evil.internal")
    assert not ok
    assert "privatna" in msg.lower() or "lokalna" in msg.lower()


def test_imap_probe_rate_limit():
    from sepko.web_security import (
        _imap_probe_attempts,
        imap_probe_rate_limited,
        record_imap_probe,
    )

    class Req:
        def __init__(self):
            self.client = type("C", (), {"host": "203.0.113.9"})()
            self.headers = {}

    req = Req()
    _imap_probe_attempts.clear()
    for _ in range(8):
        assert not imap_probe_rate_limited(tenant_id=42, request=req)  # type: ignore[arg-type]
        record_imap_probe(tenant_id=42, request=req)  # type: ignore[arg-type]
    assert imap_probe_rate_limited(tenant_id=42, request=req)  # type: ignore[arg-type]
    _imap_probe_attempts.clear()
