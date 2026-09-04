from __future__ import annotations

import json
from pathlib import Path

from sepko.efi import normalize_pay_method, normalize_type_of_inv
from sepko.schemas import FiscalizeRequest
from sepko.services import validate_totals

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "philia_26-010-000354.json"


def test_aliases_type_and_pay():
    assert normalize_type_of_inv("cash") == "CASH"
    assert normalize_type_of_inv("non_cash") == "NONCASH"
    assert normalize_pay_method("transfer") == "ORDER"
    assert normalize_pay_method("card") == "CARD"


def test_philia_fixture_normalizes_to_efi():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    req = FiscalizeRequest.model_validate(payload)
    assert req.invoice_type == "NONCASH"
    assert req.payment_method == "ORDER"
    assert req.inv_type == "INVOICE"
    assert validate_totals(req) is None


def test_cash_cannot_use_order():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["invoice_type"] = "CASH"
    payload["payment_method"] = "ORDER"
    req = FiscalizeRequest.model_validate(payload)
    err = validate_totals(req)
    assert err and "BANKNOTE" in err


def test_noncash_rejects_card():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["invoice_type"] = "NONCASH"
    payload["payment_method"] = "CARD"
    req = FiscalizeRequest.model_validate(payload)
    err = validate_totals(req)
    assert err and "NONCASH" in err
