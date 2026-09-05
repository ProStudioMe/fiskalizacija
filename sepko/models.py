from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sepko.db import Base


class TenantStatus(str, Enum):
    trial = "trial"
    active = "active"
    suspended = "suspended"


class InvoiceStatus(str, Enum):
    draft = "draft"
    pending = "pending"
    fiscalized = "fiscalized"
    failed = "failed"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    pib: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default=TenantStatus.active.value)
    mode: Mapped[str] = mapped_column(String(16), default="test")  # test | prod
    partner_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    settings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_type: Mapped[str] = mapped_column(String(32), default="trial")  # trial | monthly | yearly
    license_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    license_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="tenant")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="tenant")
    users: Mapped[list[User]] = relationship(back_populates="tenant")
    articles: Mapped[list[Article]] = relationship(back_populates="tenant")
    categories: Mapped[list[Category]] = relationship(back_populates="tenant")
    tax_rates: Mapped[list[TaxRate]] = relationship(back_populates="tenant")
    customers: Mapped[list[Customer]] = relationship(back_populates="tenant")
    suppliers: Mapped[list["Supplier"]] = relationship(back_populates="tenant")
    cash_deposits: Mapped[list[CashDeposit]] = relationship(back_populates="tenant")
    bank_statements: Mapped[list["BankStatement"]] = relationship(back_populates="tenant")
    bank_transactions: Mapped[list["BankTransaction"]] = relationship(back_populates="tenant")
    customer_payments: Mapped[list["CustomerPayment"]] = relationship(back_populates="tenant")
    invoice_schedules: Mapped[list["InvoiceSchedule"]] = relationship(back_populates="tenant")
    incoming_invoices: Mapped[list["IncomingInvoice"]] = relationship(back_populates="tenant")
    expense_categories: Mapped[list["ExpenseCategory"]] = relationship(back_populates="tenant")
    expenses: Mapped[list["Expense"]] = relationship(back_populates="tenant")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), default="admin")  # admin | kasir | superadmin
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant | None] = relationship(back_populates="users")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_category_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(default=1)
    color: Mapped[str] = mapped_column(String(16), default="#e65100")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="categories")
    articles: Mapped[list[Article]] = relationship(back_populates="category")


class TaxRate(Base):
    __tablename__ = "tax_rates"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_tenant_tax_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"))
    is_default: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)

    tenant: Mapped[Tenant] = relationship(back_populates="tax_rates")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_tenant_article_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(16), default="KOM")
    price_gross: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))  # VP neto (bez PDV)
    price_retail: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)  # MP
    stock_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True, default="#c62828")
    thumbnail_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("21"))
    tax_rate_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="articles")
    category: Mapped[Category | None] = relationship(back_populates="articles")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "pib", name="uq_tenant_customer_pib"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    pib: Mapped[str] = mapped_column(String(32))  # PIB ili JMBG
    pdv_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, default="Crna Gora")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tax_card_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)  # legacy / sastavljeno
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stalni popust na račun (%) — automatski se primjenjuje pri izboru komitenta
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="customers")
    payments: Mapped[list["CustomerPayment"]] = relationship(back_populates="customer")

    def composed_address(self) -> str | None:
        parts = [p for p in (self.street, self.city, self.country) if p]
        if parts:
            return ", ".join(parts)
        return self.address


