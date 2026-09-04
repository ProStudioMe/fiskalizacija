from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from sepko.efi import normalize_inv_type, normalize_pay_method, normalize_type_of_inv

_MAX_MONEY = Decimal("10000000")
_CREDIT_TYPES = frozenset({"CREDIT_NOTE", "CORRECTIVE"})


class BuyerIn(BaseModel):
    pib: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=512)


class InvoiceLineIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(max_digits=14, decimal_places=4)
    unit_price_net: Decimal = Field(max_digits=14, decimal_places=4)
    vat_rate: Decimal = Field(ge=0, le=100)
    total_gross: Decimal = Field(max_digits=14, decimal_places=4)


class TotalsIn(BaseModel):
    net: Decimal = Field(max_digits=14, decimal_places=2)
    vat: Decimal = Field(max_digits=14, decimal_places=2)
    gross: Decimal = Field(max_digits=14, decimal_places=2)


class FiscalizeRequest(BaseModel):
    """external_id je opciono — ako nedostaje, Sepko generiše InvNum {PJ}/{rbr}/{god}/{ENU}."""

    external_id: str | None = Field(default=None, max_length=64)
    issue_datetime: datetime
    invoice_type: str = "CASH"
    payment_method: str = "BANKNOTE"
    inv_type: str = "INVOICE"
    inv_ord_num: int | None = Field(default=None, ge=1, le=10_000_000)
    currency: str = Field(default="EUR", min_length=3, max_length=8)
    buyer: BuyerIn | None = None
    lines: list[InvoiceLineIn] = Field(min_length=1, max_length=500)
    totals: TotalsIn
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("invoice_type")
    @classmethod
    def _type_of_inv(cls, v: str) -> str:
        return normalize_type_of_inv(v)

    @field_validator("payment_method")
    @classmethod
    def _pay_method(cls, v: str) -> str:
        return normalize_pay_method(v)

    @field_validator("inv_type")
    @classmethod
    def _inv_type(cls, v: str) -> str:
        return normalize_inv_type(v)

    @field_validator("lines")
    @classmethod
    def lines_not_empty(cls, v: list[InvoiceLineIn]) -> list[InvoiceLineIn]:
        if not v:
            raise ValueError("at least one line required")
        return v

    @model_validator(mode="after")
    def amounts_sign_and_bounds(self) -> "FiscalizeRequest":
        allow_neg = self.inv_type in _CREDIT_TYPES
        amounts = [self.totals.net, self.totals.vat, self.totals.gross]
        for line in self.lines:
            amounts.extend([line.quantity, line.unit_price_net, line.total_gross])
            if line.quantity == 0:
                raise ValueError("quantity must not be 0")
            if line.quantity < 0 and not allow_neg:
                raise ValueError("quantity must be > 0")
        for amt in amounts:
            if abs(amt) > _MAX_MONEY:
                raise ValueError("amount exceeds maximum")
            if amt < 0 and not allow_neg:
                raise ValueError("negative amounts only allowed for CREDIT_NOTE/CORRECTIVE")
        return self


class FiscalizeResponse(BaseModel):
    external_id: str
    status: str
    ikof: str | None = None
    jikr: str | None = None
    qr_url: str | None = None
    partner_ref: str | None = None
    inv_num: str | None = None
    inv_ord_num: int | None = None
    fiscalized_at: datetime | None = None
    error_message: str | None = None


class CashDepositRequest(BaseModel):
    operation: str = "INITIAL"  # INITIAL | WITHDRAW
    amount: Decimal = Field(ge=0, le=Decimal("10000000"), max_digits=14, decimal_places=2)
    change_datetime: datetime | None = None

    @field_validator("operation")
    @classmethod
    def _op(cls, v: str) -> str:
        op = (v or "").strip().upper()
        if op not in ("INITIAL", "WITHDRAW"):
            raise ValueError("operation must be INITIAL or WITHDRAW")
        return op


class CashDepositResponse(BaseModel):
    id: int
    operation: str
    amount: Decimal
    status: str
    tcr_code: str
    change_datetime: datetime
    partner_ref: str | None = None
    error_message: str | None = None


class CashDaySummary(BaseModel):
    date: str
    initial: Decimal
    banknote_sales: Decimal
    withdrawals: Decimal
    cash_in_drawer: Decimal
    has_initial: bool
    invoice_count: int


class TenantOut(BaseModel):
    id: int
    slug: str
    name: str
    pib: str
    status: str
    mode: str

    model_config = {"from_attributes": True}


class FiscalCodesOut(BaseModel):
    busin_unit_code: str = ""
    tcr_code: str = ""
    soft_code: str = ""
    operator_code: str = ""
    is_issuer_in_vat: bool = True
    token_provider: str = ""
    has_telekom_token: bool = False
    has_posta_token: bool = False


class SettingsOut(BaseModel):
    tenant: TenantOut
    partner_mode: str
    webhook_url: str | None
    api_key_prefixes: list[str]
    fiscal: FiscalCodesOut


class HealthOut(BaseModel):
    status: str
    service: str
    version: str
    db: str = "ok"
