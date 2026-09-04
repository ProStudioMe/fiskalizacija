"""Upload logotipa komitenata (lokalni disk, tenant-scoped)."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from sepko.config import get_settings

ALLOWED_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def uploads_root() -> Path:
    settings = get_settings()
    # pored sqlite data/ ako postoji; inače ./data/uploads
    base = Path("data") / "uploads"
    raw = getattr(settings, "uploads_dir", None)
    if raw:
        base = Path(str(raw))
    base.mkdir(parents=True, exist_ok=True)
    return base


def customer_logo_dir(tenant_id: int) -> Path:
    path = uploads_root() / f"t{tenant_id}" / "customers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_ext(filename: str | None) -> str | None:
    if not filename:
        return None
    name = filename.strip().lower()
    m = re.search(r"(\.[a-z0-9]{2,5})$", name)
    if not m:
        return None
    ext = m.group(1)
    return ext if ext in ALLOWED_EXT else None


async def save_customer_logo(
    tenant_id: int,
    customer_id: int,
    upload: UploadFile | None,
    previous: str | None = None,
) -> str | None:
    """Sačuvaj novi logo; vrati relativni filename ili previous ako nema fajla."""
    if upload is None or not upload.filename:
        return previous
    ext = _safe_ext(upload.filename)
    if not ext:
        raise ValueError("Dozvoljeni formati loga: PNG, JPG, WEBP, GIF.")
    data = await upload.read()
    if not data:
        return previous
    if len(data) > MAX_BYTES:
        raise ValueError("Logo je prevelik (max 2 MB).")
    dest_dir = customer_logo_dir(tenant_id)
    fname = f"{customer_id}-{uuid.uuid4().hex[:8]}{ext}"
    dest = dest_dir / fname
    dest.write_bytes(data)
    if previous:
        old = dest_dir / Path(previous).name
        if old.is_file() and old.resolve().parent == dest_dir.resolve():
            try:
                old.unlink()
            except OSError:
                pass
    return fname


def delete_customer_logo(tenant_id: int, filename: str | None) -> None:
    if not filename:
        return
    path = customer_logo_dir(tenant_id) / Path(filename).name
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def resolve_customer_logo(tenant_id: int, filename: str | None) -> Path | None:
    if not filename:
        return None
    path = customer_logo_dir(tenant_id) / Path(filename).name
    if path.is_file():
        return path
    return None