class CashDeposit(Base):
    """EFI RegisterCashDeposit — INITIAL na početku dana, WITHDRAW tokom dana."""

    __tablename__ = "cash_deposits"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    operation: Mapped[str] = mapped_column(String(16))  # INITIAL | WITHDRAW
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    tcr_code: Mapped[str] = mapped_column(String(16), default="")
    operator_code: Mapped[str] = mapped_column(String(16), default="")
    change_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    partner_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="registered")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="cash_deposits")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "external_id", name="uq_tenant_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default=InvoiceStatus.draft.value)
    invoice_type: Mapped[str] = mapped_column(String(32))  # cash | non_cash
    payment_method: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    issue_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    buyer_pib: Mapped[str | None] = mapped_column(String(32), nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buyer_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_net: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    total_vat: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    total_gross: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payload_json: Mapped[str] = mapped_column(Text)
    ikof: Mapped[str | None] = mapped_column(String(128), nullable=True)
    jikr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    partner_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    inv_num: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inv_ord_num: Mapped[int | None] = mapped_column(nullable=True)
    type_of_inv: Mapped[str | None] = mapped_column(String(16), nullable=True)
    inv_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fiscalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Šablon za mjesečni raspored (ne mora biti fiskalizovan)
    is_template: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tenant: Mapped[Tenant] = relationship(back_populates="invoices")
    lines: Mapped[list[InvoiceLine]] = relationship(back_populates="invoice", cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_price_net: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    total_gross: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Language(Base):
    """Podržani UI jezici (VG eFiskal set)."""

    __tablename__ = "languages"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    native_name: Mapped[str] = mapped_column(String(128), default="")
    script: Mapped[str] = mapped_column(String(16), default="Latn")  # Latn | Cyrl
    is_default: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    translations: Mapped[list[Translation]] = relationship(back_populates="language")


class TranslationKey(Base):
    """Ključ UI stringa (npr. nav.pregled, btn.save)."""

    __tablename__ = "translation_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), default="ui", index=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    translations: Mapped[list[Translation]] = relationship(back_populates="translation_key", cascade="all, delete-orphan")


class Translation(Base):
    """Prevod jednog ključa za jedan jezik."""

    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("key", "language_code", name="uq_translation_key_lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(ForeignKey("translation_keys.key"), index=True)
    language_code: Mapped[str] = mapped_column(ForeignKey("languages.code"), index=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    translation_key: Mapped[TranslationKey] = relationship(back_populates="translations")
    language: Mapped[Language] = relationship(back_populates="translations")


class BankStatement(Base):
    """Bankovni izvod (npr. Hipotekarna) uvezen s maila ili fajla."""

    __tablename__ = "bank_statements"
    __table_args__ = (UniqueConstraint("tenant_id", "file_hash", name="uq_tenant_statement_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    statement_day: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    statement_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    opening: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    closing: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    debit_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    credit_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    tx_count: Mapped[int] = mapped_column(default=0)
    source: Mapped[str] = mapped_column(String(32), default="mail")  # mail | upload
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="bank_statements")
    transactions: Mapped[list["BankTransaction"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )


class BankTransaction(Base):
    """Stavka bankovnog izvoda."""

    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("bank_statements.id"), nullable=True, index=True)
    tx_date: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_type: Mapped[str] = mapped_column(String(32), default="other")  # credit | debit | card | other
    status: Mapped[str] = mapped_column(String(32), default="needs_review", index=True)
    # needs_review | matched | card | ignored
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    incoming_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("incoming_invoices.id"), nullable=True, index=True
    )
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="bank_transactions")
    statement: Mapped[BankStatement | None] = relationship(back_populates="transactions")
    payments: Mapped[list["CustomerPayment"]] = relationship(back_populates="bank_tx")
    incoming_invoice: Mapped["IncomingInvoice | None"] = relationship(
        foreign_keys=[incoming_invoice_id]
    )


class CustomerPayment(Base):
    """Uplata kupca (ručna ili iz bankovnog izvoda) — za finansijsku karticu."""

    __tablename__ = "customer_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True, index=True)
    bank_tx_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transactions.id"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    paid_at: Mapped[str | None] = mapped_column(String(16), nullable=True)  # YYYY-MM-DD
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="customer_payments")
    customer: Mapped[Customer] = relationship(back_populates="payments")
    bank_tx: Mapped[BankTransaction | None] = relationship(back_populates="payments")


class InvoiceMailSeen(Base):
    """Dedup IMAP poruka (fakture / izvodi)."""

    __tablename__ = "invoice_mail_seen"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", name="uq_tenant_mail_seen"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), default="izvod")  # izvod | faktura
    matched: Mapped[str | None] = mapped_column(String(512), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IncomingInvoiceStatus(str, Enum):
    draft = "draft"
    recorded = "recorded"
    paid = "paid"
    disputed = "disputed"


class IncomingInvoiceSource(str, Enum):
    manual = "manual"
    qr = "qr"
    mail = "mail"


class Supplier(Base):
    """Dobavljač (ulazne fakture / AP)."""

    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("tenant_id", "pib", name="uq_tenant_supplier_pib"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    pib: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, default="Crna Gora")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="suppliers")
    incoming_invoices: Mapped[list["IncomingInvoice"]] = relationship(back_populates="supplier")


class IncomingInvoice(Base):
    """Ulazna faktura (AP) — ručni unos, QR sa tax.gov.me, ili mail."""

    __tablename__ = "incoming_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True, index=True)
    number: Mapped[str] = mapped_column(String(64), default="")
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    supplier_pib: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=IncomingInvoiceStatus.draft.value, index=True)
    source: Mapped[str] = mapped_column(String(16), default=IncomingInvoiceSource.manual.value)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    total_net: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    total_vat: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    total_gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    ikof: Mapped[str | None] = mapped_column(String(128), nullable=True)
    jikr: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tenant: Mapped[Tenant] = relationship(back_populates="incoming_invoices")
    supplier: Mapped[Supplier | None] = relationship(back_populates="incoming_invoices")
    lines: Mapped[list["IncomingInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    expenses: Mapped[list["Expense"]] = relationship(back_populates="incoming_invoice")


class IncomingInvoiceLine(Base):
    __tablename__ = "incoming_invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("incoming_invoices.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("1"))
    unit_price_net: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("21"))
    total_gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    invoice: Mapped[IncomingInvoice] = relationship(back_populates="lines")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_expense_cat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    color: Mapped[str] = mapped_column(String(16), default="#6366f1")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="expense_categories")
    expenses: Mapped[list["Expense"]] = relationship(back_populates="category")


