from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.auth import generate_api_key
from sepko.db import Base, get_db
from sepko.efi import TenantFiscal, save_tenant_fiscal
from sepko.main import app
from sepko.models import ApiKey, Tenant
from sepko.schemas import CashDepositRequest, FiscalizeRequest, InvoiceLineIn, TotalsIn
from sepko.services import cash_day_summary, fiscalize_invoice, preview_inv_num, register_cash_deposit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "philia_26-010-000354.json"


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


def test_preview_inv_num_format(db_session):
    db, tenant = db_session
    inv_num, ord_num = preview_inv_num(db, tenant)
    year = datetime.now(timezone.utc).year
    assert ord_num == 1
    assert inv_num == f"ph000bu001/1/{year}/ph000cr001"


def test_auto_external_id_from_inv_num(db_session):
    db, tenant = db_session
    req = FiscalizeRequest(
        issue_datetime=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        invoice_type="CASH",
        payment_method="BANKNOTE",
        lines=[
            InvoiceLineIn(
                code="A1",
                name="Test",
                quantity=1,
                unit_price_net=Decimal("10"),
                vat_rate=Decimal("21"),
                total_gross=Decimal("12.10"),
            )
        ],
        totals=TotalsIn(net=Decimal("10"), vat=Decimal("2.10"), gross=Decimal("12.10")),
    )
    result = fiscalize_invoice(db, tenant, req)
    assert result.status == "fiscalized"
    assert result.inv_num == result.external_id
    assert result.inv_num.startswith("ph000bu001/")
    assert "/2026/" in result.inv_num
    assert result.inv_num.endswith("/ph000cr001")


def test_cash_deposit_initial_and_summary(db_session):
    db, tenant = db_session
    r = register_cash_deposit(
        db,
        tenant,
        CashDepositRequest(operation="INITIAL", amount=Decimal("50.00")),
    )
    assert r.status == "registered"
    assert r.operation == "INITIAL"
    summary = cash_day_summary(db, tenant)
    assert summary.has_initial
    assert summary.initial == Decimal("50.00")
    assert summary.cash_in_drawer == Decimal("50.00")

    w = register_cash_deposit(
        db,
        tenant,
        CashDepositRequest(operation="WITHDRAW", amount=Decimal("10.00")),
    )
    assert w.status == "registered"
    summary2 = cash_day_summary(db, tenant)
    assert summary2.withdrawals == Decimal("10.00")
    assert summary2.cash_in_drawer == Decimal("40.00")
