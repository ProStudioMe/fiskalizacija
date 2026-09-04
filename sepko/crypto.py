"""Kriptovanje u mirovanju (Fernet) za lozinke/PIN u bazi.

Ključ se izvodi iz SEPKO_SECRET_KEY — nikad ne commituj .env.
Produkcija: HashiCorp Vault / SEPKO_VAULT_ADDR za rotaciju tajni.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sepko.config import get_settings

ENC_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    settings = get_settings()
    material = (settings.secret_key or "").encode("utf-8")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"sepko-at-rest-v1",
        info=b"fernet",
    ).derive(material)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str | None) -> str:
    raw = (plain or "").strip()
    if not raw or raw.startswith(ENC_PREFIX):
        return raw
    token = _fernet().encrypt(raw.encode("utf-8")).decode("ascii")
    return f"{ENC_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str:
    raw = value or ""
    if not raw.startswith(ENC_PREFIX):
        return raw
    token = raw[len(ENC_PREFIX) :].encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def is_encrypted(value: str | None) -> bool:
    return (value or "").startswith(ENC_PREFIX)


def fingerprint(value: str) -> str:
    """Ne-reverzibilni trag za logove (npr. prefix API ključa)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
