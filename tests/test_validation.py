from __future__ import annotations

import json
from pathlib import Path

from sepko.schemas import FiscalizeRequest
from sepko.services import validate_totals

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "philia_26-010-000354.json"


def test_philia_totals_valid():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    req = FiscalizeRequest.model_validate(payload)
    assert validate_totals(req) is None
