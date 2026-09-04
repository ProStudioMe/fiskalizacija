"""QR / verify URL parsing for CG tax.gov.me incoming invoices."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, unquote, urlparse

import httpx

# Known CG fiscal verify hosts
_VERIFY_HOSTS = (
    "efitest.tax.gov.me",
    "mapr.tax.gov.me",
    "tax.gov.me",
)


@dataclass
class QrInvoiceDraft:
    """Predpopuna ulazne fakture iz QR / verify URL-a."""

    qr_url: str
    ikof: str | None = None
    supplier_pib: str | None = None
    issue_date: date | None = None
    issue_datetime: datetime | None = None
    ord_num: int | None = None
    busin_unit_code: str | None = None
    tcr_code: str | None = None
    soft_code: str | None = None
    total_gross: Decimal | None = None
    supplier_name: str | None = None
    jikr: str | None = None
    number: str = ""
    lines: list[dict] = field(default_factory=list)
    notes: str | None = None
    fetched: bool = False
    fetch_error: str | None = None


def is_cg_verify_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _VERIFY_HOSTS)


def parse_verify_url(raw: str) -> QrInvoiceDraft:
    """Parse tax.gov.me verify URL query into a draft. Accepts full URL or raw query."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Prazan QR / URL")

    # Some scanners return only the query part
    if "://" not in text and ("iic=" in text or "tin=" in text):
        text = "https://mapr.tax.gov.me/ic/#/verify?" + text.lstrip("?")

    # Hash-router: .../ic/#/verify?iic=...
    if "#/" in text and "?" in text.split("#", 1)[-1]:
        # Keep query after hash
        before, after = text.split("#", 1)
        if "?" in after:
            frag_path, frag_q = after.split("?", 1)
            text = before.rstrip("?&") + "?" + frag_q

    parsed = urlparse(text)
    qs = parse_qs(parsed.query)
    # Also try fragment query
    if not qs and parsed.fragment and "?" in parsed.fragment:
        qs = parse_qs(parsed.fragment.split("?", 1)[1])

    def one(key: str) -> str | None:
        vals = qs.get(key) or qs.get(key.upper()) or qs.get(key.lower())
        if not vals:
            return None
        return unquote(str(vals[0])).strip() or None

    iic = one("iic")
    tin = one("tin")
    crtd = one("crtd")
    ord_s = one("ord")
    bu = one("bu")
    cr = one("cr")
    sw = one("sw")
    prc = one("prc")

    if not iic and not tin:
        raise ValueError("QR ne sadrži iic/tin — nije CG fiskalni verify URL")

    issue_dt: datetime | None = None
    issue_d: date | None = None
    if crtd:
        try:
            issue_dt = datetime.fromisoformat(crtd.replace("Z", "+00:00"))
            issue_d = issue_dt.date()
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%d.%m.%Y", "%Y-%m-%d"):
                try:
                    issue_dt = datetime.strptime(crtd[:19], fmt)
                    issue_d = issue_dt.date()
                    break
                except ValueError:
                    continue

    gross: Decimal | None = None
    if prc:
        from sepko.money import parse_amount

        gross = parse_amount(prc, quantize="0.01")

    ord_num: int | None = None
    if ord_s:
        try:
            ord_num = int(ord_s)
        except ValueError:
            ord_num = None

    number = ""
    if bu and ord_num and issue_d and cr:
        number = f"{bu}/{ord_num}/{issue_d.year}/{cr}"
    elif ord_num and issue_d:
        number = f"{ord_num}/{issue_d.year}"

    # Normalize to canonical URL for storage (keep hash-router form used by CG portal)
    if text.startswith("http") and "#/" in (raw or ""):
        # Reconstruct hash form if original had it
        host_base = "https://efitest.tax.gov.me" if "efitest" in text else "https://mapr.tax.gov.me"
        q = parsed.query
        if not q and "?" in text:
            q = text.split("?", 1)[1]
        qr_url = f"{host_base}/ic/#/verify?{q}"
    elif text.startswith("http"):
        qr_url = text
    else:
        qr_url = f"https://mapr.tax.gov.me/ic/#/verify?{parsed.query}"
    qr_url = qr_url[:512]

    return QrInvoiceDraft(
        qr_url=qr_url,
        ikof=iic,
        supplier_pib=tin,
        issue_date=issue_d,
        issue_datetime=issue_dt,
        ord_num=ord_num,
        busin_unit_code=bu,
        tcr_code=cr,
        soft_code=sw,
        total_gross=gross,
        number=number[:64],
    )


