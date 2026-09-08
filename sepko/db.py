from collections.abc import AsyncGenerator, Generator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from sepko.config import get_settings
from sepko.sqlsafe import add_column_if_missing


class Base(DeclarativeBase):
    pass


def sync_database_url(url: str) -> str:
    """SQLAlchemy sync URL (psycopg / sqlite) derived from SEPKO_DATABASE_URL."""
    u = (url or "").strip()
    if u.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + u.removeprefix("postgresql+asyncpg://")
    if "+asyncpg" in u:
        return u.replace("+asyncpg", "+psycopg", 1)
    if u.startswith("postgresql://"):
        return "postgresql+psycopg://" + u.removeprefix("postgresql://")
    if u.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + u.removeprefix("sqlite+aiosqlite://")
    return u


def async_database_url(url: str) -> str:
    """SQLAlchemy async URL (asyncpg / aiosqlite) — does not block the event loop."""
    u = (url or "").strip()
    if u.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + u.removeprefix("postgresql+psycopg://")
    if "+psycopg" in u:
        return u.replace("+psycopg", "+asyncpg", 1)
    if u.startswith("postgresql://"):
        return "postgresql+asyncpg://" + u.removeprefix("postgresql://")
    if u.startswith("sqlite+aiosqlite://"):
        return u
    if u.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + u.removeprefix("sqlite://")
    return u


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///./"):
        path = Path(url.removeprefix("sqlite:///./"))
        path.parent.mkdir(parents=True, exist_ok=True)


def _sync_engine_kwargs(url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return kwargs


def _make_engine(url: str | None = None):
    settings = get_settings()
    raw = url if url is not None else settings.database_url
    sync_url = sync_database_url(raw)
    _ensure_sqlite_dir(sync_url)
    return create_engine(sync_url, **_sync_engine_kwargs(sync_url))


def _make_async_engine():
    settings = get_settings()
    url = async_database_url(settings.database_url)
    _ensure_sqlite_dir(sync_database_url(url))
    kwargs: dict = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return create_async_engine(url, **kwargs)


engine = _make_engine()
_admin_url = (get_settings().database_admin_url or "").strip()
ddl_engine = _make_engine(_admin_url) if _admin_url else engine
async_engine = _make_async_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """Sync session for `def` HTML/API rute (FastAPI ih izvršava u threadpoolu)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """AsyncSession + asyncpg — za `async def` rute pod opterećenjem."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def ping_async() -> None:
    """Provjera da asyncpg (ili aiosqlite) pool živi, bez blokiranja loopa."""
    async with async_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def dispose_async_engine() -> None:
    await async_engine.dispose()


def init_db() -> None:
    from sepko import models  # noqa: F401
    from sepko.i18n import ensure_translations
    from sqlalchemy import inspect

    Base.metadata.create_all(bind=ddl_engine)
    insp = inspect(ddl_engine)
    with ddl_engine.begin() as conn:
        if "invoices" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("invoices")}
            extras = {
                "inv_num": "VARCHAR(64)",
                "inv_ord_num": "INTEGER",
                "type_of_inv": "VARCHAR(16)",
                "inv_type": "VARCHAR(32)",
                "is_template": "BOOLEAN",
            }
            for name, coltype in extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "invoices", name, coltype)
        if "customers" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("customers")}
            cust_extras = {
                "pdv_number": "VARCHAR(64)",
                "street": "VARCHAR(255)",
                "city": "VARCHAR(128)",
                "country": "VARCHAR(64)",
                "email": "VARCHAR(255)",
                "phone": "VARCHAR(64)",
                "tax_card_number": "VARCHAR(64)",
                "contact": "VARCHAR(255)",
                "logo_filename": "VARCHAR(255)",
                "discount_pct": "NUMERIC(5,2)",
            }
            for name, coltype in cust_extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "customers", name, coltype)
        if "suppliers" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("suppliers")}
            supp_extras = {
                "pdv_number": "VARCHAR(64)",
                "street": "VARCHAR(255)",
                "city": "VARCHAR(128)",
                "country": "VARCHAR(64)",
                "email": "VARCHAR(255)",
                "phone": "VARCHAR(64)",
                "contact": "VARCHAR(255)",
                "notes": "TEXT",
            }
            for name, coltype in supp_extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "suppliers", name, coltype)
        if "articles" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("articles")}
            art_extras = {
                "category_id": "INTEGER",
                "price_retail": "NUMERIC(14,4)",
                "stock_qty": "NUMERIC(14,4)",
                "barcode": "VARCHAR(64)",
                "description": "TEXT",
                "color": "VARCHAR(16)",
                "tax_rate_code": "VARCHAR(32)",
                "thumbnail_filename": "VARCHAR(255)",
            }
            for name, coltype in art_extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "articles", name, coltype)
        if "tenants" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("tenants")}
            tenant_extras = {
                "license_type": "VARCHAR(32)",
                "license_from": "DATE",
                "license_until": "DATE",
            }
            for name, coltype in tenant_extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "tenants", name, coltype)
        if "license_invoices" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("license_invoices")}
            lic_inv_extras = {
                "kind": "VARCHAR(16)",
                "covers_until": "DATE",
            }
            for name, coltype in lic_inv_extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "license_invoices", name, coltype)
        if "bank_transactions" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("bank_transactions")}
            if "incoming_invoice_id" not in existing:
                add_column_if_missing(conn, "bank_transactions", "incoming_invoice_id", "INTEGER")
        if "invoice_schedules" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("invoice_schedules")}
            sched_extras = {
                "contract_number": "VARCHAR(128)",
                "period_mode": "VARCHAR(16)",
            }
            for name, coltype in sched_extras.items():
                if name not in existing:
                    add_column_if_missing(conn, "invoice_schedules", name, coltype)
        if "users" in insp.get_table_names() and ddl_engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE users ALTER COLUMN tenant_id DROP NOT NULL"))

    # Jezici + osnovni prevodi (idempotentno)
    db = SessionLocal()
    try:
        ensure_translations(db)
        from sepko.bootstrap import ensure_client_tenants
        from sepko.web_auth import ensure_superadmin

        ensure_superadmin(db)
        ensure_client_tenants(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
