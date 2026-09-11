"""Ručno: python scripts/ensure_tenants.py [--reset-password]."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sepko.bootstrap import ensure_client_tenants
from sepko.db import SessionLocal, init_db
from sepko.models import User
from sepko.web_auth import hash_password


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Postavi lozinke iz env i na postojeće admine (prostudio/hotel).",
    )
    args = parser.parse_args()

    init_db()
    if not args.reset_password:
        print("Tenanti prostudio + hotel su osigurani (postojece lozinke nisu dirane).")
        return

    from sepko.config import get_settings

    settings = get_settings()
    pairs = [
        ("admin@prostudio.me", (settings.prostudio_admin_password or "").strip()),
        ("admin@hotel.me", (settings.hotel_admin_password or "").strip()),
    ]
    db = SessionLocal()
    try:
        for email, password in pairs:
            if len(password) < 8:
                print(f"Preskočeno {email}: lozinka u env nije setovana ili je kraća od 8.")
                continue
            user = db.query(User).filter(User.email == email).first()
            if not user:
                print(f"Nema korisnika {email}. Pokreni bez --reset-password.")
                continue
            user.password_hash = hash_password(password)
            user.active = True
            print(f"Lozinka ažurirana: {email}")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