def enrich_from_portal(draft: QrInvoiceDraft, *, timeout: float = 12.0) -> QrInvoiceDraft:
    """
    Best-effort fetch of public verify page. Portal often returns SPA shell only —
    we still try to scrape JSON embedded in HTML or follow known API patterns.
    Never fails hard: sets fetch_error and returns draft.
    """
    if not draft.qr_url or not is_cg_verify_url(draft.qr_url):
        draft.fetch_error = "URL nije CG verify portal"
        return draft

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            # Attempt JSON API variants used by some fiscal portals
            api_candidates = []
            if draft.ikof and draft.supplier_pib:
                base = "https://efitest.tax.gov.me" if "efitest" in draft.qr_url else "https://mapr.tax.gov.me"
                api_candidates.extend(
                    [
                        f"{base}/ic/api/invoice?iic={draft.ikof}&tin={draft.supplier_pib}",
                        f"{base}/api/verify?iic={draft.ikof}&tin={draft.supplier_pib}",
                    ]
                )
            for url in api_candidates:
                try:
                    r = client.get(url, headers={"Accept": "application/json"})
                    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                        data = r.json()
                        _apply_portal_json(draft, data)
                        draft.fetched = True
                        return draft
                except Exception:
                    continue

            # Fallback: GET verify page (may be SPA)
            r = client.get(draft.qr_url, headers={"Accept": "text/html"})
            if r.status_code >= 400:
                draft.fetch_error = f"Portal HTTP {r.status_code}"
                return draft
            text = r.text or ""
            # Very light scrape for name / FIC if present in HTML
            import re

            m = re.search(r'"sellerName"\s*:\s*"([^"]+)"', text) or re.search(
                r'"name"\s*:\s*"([^"]+)"', text
            )
            if m and not draft.supplier_name:
                draft.supplier_name = m.group(1)[:255]
            m = re.search(r'"fic"\s*:\s*"([^"]+)"', text, re.I) or re.search(
                r'"jikr"\s*:\s*"([^"]+)"', text, re.I
            )
            if m and not draft.jikr:
                draft.jikr = m.group(1)[:128]
            draft.fetched = bool(draft.supplier_name or draft.jikr)
            if not draft.fetched:
                draft.fetch_error = "Portal nije vratio stavke (SPA) — dopuni ručno"
    except httpx.HTTPError as exc:
        draft.fetch_error = f"Mreža: {exc}"
    return draft


def _apply_portal_json(draft: QrInvoiceDraft, data: dict) -> None:
    if not isinstance(data, dict):
        return
    nested = data.get("invoice") or data.get("result") or data.get("data") or data
    if not isinstance(nested, dict):
        nested = data
    draft.supplier_name = (nested.get("sellerName") or nested.get("name") or draft.supplier_name or "")[
        :255
    ] or draft.supplier_name
    draft.jikr = (nested.get("fic") or nested.get("jikr") or draft.jikr or "")[:128] or draft.jikr
    items = nested.get("items") or nested.get("lines") or []
    lines: list[dict] = []
    if isinstance(items, list):
        for it in items[:200]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("n") or it.get("name") or it.get("Naziv") or "Stavka")[:255]
            try:
                qty = Decimal(str(it.get("q") or it.get("quantity") or "1"))
            except (InvalidOperation, ValueError):
                qty = Decimal("1")
            try:
                up = Decimal(str(it.get("upb") or it.get("unit_price_net") or it.get("price") or "0"))
            except (InvalidOperation, ValueError):
                up = Decimal("0")
            try:
                vr = Decimal(str(it.get("vr") or it.get("vat_rate") or "21"))
            except (InvalidOperation, ValueError):
                vr = Decimal("21")
            try:
                gross = Decimal(str(it.get("pa") or it.get("total_gross") or "0"))
            except (InvalidOperation, ValueError):
                gross = (up * qty * (1 + vr / Decimal("100"))).quantize(Decimal("0.01"))
            lines.append(
                {
                    "code": str(it.get("c") or it.get("code") or "")[:64],
                    "name": name,
                    "quantity": qty,
                    "unit_price_net": up,
                    "vat_rate": vr,
                    "total_gross": gross.quantize(Decimal("0.01")),
                }
            )
    draft.lines = lines
    if nested.get("totPrice") and draft.total_gross is None:
        try:
            draft.total_gross = Decimal(str(nested["totPrice"])).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            pass


def draft_from_qr(raw: str, *, fetch: bool = True) -> QrInvoiceDraft:
    draft = parse_verify_url(raw)
    if fetch:
        enrich_from_portal(draft)
    return draft
