from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.db import Base
from sepko.efi import TenantFiscal, save_tenant_fiscal
from sepko.models import Invoice, InvoiceLine, InvoiceStatus, Tenant
from sepko.services import copy_invoices


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
    tenant = Tenant(slug="philia", name="PHILIA DOO", pib="12345678", status="active", mode="test")
    db.add(tenant)
    db.flush()
    save_tenant_fiscal(
        tenant,
        TenantFiscal(
            busin_unit_code="ph000bu001",
            tcr_code="ph000cr001",
            soft_code="sepko00001",
            operator_code="op00000001",
            is_issuer_in_vat=True,
        ),
    )
    db.commit()
    yield db, tenant
    db.close()


def test_copy_invoices_creates_drafts(db_session):
    db, tenant = db_session
    src = Invoice(
        tenant_id=tenant.id,
        external_id="src-1",
        status=InvoiceStatus.fiscalized.value,
        invoice_type="NONCASH",
        payment_method="ORDER",
        currency="EUR",
        issue_datetime=datetime.now(timezone.utc),
        buyer_pib="03123456",
        buyer_name="Tekport d.o.o.",
        buyer_address="Podgorica",
        notes="Demo",
        total_net=Decimal("100.00"),
        total_vat=Decimal("21.00"),
        total_gross=Decimal("121.00"),
        payload_json="{}",
        inv_num="ph000bu001/1/2026/ph000cr001",
        inv_ord_num=1,
        ikof="IKOF-TEST",
        jikr="JIKR-TEST",
    )
    src.lines.append(
        InvoiceLine(
            code="2",
            name="Usluga",
            quantity=Decimal("1"),
            unit_price_net=Decimal("100"),
            vat_rate=Decimal("21"),
            total_gross=Decimal("121.00"),
        )
    )
    db.add(src)
    db.commit()
    db.refresh(src)

    copies = copy_invoices(db, tenant, [src.id])
    assert len(copies) == 1
    copy = copies[0]
    assert copy.id != src.id
    assert copy.status == InvoiceStatus.draft.value
    assert copy.buyer_name == "Tekport d.o.o."
    assert copy.buyer_pib == "03123456"
    assert copy.total_gross == Decimal("121.00")
    assert copy.ikof is None
    assert copy.jikr is None
    assert copy.inv_num is None
    assert len(copy.lines) == 1
    assert copy.lines[0].name == "Usluga"


def test_copy_invoices_bulk(db_session):
    db, tenant = db_session
    ids = []
    for i in range(3):
        inv = Invoice(
            tenant_id=tenant.id,
            external_id=f"bulk-{i}",
            status=InvoiceStatus.fiscalized.value,
            invoice_type="NONCASH",
            payment_method="ORDER",
            currency="EUR",
            issue_datetime=datetime.now(timezone.utc),
            buyer_name=f"Kupac {i}",
            total_net=Decimal("10.00"),
            total_vat=Decimal("2.10"),
            total_gross=Decimal("12.10"),
            payload_json="{}",
            inv_ord_num=i + 1,
        )
        inv.lines.append(
            InvoiceLine(
                code=str(i),
                name=f"Art {i}",
                quantity=Decimal("1"),
                unit_price_net=Decimal("10"),
                vat_rate=Decimal("21"),
                total_gross=Decimal("12.10"),
            )
        )
        db.add(inv)
        db.flush()
        ids.append(inv.id)
    db.commit()

    copies = copy_invoices(db, tenant, ids)
    assert len(copies) == 3
    assert all(c.status == InvoiceStatus.draft.value for c in copies)
