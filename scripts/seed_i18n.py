"""Seed / osvježi jezike i prevode u bazi."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sepko.db import SessionLocal, init_db
from sepko.i18n import ensure_translations, list_languages
from sepko.models import Translation, TranslationKey


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        n = ensure_translations(db)
        db.commit()
        langs = list_languages(db)
        keys = db.query(TranslationKey).count()
        vals = db.query(Translation).count()
        print(f"Languages: {len(langs)}")
        for code, name in langs:
            print(f"  - {code}: {name}")
        print(f"Translation keys: {keys}")
        print(f"Translations: {vals} (upserted this run: {n})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
