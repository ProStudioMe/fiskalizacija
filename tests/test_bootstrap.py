from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.bootstrap import ensure_client_tenants
from sepko.brand import LICENSE_ITEM
from sepko.db import Base
from sepko.models import Article, Tenant, User


def test_ensure_client_tenants_creates_admins(monkeypatch):
    monkeypatch.setenv("SEPKO_ENV", "development")
    monkeypatch.setenv("SEPKO_PROSTUDIO_ADMIN_PASSWORD", "")
    monkeypatch.setenv("SEPKO_HOTEL_ADMIN_PASSWORD", "")
    from sepko.config import get_settings

    get_settings.cache_clear()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    try:
        ensure_client_tenants(db)
        db.commit()
        slugs = {t.slug for t in db.query(Tenant).all()}
        emails = {u.email for u in db.query(User).all()}
        assert slugs == {"hotel", "prostudio"}
        assert emails == {"admin@philiahotel.com", "finansije@prostudio.me"}
        hotel = db.query(Tenant).filter(Tenant.slug == "hotel").one()
        assert hotel.license_from == date.today()
        ps = db.query(Tenant).filter(Tenant.slug == "prostudio").one()
        codes = {
            a.code
            for a in db.query(Article).filter(Article.tenant_id == ps.id).all()
        }
        assert "PR-BASIC" in codes
        assert "2" in codes
        license_row = (
            db.query(Article)
            .filter(Article.tenant_id == ps.id, Article.code == "PR-BASIC")
            .one()
        )
        assert license_row.name == LICENSE_ITEM
        assert license_row.thumbnail_filename == "demo/proracun.svg"
        license_row.thumbnail_filename = "demo/app.svg"
        db.commit()
        ensure_client_tenants(db)
        db.commit()
        db.refresh(license_row)
        assert license_row.thumbnail_filename == "demo/proracun.svg"
        hotel_articles = (
            db.query(Article).filter(Article.tenant_id == hotel.id).count()
        )
        assert hotel_articles == 0
    finally:
        db.close()
        engine.dispose()
        get_settings.cache_clear()
