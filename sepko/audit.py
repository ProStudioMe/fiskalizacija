"""Revizijski trag fiskalnih i osjetljivih akcija (korisnik, IP, vrijeme)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.orm import Session

from sepko.models import AuditLog


@dataclass
class AuditActor:
    user_id: int | None = None
    email: str = ""
    tenant_id: int | None = None
    ip: str = ""


_actor: ContextVar[AuditActor | None] = ContextVar("sepko_audit_actor", default=None)


def bind_actor(actor: AuditActor | None) -> None:
    _actor.set(actor)


def current_actor() -> AuditActor:
    return _actor.get() or AuditActor()


def write_audit(
    db: Session,
    action: str,
    *,
    tenant_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: str | None = None,
) -> None:
    actor = current_actor()
    db.add(
        AuditLog(
            tenant_id=tenant_id if tenant_id is not None else actor.tenant_id,
            user_id=actor.user_id,
            actor_email=(actor.email or "")[:255],
            ip=(actor.ip or "")[:64],
            action=action[:64],
            entity_type=(entity_type or "")[:32] or None,
            entity_id=entity_id,
            detail=(detail or "")[:4000] or None,
        )
    )
