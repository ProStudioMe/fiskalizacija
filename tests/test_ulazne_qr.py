"""QR import + bank statement parse unit tests."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.db import Base
from sepko.finansije import parse_tx_lines_from_text
from sepko.models import Customer, IncomingInvoice, Supplier, Tenant
from sepko.qr_import import parse_verify_url
from sepko.ulazne import create_incoming_invoice, merge_suppliers_into_customers


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


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    tenant = Tenant(slug="t1", name="Test DOO", pib="12345678", status="active", mode="test")
    db.add(tenant)
    db.commit()
    yield db, tenant
    db.close()


def test_create_incoming_via_customer_id(db_session):
    db, tenant = db_session
    cust = Customer(tenant_id=tenant.id, pib="87654321", name="Komitent DOO", active=True)
    db.add(cust)
    db.flush()
    inv = create_incoming_invoice(
        db,
        tenant,
        number="UF-1",
        customer_id=cust.id,
        total_gross=Decimal("121.00"),
    )
    db.commit()
    assert inv.customer_id == cust.id
    assert inv.supplier_id is None
    assert inv.supplier_pib == "87654321"
    assert inv.supplier_name == "Komitent DOO"


def test_merge_suppliers_into_customers(db_session):
    db, tenant = db_session
    supplier = Supplier(
        tenant_id=tenant.id,
        pib="11112222",
        name="Stari dobavljač",
        email="d@example.com",
        active=True,
    )
    db.add(supplier)
    db.flush()
    inv = IncomingInvoice(
        tenant_id=tenant.id,
        supplier_id=supplier.id,
        customer_id=None,
        number="UF-old",
        supplier_pib=supplier.pib,
        supplier_name=supplier.name,
        total_gross=Decimal("10.00"),
    )
    db.add(inv)
    db.flush()
    inv_id = inv.id

    merge_suppliers_into_customers(db)
    db.commit()

    cust = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.pib == "11112222")
        .one()
    )
    linked = db.get(IncomingInvoice, inv_id)
    assert linked is not None
    assert cust.name == "Stari dobavljač"
    assert cust.email == "d@example.com"
    assert linked.customer_id == cust.id
