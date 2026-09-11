"""Upload logotipa komitenata i thumbnail artikala (lokalni disk, tenant-scoped)."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from sepko.config import get_settings

ALLOWED_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
MAX_BYTES = 2 * 1024 * 1024  # 2 MB
DEMO_PREFIX = "demo/"


def uploads_root() -> Path:
    settings = get_settings()
    # pored sqlite data/ ako postoji; inače ./data/uploads
    base = Path("data") / "uploads"
    raw = getattr(settings, "uploads_dir", None)
    if raw:
        base = Path(str(raw))
    base.mkdir(parents=True, exist_ok=True)
    return base


def demo_products_dir() -> Path:
    return Path(__file__).resolve().parent / "static" / "img" / "demo-products"


def customer_logo_dir(tenant_id: int) -> Path:
    path = uploads_root() / f"t{tenant_id}" / "customers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def company_logo_dir(tenant_id: int) -> Path:
    path = uploads_root() / f"t{tenant_id}" / "company"
    path.mkdir(parents=True, exist_ok=True)
    return path


def article_thumb_dir(tenant_id: int) -> Path:
    path = uploads_root() / f"t{tenant_id}" / "articles"
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
    if not ext or ext == ".svg":
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


_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_data_uri(path: Path | None) -> str | None:
    """Ugradi sliku u HTML računa (print/PDF bez dodatnog requesta)."""
    if path is None or not path.is_file():
        return None
    mime = _IMAGE_MIME.get(path.suffix.lower())
    if not mime:
        return None
    import base64

    raw = path.read_bytes()
    if not raw or len(raw) > MAX_BYTES:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


async def save_company_logo(
    tenant_id: int,
    upload: UploadFile | None,
    previous: str | None = None,
) -> str | None:
    if upload is None or not upload.filename:
        return previous
    ext = _safe_ext(upload.filename)
    if not ext or ext == ".svg":
        raise ValueError("Dozvoljeni formati loga: PNG, JPG, WEBP, GIF.")
    data = await upload.read()
    if not data:
        return previous
    if len(data) > MAX_BYTES:
        raise ValueError("Logo je prevelik (max 2 MB).")
    dest_dir = company_logo_dir(tenant_id)
    fname = f"logo-{uuid.uuid4().hex[:10]}{ext}"
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


def delete_company_logo(tenant_id: int, filename: str | None) -> None:
    if not filename:
        return
    path = company_logo_dir(tenant_id) / Path(filename).name
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def resolve_company_logo(tenant_id: int, filename: str | None) -> Path | None:
    if not filename:
        return None
    path = company_logo_dir(tenant_id) / Path(filename).name
    return path if path.is_file() else None


def is_demo_thumb(filename: str | None) -> bool:
    return bool(filename and filename.startswith(DEMO_PREFIX))


def article_thumb_public_url(article_id: int, filename: str | None) -> str | None:
    if not filename:
        return None
    if is_demo_thumb(filename):
        name = Path(filename[len(DEMO_PREFIX) :]).name
        return f"/static/img/demo-products/{name}?v=4"
    return f"/artikli/{article_id}/thumb"


async def save_article_thumb(
    tenant_id: int,
    article_id: int,
    upload: UploadFile | None,
    previous: str | None = None,
) -> str | None:
    if upload is None or not upload.filename:
        return previous
    ext = _safe_ext(upload.filename)
    if not ext:
        raise ValueError("Dozvoljeni formati: PNG, JPG, WEBP, GIF, SVG.")
    data = await upload.read()
    if not data:
        return previous
    if len(data) > MAX_BYTES:
        raise ValueError("Slika je prevelika (max 2 MB).")
    dest_dir = article_thumb_dir(tenant_id)
    fname = f"{article_id}-{uuid.uuid4().hex[:8]}{ext}"
    dest = dest_dir / fname
    dest.write_bytes(data)
    if previous and not is_demo_thumb(previous):
        old = dest_dir / Path(previous).name
        if old.is_file() and old.resolve().parent == dest_dir.resolve():
            try:
                old.unlink()
            except OSError:
                pass
    return fname


def delete_article_thumb(tenant_id: int, filename: str | None) -> None:
    if not filename or is_demo_thumb(filename):
        return
    path = article_thumb_dir(tenant_id) / Path(filename).name
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def resolve_article_thumb(tenant_id: int, filename: str | None) -> Path | None:
    if not filename:
        return None
    if is_demo_thumb(filename):
        path = demo_products_dir() / Path(filename[len(DEMO_PREFIX) :]).name
        return path if path.is_file() else None
    path = article_thumb_dir(tenant_id) / Path(filename).name
    return path if path.is_file() else None
