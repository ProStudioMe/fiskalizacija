"""QR za A4 račun (SVG, bez Pillow)."""
from __future__ import annotations

import base64
import io


def qr_data_uri(payload: str, box_size: int = 4, border: int = 1) -> str | None:
    text = (payload or "").strip()
    if not text:
        return None
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        return None
    img = qrcode.make(text, image_factory=SvgPathImage, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
