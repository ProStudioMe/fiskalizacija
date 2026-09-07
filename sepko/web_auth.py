from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from sepko.config import get_settings
from sepko.db import get_db
from sepko.models import Tenant, TenantStatus, User

SUPERADMIN_EMAIL = "super@sepko.me"
SUPERADMIN_ROLE = "superadmin"


class AuthRequired(Exception):
    """Redirect to /login when session missing."""


class AdminAuthRequired(Exception):
    """Redirect to /admin/login when superadmin session missing."""


class TenantSuspended(Exception):
    """Tenant status=suspended — logout and show message."""


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(check, digest)


def is_superadmin(user: User) -> bool:
    return user.role == SUPERADMIN_ROLE


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise AuthRequired()
    user = db.get(User, user_id)
    if not user or not user.active:
        request.session.clear()
        raise AuthRequired()
    return user


def get_current_superadmin(request: Request, db: Session = Depends(get_db)) -> User:
    try:
        user = get_current_user(request, db)
    except AuthRequired:
        raise AdminAuthRequired() from None
    if not is_superadmin(user):
        raise AdminAuthRequired()
    return user


def resolve_tenant(user: User, db: Session) -> Tenant:
    if is_superadmin(user) or not user.tenant_id:
        raise AuthRequired()
    tenant = db.get(Tenant, user.tenant_id)
    if not tenant:
        raise AuthRequired()
    if tenant.status == TenantStatus.suspended.value:
        raise TenantSuspended()
    return tenant


def get_current_tenant(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Tenant:
    return resolve_tenant(user, db)


def login_user(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["role"] = user.role
    request.session["email"] = user.email
    if user.tenant_id is None:
        request.session.pop("tenant_id", None)
    else:
        request.session["tenant_id"] = user.tenant_id


def logout_user(request: Request) -> None:
    request.session.clear()


def ensure_superadmin(db: Session) -> User | None:
    """Kreira super@sepko.me ako ne postoji. U produkciji treba SEPKO_SUPERADMIN_PASSWORD."""
    existing = db.query(User).filter(User.email == SUPERADMIN_EMAIL).first()
    if existing:
        existing.role = SUPERADMIN_ROLE
        existing.tenant_id = None
        existing.active = True
        return existing
    settings = get_settings()
    password = (settings.superadmin_password or "").strip()
    if not password:
        if settings.env == "production":
            return None
        password = "sepko-super"
    user = User(
        tenant_id=None,
        email=SUPERADMIN_EMAIL,
        password_hash=hash_password(password),
        full_name="ProRačun Superadmin",
        role=SUPERADMIN_ROLE,
        active=True,
    )
    db.add(user)
    db.flush()
    return user
