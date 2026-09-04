from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.models import ApiKey, Tenant


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix)."""
    raw = f"sk_live_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw), raw[:12]


def get_tenant(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    db: Session = Depends(get_db),
) -> Tenant:
    raw: str | None = None
    if x_api_key:
        raw = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()

    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    key = (
        db.query(ApiKey)
        .filter(ApiKey.key_hash == hash_api_key(raw), ApiKey.active.is_(True))
        .first()
    )
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    tenant = db.get(Tenant, key.tenant_id)
    if not tenant or tenant.status == "suspended":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Tenant inactive")
    from sepko.audit import AuditActor, bind_actor
    from sepko.web_security import client_ip

    bind_actor(
        AuditActor(
            tenant_id=tenant.id,
            email=f"api:{key.key_prefix}",
            ip=client_ip(request),
        )
    )
    return tenant