class Expense(Base):
    """Trošak — ručni unos ili iz ulazne fakture / bankovne stavke."""

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("expense_categories.id"), nullable=True, index=True)
    incoming_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("incoming_invoices.id"), nullable=True, index=True
    )
    bank_tx_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transactions.id"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="expenses")
    category: Mapped[ExpenseCategory | None] = relationship(back_populates="expenses")
    incoming_invoice: Mapped[IncomingInvoice | None] = relationship(
        back_populates="expenses", foreign_keys=[incoming_invoice_id]
    )
    bank_tx: Mapped[BankTransaction | None] = relationship(
        foreign_keys=[bank_tx_id],
        primaryjoin="Expense.bank_tx_id==BankTransaction.id",
        viewonly=True,
    )


class InvoiceSchedule(Base):
    """Mjesečni raspored: kopiraj šablon fakture na odabrane dane + opciono fiskalizuj/pošalji."""

    __tablename__ = "invoice_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    template_invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    # Dani u mjesecu, npr. "1,15" ili "28"
    days_of_month: Mapped[str] = mapped_column(String(64), default="1")
    # Broj ugovora / ugovorne reference
    contract_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # current = period je mjesec izdavanja; previous = prethodni mjesec
    period_mode: Mapped[str] = mapped_column(String(16), default="previous")
    auto_fiscalize: Mapped[bool] = mapped_column(default=False)
    auto_email: Mapped[bool] = mapped_column(default=True)
    email_to: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    last_run_key: Mapped[str | None] = mapped_column(String(16), nullable=True)  # YYYY-MM-DD
    last_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tenant: Mapped[Tenant] = relationship(back_populates="invoice_schedules")
    template_invoice: Mapped[Invoice] = relationship(foreign_keys=[template_invoice_id])
    customer: Mapped[Customer | None] = relationship(foreign_keys=[customer_id])


class AdminAuditLog(Base):
    """Promjene koje radi superadmin (kreiranje tenanta, status, licenca)."""

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    actor_email: Mapped[str] = mapped_column(String(255), default="")
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """Tenant revizija: izdavanje računa, storno, cijene, blagajna."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    actor_email: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    tenant: Mapped[Tenant | None] = relationship(back_populates="audit_logs")
