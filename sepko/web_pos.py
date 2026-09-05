"""Mini POS / maloprodaja — desktop + PWA kasa (isti fiskalni kod)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.efi import CASH_PAY_METHODS, load_tenant_fiscal, load_tenant_ui
from sepko.models import Article, Invoice, InvoiceStatus
from sepko.schemas import FiscalizeRequest, InvoiceLineIn, TotalsIn
from sepko.services import cash_day_summary, fiscalize_invoice
from sepko.web_auth import AuthRequired
from sepko.web_security import flash, redirect, validate_csrf
from sepko.web_templates import render

router = APIRouter(tags=["pos"])


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


def _safe_return(value: str | None, *, pwa: bool = False) -> str:
    raw = (value or "").strip()
    if raw.startswith("/app/kasa") or raw.startswith("/kasa"):
        return raw.split("?")[0]
    return "/app/kasa" if pwa else "/kasa"


def _article_sell_gross(article: Article) -> Decimal:
    """MP cijena za kasu: price_retail (bruto) ili VP neto + PDV."""
    if article.price_retail is not None and Decimal(str(article.price_retail)) > 0:
        return Decimal(str(article.price_retail)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net = Decimal(str(article.price_gross or 0))
    rate = Decimal(str(article.vat_rate or 0))
    gross = net * (1 + rate / Decimal("100"))
    return gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _articles_payload(articles: list[Article]) -> list[dict]:
    from sepko.uploads import article_thumb_public_url

    out = []
    for a in articles:
        out.append(
            {
                "id": a.id,
                "code": a.code,
                "name": a.name,
                "unit": a.unit or "KOM",
                "barcode": a.barcode or "",
                "vat": float(a.vat_rate or 0),
                "price": float(_article_sell_gross(a)),
                "thumb": article_thumb_public_url(a.id, a.thumbnail_filename),
                "initials": (a.name or "?")[:2].upper(),
            }
        )
    return out


def _pos_context(db: Session, user, tenant, *, pwa: bool = False) -> dict:
    articles = (
        db.query(Article)
        .filter(Article.tenant_id == tenant.id, Article.active.is_(True))
        .order_by(Article.name)
        .all()
    )
    day = cash_day_summary(db, tenant)
    fiscal = load_tenant_fiscal(tenant)
    ui = load_tenant_ui(tenant)
    recent = (
        db.query(Invoice)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.type_of_inv == "CASH",
            Invoice.status == InvoiceStatus.fiscalized.value,
        )
        .order_by(Invoice.id.desc())
        .limit(12)
        .all()
    )
    return {
        "user": user,
        "tenant": tenant,
        "day": day,
        "fiscal": fiscal,
        "ui": ui,
        "articles_json": _articles_payload(articles),
        "recent": recent,
        "pwa": pwa,
        "form_action": "/kasa/fiskalizuj",
        "return_to": "/app/kasa" if pwa else "/kasa",
        "blagajna_url": "/app/kasa/blagajna" if pwa else "/blagajna",
    }


@router.get("/kasa", response_class=HTMLResponse)
def pos_page(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return render(request, "pos.html", _pos_context(db, user, tenant, pwa=False))


@router.get("/app/kasa", response_class=HTMLResponse)
def pos_pwa_page(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return render(request, "pos.html", _pos_context(db, user, tenant, pwa=True))


@router.get("/app/kasa/blagajna", response_class=HTMLResponse)
def pos_pwa_cash_redirect():
    """Blagajna ostaje ista stranica; PWA se vraća na /app/kasa."""
    return redirect("/blagajna?from=pwa")


@router.get("/manifest-kasa.webmanifest")
def pos_pwa_manifest(request: Request):
    origin = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "name": "SEPKO Kasa",
            "short_name": "Kasa",
            "id": "/app/kasa",
            "description": "Maloprodaja i gotovinska fiskalizacija",
            "start_url": "/app/kasa",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#0f0a1a",
            "theme_color": "#5b21b6",
            "lang": "sr-ME",
            "launch_handler": {"client_mode": "focus-existing"},
            "icons": [
                {
                    "src": f"{origin}/static/img/sepko-mark.svg?v=4",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                }
            ],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/kasa/fiskalizuj")
def pos_fiscalize(
    request: Request,
    csrf_token: str = Form(""),
    line_article_id: list[str] = Form(default=[]),
    line_qty: list[str] = Form(default=[]),
    line_price: list[str] = Form(default=[]),
    line_code: list[str] = Form(default=[]),
    line_name: list[str] = Form(default=[]),
    line_vat: list[str] = Form(default=[]),
    payment_method: str = Form("BANKNOTE"),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    wants_json = "application/json" in (request.headers.get("accept") or "")

    def fail(msg: str, *, status: int = 400, loc: str | None = None):
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=status)
        flash(request, msg, "error")
        return redirect(loc or back)

    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        if wants_json:
            return JSONResponse({"ok": False, "error": "Prijava je istekla."}, status_code=401)
        return redirect("/login")

    pwa = (return_to or "").startswith("/app/kasa")
    back = _safe_return(return_to, pwa=pwa)

    if not validate_csrf(request, csrf_token):
        return fail("Nevažeći CSRF token.")

    from sepko.money import parse_amount, parse_nonneg_money

    pay = (payment_method or "BANKNOTE").strip().upper()
    if pay not in CASH_PAY_METHODS:
        return fail("Kasa prihvata samo gotovinu, karticu ili ostalo-gotovina.")

    day = cash_day_summary(db, tenant)
    if not day.has_initial:
        blag = "/blagajna" + ("?from=pwa" if pwa else "")
        if wants_json:
            return JSONResponse(
                {"ok": False, "error": "Prvo otvori blagajnu (INITIAL) za danas.", "redirect": blag},
                status_code=400,
            )
        flash(request, "Prvo otvori blagajnu (INITIAL) za danas.", "error")
        return redirect(blag)

    lines: list[InvoiceLineIn] = []
    for i, aid in enumerate(line_article_id):
        qty = parse_amount(line_qty[i] if i < len(line_qty) and line_qty[i] else "0", quantize=None) or Decimal("0")
        if qty <= 0:
            continue
        gross_unit = parse_nonneg_money(
            line_price[i] if i < len(line_price) and line_price[i] else "0",
            quantize="0.01",
        )
        if gross_unit is None or gross_unit <= 0:
            continue
        try:
            rate = Decimal(str((line_vat[i] if i < len(line_vat) else "21") or "21").replace(",", "."))
        except Exception:
            rate = Decimal("21")
        code = (line_code[i] if i < len(line_code) else "") or "POS"
        name = (line_name[i] if i < len(line_name) else "") or "Artikal"
        if aid and str(aid).isdigit():
            art = (
                db.query(Article)
                .filter(Article.tenant_id == tenant.id, Article.id == int(aid))
                .first()
            )
            if art:
                code = art.code
                name = art.name
                rate = art.vat_rate
                if gross_unit <= 0:
                    gross_unit = _article_sell_gross(art)

        line_gross = (gross_unit * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        factor = Decimal("1") + (rate / Decimal("100"))
        line_net = (line_gross / factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        unit_net = (line_net / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if qty else line_net
        lines.append(
            InvoiceLineIn(
                code=code[:64],
                name=name[:255],
                quantity=qty,
                unit_price_net=unit_net,
                vat_rate=rate,
                total_gross=line_gross,
            )
        )

    if not lines:
        return fail("Korpa je prazna.")

    total_gross = sum((ln.total_gross for ln in lines), Decimal("0"))
    total_net = Decimal("0")
    total_vat = Decimal("0")
    for ln in lines:
        net = (ln.unit_price_net * ln.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat = (ln.total_gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_net += net
        total_vat += vat

    req = FiscalizeRequest(
        issue_datetime=datetime.now(timezone.utc),
        invoice_type="CASH",
        payment_method=pay,
        currency="EUR",
        lines=lines,
        totals=TotalsIn(net=total_net, vat=total_vat, gross=total_gross),
        notes="POS / maloprodaja",
    )
    result = fiscalize_invoice(db, tenant, req)
    if result.status == "fiscalized":
        inv = (
            db.query(Invoice)
            .filter(
                Invoice.tenant_id == tenant.id,
                Invoice.external_id == result.external_id,
            )
            .first()
        )
        msg = f"Fiskalizovano {result.inv_num}. JIKR: {result.jikr}"
        receipt = None
        if inv:
            receipt = f"/racuni/{inv.id}/stampa?{urlencode({'fmt': 'thermal', 'auto': '1'})}"
        if wants_json:
            return JSONResponse(
                {
                    "ok": True,
                    "message": msg,
                    "receipt_url": receipt,
                    "inv_num": result.inv_num,
                }
            )
        flash(request, msg)
        if receipt:
            return redirect(receipt)
        return redirect(back)
    return fail(result.error_message or "Fiskalizacija nije uspjela.")


@router.get("/maloprodaja/promet", response_class=HTMLResponse)
def pos_day_sales(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    day = cash_day_summary(db, tenant)
    today = datetime.now(timezone.utc).date()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.issue_datetime >= start,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.type_of_inv == "CASH",
        )
        .order_by(Invoice.id.desc())
        .all()
    )
    fiscal = load_tenant_fiscal(tenant)
    return render(
        request,
        "pos_day.html",
        {
            "user": user,
            "tenant": tenant,
            "day": day,
            "invoices": invoices,
            "fiscal": fiscal,
        },
    )
