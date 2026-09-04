from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sepko.db import Base
from sepko.i18n import LANGUAGES_SEED, STRINGS_SEED, ensure_languages, ensure_translations, t
from sepko.models import Language, Translation, TranslationKey


@pytest.fixture()
def db_session_sqlite():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    yield db
    db.close()


def test_languages_seed_count():
    assert len(LANGUAGES_SEED) == 9
    codes = {x["code"] for x in LANGUAGES_SEED}
    assert "cnr" in codes and "en" in codes and "sq" in codes


def test_t_helper():
    assert t({"nav.pregled": "Pregled"}, "nav.pregled") == "Pregled"
    assert t({}, "missing", "fallback") == "fallback"
    assert t({}, "missing") == "missing"


def test_ensure_i18n_tables(db_session_sqlite):
    db = db_session_sqlite
    ensure_languages(db)
    n = ensure_translations(db)
    db.commit()
    assert db.query(Language).count() == 9
    assert db.query(TranslationKey).count() == len(STRINGS_SEED)
    assert db.query(Translation).count() >= len(STRINGS_SEED)
    assert n > 0
    cnr = db.get(Language, "cnr")
    assert cnr is not None and cnr.is_default
