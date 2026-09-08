"""Sepko web back-office (Jinja) — Faza 1."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from sepko.audit import write_audit
from sepko.config import get_settings
from sepko.db import get_db
from sepko.efi import (
    TenantCompany,
    TenantUiSettings,
    UI_LANGUAGES,
    load_tenant_company,
    load_tenant_fiscal,
    load_tenant_ui,
    normalize_ui_language,
    parse_display_inv_num,
    display_inv_num,
    save_tenant_company,
    save_tenant_ui,
)
from sepko.catalog import ARTICLE_COLORS, ensure_tax_rates, resolve_vat
from sepko.models import (
    ApiKey,
    Article,
    CashDeposit,
    Category,
    Customer,
    Invoice,
    InvoiceSchedule,
    TaxRate,
    Tenant,
    TenantStatus,
    User,
)
from sepko.schemas import BuyerIn, CashDepositRequest, FiscalizeRequest, InvoiceLineIn, TotalsIn
from sepko.services import (
    cash_day_summary,
    copy_invoices,
    delete_draft_invoice,
    delete_draft_invoices,
    fiscalize_invoice,
    fiscalize_invoices,
    fiscalize_saved_invoice,
    invoice_is_editable,
    preview_inv_num,
    register_cash_deposit,
    save_draft_invoice,
    update_draft_invoice,
)
from sepko.web_auth import (
    AuthRequired,
    login_user,
    logout_user,
    resolve_tenant,
    verify_password,
)
from sepko.web_security import (
    clear_login_attempts,
    flash,
    login_rate_limited,
    record_login_attempt,
    redirect,
    rotate_csrf,
    validate_csrf,
)
from sepko.web_templates import render

router = APIRouter(tags=["web"])

PAGE_SIZES = (10, 20, 50, 100)
DEFAULT_PER_PAGE = 20
PER_PAGE_COOKIE = "sepko_per_page"


def _resolve_per_page(request: Request, per_page: int | None) -> tuple[int, bool]:
    """Vrati (veličina stranice, treba_upisati_cookie). Pamti izbor u sesiji + cookie."""
    if per_page is not None and per_page in PAGE_SIZES:
        request.session["list_per_page"] = per_page
        return per_page, True
    saved = request.session.get("list_per_page")
    if isinstance(saved, int) and saved in PAGE_SIZES:
        return saved, False
    raw = request.cookies.get(PER_PAGE_COOKIE)
    try:
        n = int(raw) if raw is not None else 0
    except ValueError:
        n = 0
    if n in PAGE_SIZES:
        request.session["list_per_page"] = n
        return n, False
    return DEFAULT_PER_PAGE, False


def _with_per_page_cookie(response, per_page: int, set_cookie: bool):
    if set_cookie:
        response.set_cookie(
            PER_PAGE_COOKIE,
            str(per_page),
            max_age=365 * 24 * 3600,
            httponly=False,
            samesite="lax",
            path="/",
        )
    return response


def _parse_nonneg_money(raw: str) -> Decimal | None:
    from sepko.money import parse_nonneg_money

    return parse_nonneg_money(raw)


def _parse_discount_pct(raw: str) -> Decimal:
    """Popust 0–100 %. Neispravan unos → 0."""
    from sepko.money import parse_discount_pct

    return parse_discount_pct(raw)


def _paginate(query, page: int, per_page: int):
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = max(1, min(page, pages))
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, total, page, pages


def _auth(request: Request, db: Session) -> tuple[User, Tenant]:
    from sepko.web_auth import get_current_user

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("role") == "superadmin":
        return redirect("/admin")
    if request.session.get("user_id"):
        return redirect("/")
    return render(request, "login.html", {"user": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/login")
    if login_rate_limited(request):
        flash(request, "Previše pokušaja. Sačekajte minut.", "error")
        return redirect("/login")

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        record_login_attempt(request)
        flash(request, "Pogrešan email ili lozinka.", "error")
        return redirect("/login")
    if user.role == "superadmin":
        flash(request, "Koristite prijavu za platformu.", "error")
        return redirect("/admin/login")
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    if not tenant or tenant.status == TenantStatus.suspended.value:
        flash(request, "Nalog firme je suspendovan. Kontaktirajte podršku.", "error")
        return redirect("/login")

    clear_login_attempts(request)
    login_user(request, user)
    rotate_csrf(request)
    write_audit(db, "auth.login", tenant_id=tenant.id, detail=user.email)
    db.commit()
    flash(request, "Uspješna prijava.")
    return redirect("/")


@router.post("/logout")
def logout_submit(
    request: Request,
    csrf_token: str = Form(""),
):
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/")
    logout_user(request)
    return redirect("/login")


@router.get("/logout")
def logout_legacy():
    # GET odjava je uklonjena zbog CSRF; zadržan redirect radi starih bookmarkova.
    return redirect("/login")


def _build_invoice_list(
    request: Request,
    db: Session,
    tenant: Tenant,
    *,
    q: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    per_page: int | None,
    page: int,
    sort: str | None,
    dir: str | None,
    client: str | None,
    tip: str | None,
    list_path: str = "/racuni",
) -> tuple[dict, int, bool]:
    """Shared invoice table context for Pregled and Fakture."""
    status_val = (status or "all").strip()
    client_val = (client or "").strip()
    tip_val = (tip or "").strip()
    sort_key = (sort or "number").strip()
    if sort_key not in _INVOICE_SORTS:
        sort_key = "number"
    sort_dir = "asc" if (dir or "").strip().lower() == "asc" else "desc"

    query = db.query(Invoice).filter(Invoice.tenant_id == tenant.id)
    if q and q.strip():
        q_raw = q.strip()
        term = f"%{q_raw}%"
        search_clauses = [
            Invoice.external_id.ilike(term),
            Invoice.inv_num.ilike(term),
            Invoice.buyer_name.ilike(term),
            Invoice.buyer_pib.ilike(term),
            Invoice.jikr.ilike(term),
            Invoice.ikof.ilike(term),
        ]
        # Lokalni broj 1-1-32/2026 → inv_ord_num + godina
        parsed = parse_display_inv_num(q_raw)
        if parsed:
            ord_num, year = parsed
            search_clauses.append(
                (Invoice.inv_ord_num == ord_num)
                & (func.extract("year", Invoice.issue_datetime) == year)
            )
            # EFI InvNum često: .../32/2026/...
            search_clauses.append(Invoice.inv_num.ilike(f"%/{ord_num}/{year}/%"))
            search_clauses.append(Invoice.external_id.ilike(f"%/{ord_num}/{year}%"))
        else:
            # djelimičan unos tipa 1-1-32
            m_partial = q_raw.replace(" ", "")
            if m_partial.startswith("1-1-") and m_partial[4:].isdigit():
                search_clauses.append(Invoice.inv_ord_num == int(m_partial[4:]))
            elif q_raw.isdigit():
                search_clauses.append(Invoice.inv_ord_num == int(q_raw))
        query = query.filter(or_(*search_clauses))
    if status_val and status_val != "all":
        if status_val == "pending":
            # Nefiskalizovani: nacrti, u toku i neuspješni (sve što nije fiscalized)
            query = query.filter(Invoice.status.in_(["pending", "draft", "failed"]))
        else:
            query = query.filter(Invoice.status == status_val)
    if client_val:
        if client_val == "__none__":
            query = query.filter(or_(Invoice.buyer_name.is_(None), Invoice.buyer_name == ""))
        else:
            # tačno ili ilike (razlike u velikim slovima / razmacima)
            query = query.filter(Invoice.buyer_name.ilike(client_val))
    if tip_val:
        query = query.filter(Invoice.payment_method == tip_val)
    d_from = _parse_date(date_from)
    d_to = _parse_date(date_to)
    if d_from:
        query = query.filter(func.date(Invoice.issue_datetime) >= d_from)
    if d_to:
        query = query.filter(func.date(Invoice.issue_datetime) <= d_to)

    size, set_cookie = _resolve_per_page(request, per_page)
    col = _INVOICE_SORTS[sort_key]
    if sort_key == "number":
        # Broj računa: zadnja (najveći rbr) → prva; nacrti bez broja po id
        year_col = func.extract("year", Invoice.issue_datetime)
        if sort_dir == "asc":
            ordered = query.order_by(
                year_col.asc().nulls_last(),
                Invoice.inv_ord_num.asc().nulls_last(),
                Invoice.id.asc(),
            )
        else:
            # Zadnja → prva; nacrti (bez broja) na vrhu
            ordered = query.order_by(
                year_col.desc().nulls_first(),
                Invoice.inv_ord_num.desc().nulls_first(),
                Invoice.id.desc(),
            )
    else:
        primary = col.asc() if sort_dir == "asc" else col.desc()
        ordered = query.order_by(
            primary,
            Invoice.id.asc() if sort_dir == "asc" else Invoice.id.desc(),
        )
    invoices, total, page, pages = _paginate(ordered, page, size)

    clients = [
        row[0]
        for row in (
            db.query(Invoice.buyer_name)
            .filter(
                Invoice.tenant_id == tenant.id,
                Invoice.buyer_name.isnot(None),
                Invoice.buyer_name != "",
            )
            .distinct()
            .order_by(Invoice.buyer_name)
            .all()
        )
    ]
    tips = [
        row[0]
        for row in (
            db.query(Invoice.payment_method)
            .filter(Invoice.tenant_id == tenant.id)
            .distinct()
            .order_by(Invoice.payment_method)
            .all()
        )
    ]

    filters = {
        "q": q or "",
        "status": status_val,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "per_page": size,
        "sort": sort_key,
        "dir": sort_dir,
        "client": client_val,
        "tip": tip_val,
    }

    def qs(**overrides):
        merged = {**filters, "page": 1, **overrides}
        return _invoice_list_qs(**merged)

    ctx = {
        "invoices": invoices,
        "q": filters["q"],
        "status": status_val,
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
        "per_page": size,
        "page_sizes": PAGE_SIZES,
        "page": page,
        "pages": pages,
        "total": total,
        "sort": sort_key,
        "dir": sort_dir,
        "client": client_val,
        "tip": tip_val,
        "clients": clients,
        "tips": tips,
        "qs": qs,
        "pager_qs": _invoice_list_qs(**{**filters, "page": page}),
        "list_path": list_path,
    }
    return ctx, size, set_cookie


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    q: str | None = Query(None),
    status: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    per_page: int | None = Query(None),
    page: int = Query(1, ge=1),
    sort: str | None = Query("number"),
    dir: str | None = Query("desc"),
    client: str | None = Query(None),
    tip: str | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    today = datetime.now(timezone.utc).date()
    base_q = db.query(Invoice).filter(Invoice.tenant_id == tenant.id)
    count_total = base_q.count()
    count_today = base_q.filter(func.date(Invoice.issue_datetime) == today).count()
    gross_today = (
        db.query(func.coalesce(func.sum(Invoice.total_gross), 0))
        .filter(Invoice.tenant_id == tenant.id, func.date(Invoice.issue_datetime) == today)
        .scalar()
    )
    fiscal = load_tenant_fiscal(tenant)
    day = cash_day_summary(db, tenant, today)

    list_ctx, size, set_cookie = _build_invoice_list(
        request,
        db,
        tenant,
        q=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
        per_page=per_page,
        page=page,
        sort=sort,
        dir=dir,
        client=client,
        tip=tip,
        list_path="/",
    )
    resp = render(
        request,
        "dashboard.html",
        {
            "user": user,
            "tenant": tenant,
            "fiscal": fiscal,
            "day": day,
            "stats": {
                "count_today": count_today,
                "count_total": count_total,
                "gross_today": float(gross_today or 0),
            },
            **list_ctx,
        },
    )
    return _with_per_page_cookie(resp, size, set_cookie)


def _get_article(db: Session, tenant: Tenant, article_id: int) -> Article | None:
    return (
        db.query(Article)
        .filter(Article.tenant_id == tenant.id, Article.id == article_id)
        .first()
    )


def _get_category(db: Session, tenant: Tenant, category_id: int) -> Category | None:
    return (
        db.query(Category)
        .filter(Category.tenant_id == tenant.id, Category.id == category_id)
        .first()
    )


@router.get("/podesavanja/artikli", response_class=HTMLResponse)
def articles_hub(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return render(
        request,
        "articles_hub.html",
        {"user": user, "tenant": tenant},
    )


@router.get("/artikli", response_class=HTMLResponse)
def articles_page(
    request: Request,
    edit: int | None = Query(None),
    q: str | None = Query(None),
    per_page: int | None = Query(None),
    page: int = Query(1, ge=1),
    return_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    ensure_tax_rates(db, tenant)
    query = db.query(Article).filter(Article.tenant_id == tenant.id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(or_(Article.name.ilike(term), Article.code.ilike(term), Article.barcode.ilike(term)))
    size, set_cookie = _resolve_per_page(request, per_page)
    ordered = query.order_by(Article.active.desc(), Article.name)
    articles, total, page, pages = _paginate(ordered, page, size)
    editing = _get_article(db, tenant, edit) if edit else None
    categories = (
        db.query(Category)
        .filter(Category.tenant_id == tenant.id, Category.active.is_(True))
        .order_by(Category.position, Category.name)
        .all()
    )
    tax_rates = ensure_tax_rates(db, tenant)
    from sepko.uploads import article_thumb_public_url

    thumb_url = None
    if editing and editing.thumbnail_filename:
        thumb_url = article_thumb_public_url(editing.id, editing.thumbnail_filename)
    back = _safe_return_to(return_to)
    has_active = (
        db.query(Article.id)
        .filter(Article.tenant_id == tenant.id, Article.active.is_(True))
        .first()
        is not None
    )
    resp = render(
        request,
        "articles.html",
        {
            "user": user,
            "tenant": tenant,
            "articles": articles,
            "editing": editing,
            "categories": categories,
            "tax_rates": tax_rates,
            "colors": ARTICLE_COLORS,
            "thumb_url": thumb_url,
            "q": q or "",
            "per_page": size,
            "page_sizes": PAGE_SIZES,
            "page": page,
            "pages": pages,
            "total": total,
            "return_to": back or "",
            "has_active_articles": has_active,
        },
    )
    return _with_per_page_cookie(resp, size, set_cookie)


@router.post("/artikli")
async def articles_create(
    request: Request,
    csrf_token: str = Form(""),
    code: str = Form(...),
    name: str = Form(...),
    price_gross: str = Form(...),
    price_retail: str = Form(""),
    stock_qty: str = Form(""),
    barcode: str = Form(""),
    description: str = Form(""),
    color: str = Form("#c62828"),
    category_id: str = Form(""),
    tax_rate_code: str = Form("PDV21"),
    unit: str = Form("KOM"),
    thumb: UploadFile | None = File(None),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    back_artikli = _artikli_url(return_to)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(back_artikli)

    exists = (
        db.query(Article)
        .filter(Article.tenant_id == tenant.id, Article.code == code.strip())
        .first()
    )
    if exists:
        flash(request, "Artikal sa tom šifrom već postoji.", "error")
        return redirect(back_artikli)

    vat, tax_code = resolve_vat(db, tenant, tax_rate_code.strip() or None)
    cat_id = int(category_id) if category_id.strip().isdigit() else None
    if cat_id and not _get_category(db, tenant, cat_id):
        cat_id = None
    price = _parse_nonneg_money(price_gross)
    retail = _parse_nonneg_money(price_retail) if price_retail.strip() else None
    stock = _parse_nonneg_money(stock_qty) if stock_qty.strip() else None
    if price is None or (price_retail.strip() and retail is None) or (stock_qty.strip() and stock is None):
        flash(request, "Cijena/količina mora biti nula ili pozitivna (max 10.000.000).", "error")
        return redirect(back_artikli)
    article = Article(
        tenant_id=tenant.id,
        category_id=cat_id,
        code=code.strip(),
        name=name.strip(),
        unit=unit.strip() or "KOM",
        price_gross=price,
        price_retail=retail,
        stock_qty=stock,
        barcode=(barcode.strip() or None),
        description=(description.strip() or None),
        color=(color.strip() or "#c62828"),
        vat_rate=vat,
        tax_rate_code=tax_code,
        active=True,
    )
    db.add(article)
    db.flush()
    try:
        from sepko.uploads import save_article_thumb

        article.thumbnail_filename = await save_article_thumb(
            tenant.id, article.id, thumb, previous=None
        )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(back_artikli)
    write_audit(
        db,
        "article.create",
        tenant_id=tenant.id,
        entity_type="article",
        detail=f"{code.strip()} price={price}",
    )
    db.commit()
    flash(request, "Artikal sačuvan.")
    return redirect(_safe_return_to(return_to) or "/artikli")


@router.post("/artikli/{article_id}")
async def articles_update(
    request: Request,
    article_id: int,
    csrf_token: str = Form(""),
    name: str = Form(...),
    price_gross: str = Form(...),
    price_retail: str = Form(""),
    stock_qty: str = Form(""),
    barcode: str = Form(""),
    description: str = Form(""),
    color: str = Form("#c62828"),
    category_id: str = Form(""),
    tax_rate_code: str = Form("PDV21"),
    unit: str = Form("KOM"),
    thumb: UploadFile | None = File(None),
    remove_thumb: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/artikli")
    article = _get_article(db, tenant, article_id)
    if not article:
        flash(request, "Artikal nije pronađen.", "error")
        return redirect("/artikli")
    vat, tax_code = resolve_vat(db, tenant, tax_rate_code.strip() or None)
    cat_id = int(category_id) if category_id.strip().isdigit() else None
    if cat_id and not _get_category(db, tenant, cat_id):
        cat_id = None
    price = _parse_nonneg_money(price_gross)
    retail = _parse_nonneg_money(price_retail) if price_retail.strip() else None
    stock = _parse_nonneg_money(stock_qty) if stock_qty.strip() else None
    if price is None or (price_retail.strip() and retail is None) or (stock_qty.strip() and stock is None):
        flash(request, "Cijena/količina mora biti nula ili pozitivna (max 10.000.000).", "error")
        return redirect("/artikli")
    old_price = article.price_gross
    article.name = name.strip()
    article.unit = unit.strip() or "KOM"
    article.price_gross = price
    article.price_retail = retail
    article.stock_qty = stock
    article.barcode = barcode.strip() or None
    article.description = description.strip() or None
    article.color = color.strip() or "#c62828"
    article.category_id = cat_id
    article.vat_rate = vat
    article.tax_rate_code = tax_code
    article.active = True
    try:
        from sepko.uploads import delete_article_thumb, save_article_thumb

        if remove_thumb in ("1", "on", "true"):
            delete_article_thumb(tenant.id, article.thumbnail_filename)
            article.thumbnail_filename = None
        else:
            article.thumbnail_filename = await save_article_thumb(
                tenant.id, article.id, thumb, previous=article.thumbnail_filename
            )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/artikli?edit={article_id}")
    if old_price != price:
        write_audit(
            db,
            "article.price",
            tenant_id=tenant.id,
            entity_type="article",
            entity_id=article.id,
            detail=f"{article.code} {old_price} → {price}",
        )
    db.commit()
    flash(request, f"Artikal {article.code} izmijenjen.")
    return redirect("/artikli")


@router.get("/artikli/{article_id:int}/thumb")
def article_thumb(
    request: Request,
    article_id: int,
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    article = _get_article(db, tenant, article_id)
    if not article or not article.thumbnail_filename:
        return Response(status_code=404)
    from sepko.uploads import resolve_article_thumb

    path = resolve_article_thumb(tenant.id, article.thumbnail_filename)
    if not path:
        return Response(status_code=404)
    return FileResponse(path)


@router.post("/artikli/{article_id}/obrisi")
def articles_delete(
    request: Request,
    article_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/artikli")
    article = _get_article(db, tenant, article_id)
    if not article:
        flash(request, "Artikal nije pronađen.", "error")
        return redirect("/artikli")
    article.active = False
    db.commit()
    flash(request, f"Artikal {article.code} deaktiviran.")
    return redirect("/artikli")


@router.get("/artikli/kategorije", response_class=HTMLResponse)
def categories_page(
    request: Request,
    edit: int | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    categories = (
        db.query(Category)
        .filter(Category.tenant_id == tenant.id)
        .order_by(Category.position, Category.name)
        .all()
    )
    editing = _get_category(db, tenant, edit) if edit else None
    return render(
        request,
        "categories.html",
        {
            "user": user,
            "tenant": tenant,
            "categories": categories,
            "editing": editing,
            "colors": ARTICLE_COLORS,
        },
    )


@router.post("/artikli/kategorije")
def categories_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(...),
    position: str = Form("1"),
    color: str = Form("#e65100"),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/artikli/kategorije")
    try:
        pos = int(position)
    except ValueError:
        pos = 1
    db.add(
        Category(
            tenant_id=tenant.id,
            name=name.strip(),
            position=pos,
            color=color.strip() or "#e65100",
            active=True,
        )
    )
    db.commit()
    flash(request, "Kategorija sačuvana.")
    return redirect("/artikli/kategorije")


@router.post("/artikli/kategorije/{category_id}")
def categories_update(
    request: Request,
    category_id: int,
    csrf_token: str = Form(""),
    name: str = Form(...),
    position: str = Form("1"),
    color: str = Form("#e65100"),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/artikli/kategorije")
    cat = _get_category(db, tenant, category_id)
    if not cat:
        flash(request, "Kategorija nije pronađena.", "error")
        return redirect("/artikli/kategorije")
    try:
        pos = int(position)
    except ValueError:
        pos = 1
    cat.name = name.strip()
    cat.position = pos
    cat.color = color.strip() or "#e65100"
    cat.active = True
    db.commit()
    flash(request, "Kategorija izmijenjena.")
    return redirect("/artikli/kategorije")


@router.post("/artikli/kategorije/{category_id}/obrisi")
def categories_delete(
    request: Request,
    category_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/artikli/kategorije")
    cat = _get_category(db, tenant, category_id)
    if not cat:
        flash(request, "Kategorija nije pronađena.", "error")
        return redirect("/artikli/kategorije")
    cat.active = False
    db.commit()
    flash(request, "Kategorija deaktivirana.")
    return redirect("/artikli/kategorije")


@router.get("/artikli/poreske-stope", response_class=HTMLResponse)
def tax_rates_page(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    rates = ensure_tax_rates(db, tenant)
    return render(
        request,
        "tax_rates.html",
        {"user": user, "tenant": tenant, "tax_rates": rates},
    )


@router.post("/artikli/poreske-stope/{rate_id}/default")
def tax_rates_set_default(
    request: Request,
    rate_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/artikli/poreske-stope")
    rates = db.query(TaxRate).filter(TaxRate.tenant_id == tenant.id).all()
    found = False
    for r in rates:
        if r.id == rate_id:
            r.is_default = True
            found = True
        else:
            r.is_default = False
    if not found:
        flash(request, "Stopa nije pronađena.", "error")
        return redirect("/artikli/poreske-stope")
    db.commit()
    flash(request, "Podrazumijevana poreska stopa postavljena.")
    return redirect("/artikli/poreske-stope")


# --- Kupci ---


def _get_customer(db: Session, tenant: Tenant, customer_id: int) -> Customer | None:
    return (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.id == customer_id)
        .first()
    )


def _party_lookup_payload(row) -> dict:
    """Zajednička polja komitent/dobavljač za kopiranje forme."""
    return {
        "id": row.id,
        "pib": row.pib or "",
        "pdv_number": row.pdv_number or "",
        "name": row.name or "",
        "street": row.street or "",
        "city": row.city or "",
        "country": row.country or "Crna Gora",
        "email": row.email or "",
        "phone": row.phone or "",
        "contact": row.contact or "",
        "notes": row.notes or "",
    }


@router.get("/kupci/lookup.json")
def customers_lookup(
    request: Request,
    q: str = Query(""),
    db: Session = Depends(get_db),
):
    """Pretraga komitenata za uvoz podataka na formu dobavljača."""
    try:
        _user, tenant = _auth(request, db)
    except AuthRequired:
        return JSONResponse({"items": []}, status_code=401)
    term = (q or "").strip()[:64]
    if len(term) < 1:
        return JSONResponse({"items": []})
    like = f"%{term}%"
    rows = (
        db.query(Customer)
        .filter(
            Customer.tenant_id == tenant.id,
            Customer.active.is_(True),
            or_(
                Customer.pib.ilike(like),
                Customer.name.ilike(like),
                Customer.city.ilike(like),
                Customer.pdv_number.ilike(like),
            ),
        )
        .order_by(Customer.name)
        .limit(15)
        .all()
    )
    return JSONResponse({"items": [_party_lookup_payload(r) for r in rows]})


@router.get("/kupci", response_class=HTMLResponse)
def customers_page(
    request: Request,
    edit: int | None = Query(None),
    q: str | None = Query(None),
    per_page: int | None = Query(None),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    query = db.query(Customer).filter(Customer.tenant_id == tenant.id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Customer.name.ilike(term),
                Customer.pib.ilike(term),
                Customer.city.ilike(term),
                Customer.email.ilike(term),
                Customer.pdv_number.ilike(term),
            )
        )
    size, set_cookie = _resolve_per_page(request, per_page)
    ordered = query.order_by(Customer.active.desc(), Customer.name)
    customers, total, page, pages = _paginate(ordered, page, size)
    editing = _get_customer(db, tenant, edit) if edit else None
    resp = render(
        request,
        "customers.html",
        {
            "user": user,
            "tenant": tenant,
            "customers": customers,
            "editing": editing,
            "q": q or "",
            "per_page": size,
            "page_sizes": PAGE_SIZES,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )
    return _with_per_page_cookie(resp, size, set_cookie)


@router.post("/kupci")
async def customers_create(
    request: Request,
    csrf_token: str = Form(""),
    pib: str = Form(...),
    name: str = Form(...),
    pdv_number: str = Form(""),
    street: str = Form(""),
    city: str = Form(""),
    country: str = Form("Crna Gora"),
    email: str = Form(""),
    phone: str = Form(""),
    tax_card_number: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
    discount_pct: str = Form("0"),
    logo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/kupci")
    pib_clean = pib.strip()
    exists = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.pib == pib_clean)
        .first()
    )
    if exists:
        flash(request, "Kupac sa tim PIB-om već postoji.", "error")
        return redirect("/kupci")
    street_s = street.strip() or None
    city_s = city.strip() or None
    country_s = (country.strip() or "Crna Gora")
    addr_parts = [p for p in (street_s, city_s, country_s) if p]
    customer = Customer(
        tenant_id=tenant.id,
        pib=pib_clean,
        pdv_number=(pdv_number.strip() or None),
        name=name.strip(),
        street=street_s,
        city=city_s,
        country=country_s,
        email=(email.strip() or None),
        phone=(phone.strip() or None),
        tax_card_number=(tax_card_number.strip() or None),
        contact=(contact.strip() or None),
        address=(", ".join(addr_parts) if addr_parts else None),
        notes=(notes.strip() or None),
        discount_pct=_parse_discount_pct(discount_pct),
        active=True,
    )
    db.add(customer)
    db.flush()
    try:
        from sepko.uploads import save_customer_logo

        customer.logo_filename = await save_customer_logo(
            tenant.id, customer.id, logo, previous=None
        )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect("/kupci")
    db.commit()
    flash(request, "Komitent sačuvan.")
    return redirect("/kupci")


@router.post("/kupci/{customer_id}")
async def customers_update(
    request: Request,
    customer_id: int,
    csrf_token: str = Form(""),
    name: str = Form(...),
    pdv_number: str = Form(""),
    street: str = Form(""),
    city: str = Form(""),
    country: str = Form("Crna Gora"),
    email: str = Form(""),
    phone: str = Form(""),
    tax_card_number: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
    discount_pct: str = Form("0"),
    remove_logo: str = Form(""),
    logo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/kupci")
    customer = _get_customer(db, tenant, customer_id)
    if not customer:
        flash(request, "Kupac nije pronađen.", "error")
        return redirect("/kupci")
    customer.name = name.strip()
    customer.pdv_number = pdv_number.strip() or None
    customer.street = street.strip() or None
    customer.city = city.strip() or None
    customer.country = country.strip() or "Crna Gora"
    customer.email = email.strip() or None
    customer.phone = phone.strip() or None
    customer.tax_card_number = tax_card_number.strip() or None
    customer.contact = contact.strip() or None
    customer.notes = notes.strip() or None
    customer.discount_pct = _parse_discount_pct(discount_pct)
    customer.address = customer.composed_address()
    customer.active = True

    from sepko.uploads import delete_customer_logo, save_customer_logo

    if remove_logo in ("1", "on", "true"):
        delete_customer_logo(tenant.id, customer.logo_filename)
        customer.logo_filename = None
    else:
        try:
            customer.logo_filename = await save_customer_logo(
                tenant.id, customer.id, logo, previous=customer.logo_filename
            )
        except ValueError as exc:
            flash(request, str(exc), "error")
            return redirect(f"/kupci?edit={customer_id}")

    db.commit()
    flash(request, f"Komitent {customer.pib} izmijenjen.")
    return redirect("/kupci")


@router.get("/kupci/{customer_id}/logo")
def customer_logo(request: Request, customer_id: int, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    customer = _get_customer(db, tenant, customer_id)
    if not customer or not customer.logo_filename:
        flash(request, "Logo nije pronađen.", "error")
        return redirect("/kupci")
    from sepko.uploads import resolve_customer_logo

    path = resolve_customer_logo(tenant.id, customer.logo_filename)
    if not path:
        flash(request, "Logo fajl nedostaje.", "error")
        return redirect("/kupci")
    return FileResponse(path)


@router.post("/kupci/{customer_id}/obrisi")
def customers_delete(
    request: Request,
    customer_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/kupci")
    customer = _get_customer(db, tenant, customer_id)
    if not customer:
        flash(request, "Kupac nije pronađen.", "error")
        return redirect("/kupci")
    customer.active = False
    db.commit()
    flash(request, f"Kupac {customer.pib} deaktiviran.")
    return redirect("/kupci")


# --- Blagajna / depozit ---


@router.get("/blagajna", response_class=HTMLResponse)
def cash_page(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    day = cash_day_summary(db, tenant)
    today = datetime.now(timezone.utc).date()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    deposits = (
        db.query(CashDeposit)
        .filter(CashDeposit.tenant_id == tenant.id, CashDeposit.change_datetime >= start)
        .order_by(CashDeposit.id.desc())
        .all()
    )
    fiscal = load_tenant_fiscal(tenant)
    ui = load_tenant_ui(tenant)
    from decimal import Decimal
    from sepko.money import format_amount, parse_nonneg_money

    default_initial = parse_nonneg_money(ui.default_opening_cash, quantize="0.01") or Decimal("0")
    override = bool(ui.allow_opening_cash_override)
    initial_locked = (
        day.has_initial
        and day.initial == default_initial
        and not override
    )
    allow_initial = (not day.has_initial) or override or (day.has_initial and day.initial != default_initial)
    allow_withdraw = day.banknote_sales > 0
    suggested_withdraw = day.banknote_sales - day.withdrawals
    if suggested_withdraw < 0:
        suggested_withdraw = Decimal("0")

    if allow_withdraw and (day.has_initial or not allow_initial):
        default_op = "WITHDRAW"
        default_amount = suggested_withdraw
    elif allow_initial:
        default_op = "INITIAL"
        default_amount = default_initial
    else:
        default_op = ""
        default_amount = Decimal("0")

    from_pwa = (request.query_params.get("from") or "").strip().lower() == "pwa"
    return render(
        request,
        "cash.html",
        {
            "user": user,
            "tenant": tenant,
            "day": day,
            "deposits": deposits,
            "fiscal": fiscal,
            "from_pwa": from_pwa,
            "kasa_url": "/app/kasa" if from_pwa else "/kasa",
            "default_op": default_op,
            "default_amount": format_amount(default_amount),
            "suggest_initial": format_amount(default_initial),
            "suggest_withdraw": format_amount(suggested_withdraw),
            "allow_initial": allow_initial,
            "allow_withdraw": allow_withdraw,
            "initial_locked": initial_locked,
            "opening_override": override,
            "max_initial": format_amount(default_initial) if not override else "",
        },
    )


@router.post("/blagajna")
def cash_submit(
    request: Request,
    csrf_token: str = Form(""),
    operation: str = Form("INITIAL"),
    amount: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    back = "/blagajna?from=pwa" if (return_to or "").startswith("/app/kasa") else "/blagajna"
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(back)
    try:
        amt = _parse_nonneg_money(amount)
        if amt is None:
            raise ValueError("bad amount")
    except Exception:
        flash(request, "Neispravan iznos.", "error")
        return redirect(back)

    op = (operation or "").strip().upper()
    if op not in ("INITIAL", "WITHDRAW"):
        flash(request, "Nepoznata operacija.", "error")
        return redirect(back)

    day = cash_day_summary(db, tenant)
    ui = load_tenant_ui(tenant)
    from decimal import Decimal
    from sepko.money import format_amount, parse_nonneg_money

    default_initial = parse_nonneg_money(ui.default_opening_cash, quantize="0.01") or Decimal("0")
    override = bool(ui.allow_opening_cash_override)

    if op == "WITHDRAW":
        if day.banknote_sales <= 0:
            flash(request, "Nema gotovinske prodaje za podizanje.", "error")
            return redirect(back)
    else:
        initial_locked = (
            day.has_initial
            and day.initial == default_initial
            and not override
        )
        if initial_locked:
            flash(
                request,
                "Početni depozit je već na default iznosu. Uključi override u Podešavanjima za izmjenu.",
                "error",
            )
            return redirect(back)
        if not override and amt > default_initial:
            flash(
                request,
                f"Bez override-a početni depozit ne smije biti veći od {format_amount(default_initial)} €.",
                "error",
            )
            return redirect(back)

    result = register_cash_deposit(
        db,
        tenant,
        CashDepositRequest(operation=op, amount=amt),
    )
    if result.status == "registered":
        flash(request, f"{result.operation} {format_amount(result.amount)} € — registrovano.")
        if (return_to or "").startswith("/app/kasa"):
            return redirect("/app/kasa")
    else:
        flash(request, result.error_message or "Depozit nije uspio.", "error")
    return redirect(back)


# --- Računi ---


def _parse_date(value: str | None):
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


_INVOICE_SORTS = {
    "date": Invoice.issue_datetime,
    "client": Invoice.buyer_name,
    "amount": Invoice.total_gross,
    "number": Invoice.inv_ord_num,
    "status": Invoice.status,
    "tip": Invoice.payment_method,
}


def _invoice_list_qs(
    *,
    q: str = "",
    status: str = "all",
    date_from: str = "",
    date_to: str = "",
    per_page: int = 20,
    page: int = 1,
    sort: str = "number",
    dir: str = "desc",
    client: str = "",
    tip: str = "",
) -> str:
    data = {
        "q": q,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "per_page": str(per_page),
        "page": str(page),
        "sort": sort,
        "dir": dir,
        "client": client,
        "tip": tip,
    }
    # Drop empty optional filters; keep status/sort/dir/per_page always.
    out: dict[str, str] = {}
    for key, val in data.items():
        if key in ("q", "date_from", "date_to", "client", "tip") and not val:
            continue
        if key == "page" and str(val) in ("1", ""):
            continue
        out[key] = val
    return urlencode(out)


@router.get("/racuni", response_class=HTMLResponse)
def invoices_list(
    request: Request,
    q: str | None = Query(None),
    status: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    per_page: int | None = Query(None),
    page: int = Query(1, ge=1),
    sort: str | None = Query("number"),
    dir: str | None = Query("desc"),
    client: str | None = Query(None),
    tip: str | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    list_ctx, size, set_cookie = _build_invoice_list(
        request,
        db,
        tenant,
        q=q,
        status=status,
        date_from=date_from,
        date_to=date_to,
        per_page=per_page,
        page=page,
        sort=sort,
        dir=dir,
        client=client,
        tip=tip,
        list_path="/racuni",
    )
    resp = render(
        request,
        "invoices.html",
        {"user": user, "tenant": tenant, **list_ctx},
    )
    return _with_per_page_cookie(resp, size, set_cookie)


def _safe_return_to(raw: str | None) -> str | None:
    """Dozvoli samo interne putanje (npr. /racuni/raspored?new=1)."""
    from urllib.parse import unquote

    path = unquote((raw or "").strip())
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        return None
    return path[:200]


def _artikli_url(return_to: str | None = None) -> str:
    rt = _safe_return_to(return_to)
    if rt:
        return f"/artikli?return_to={quote(rt)}"
    return "/artikli"


def _schedule_templates(db: Session, tenant: Tenant) -> list[Invoice]:
    """Šabloni za raspored: označeni is_template + već vezani na raspored."""
    used_ids = {
        r[0]
        for r in db.query(InvoiceSchedule.template_invoice_id)
        .filter(InvoiceSchedule.tenant_id == tenant.id)
        .all()
    }
    q = db.query(Invoice).filter(Invoice.tenant_id == tenant.id)
    if used_ids:
        q = q.filter(or_(Invoice.is_template.is_(True), Invoice.id.in_(used_ids)))
    else:
        q = q.filter(Invoice.is_template.is_(True))
    marked = q.order_by(Invoice.is_template.desc(), Invoice.id.desc()).limit(200).all()
    if marked:
        return marked
    # Fallback: nacrti / nefiskalizovane (dok nema označenih šablona)
    return (
        db.query(Invoice)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status.in_(["draft", "pending", "failed"]),
        )
        .order_by(Invoice.id.desc())
        .limit(50)
        .all()
    )


@router.get("/racuni/raspored", response_class=HTMLResponse)
def schedules_page(
    request: Request,
    edit: int | None = Query(None),
    new: int | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    schedules = (
        db.query(InvoiceSchedule)
        .options(joinedload(InvoiceSchedule.template_invoice))
        .filter(InvoiceSchedule.tenant_id == tenant.id)
        .order_by(InvoiceSchedule.active.desc(), InvoiceSchedule.id.desc())
        .all()
    )
    editing = None
    if edit:
        editing = (
            db.query(InvoiceSchedule)
            .filter(InvoiceSchedule.tenant_id == tenant.id, InvoiceSchedule.id == edit)
            .first()
        )
    show_form = bool(editing) or bool(new)
    return render(
        request,
        "schedules.html",
        {
            "user": user,
            "tenant": tenant,
            "schedules": schedules,
            "editing": editing,
            "show_form": show_form,
            "templates": _schedule_templates(db, tenant) if show_form else [],
        },
    )


@router.post("/racuni/raspored")
def schedules_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(...),
    template_invoice_id: int = Form(...),
    days_of_month: str = Form("1"),
    contract_number: str = Form(""),
    period_mode: str = Form("previous"),
    email_to: str = Form(""),
    auto_fiscalize: str = Form(""),
    auto_email: str = Form(""),
    active: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni/raspored")
    from sepko.schedules import format_days_of_month, normalize_period_mode, parse_days_of_month

    tmpl = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == template_invoice_id)
        .first()
    )
    if not tmpl:
        flash(request, "Šablon fakture nije pronađen.", "error")
        return redirect("/racuni/raspored")
    customer_id = None
    if tmpl.buyer_pib:
        cust = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant.id, Customer.pib == tmpl.buyer_pib)
            .first()
        )
        if cust:
            customer_id = cust.id
    days = format_days_of_month(parse_days_of_month(days_of_month))
    if not tmpl.is_template and tmpl.status != "fiscalized":
        tmpl.is_template = True
    db.add(
        InvoiceSchedule(
            tenant_id=tenant.id,
            name=name.strip() or f"Automatska {tmpl.id}",
            template_invoice_id=tmpl.id,
            customer_id=customer_id,
            days_of_month=days,
            contract_number=(contract_number.strip() or None),
            period_mode=normalize_period_mode(period_mode),
            email_to=(email_to.strip() or None),
            auto_fiscalize=auto_fiscalize in ("1", "on", "true"),
            auto_email=auto_email in ("1", "on", "true"),
            active=active in ("1", "on", "true"),
        )
    )
    db.commit()
    flash(request, "Automatska faktura sačuvana.")
    return redirect("/racuni/raspored")


@router.post("/racuni/raspored/pokreni")
def schedules_run_due(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni/raspored")
    from sepko.schedules import run_due_schedules

    result = run_due_schedules(db, tenant_id=tenant.id)
    flash(
        request,
        f"Pokrenuto: {result['ok']}, preskočeno: {result['skipped']}, greške: {result['failed']}.",
        "error" if result["failed"] and not result["ok"] else "ok",
    )
    return redirect("/racuni/raspored")


@router.post("/racuni/raspored/{schedule_id:int}")
def schedules_update(
    request: Request,
    schedule_id: int,
    csrf_token: str = Form(""),
    name: str = Form(...),
    template_invoice_id: int = Form(...),
    days_of_month: str = Form("1"),
    contract_number: str = Form(""),
    period_mode: str = Form("previous"),
    email_to: str = Form(""),
    auto_fiscalize: str = Form(""),
    auto_email: str = Form(""),
    active: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni/raspored")
    from sepko.schedules import format_days_of_month, normalize_period_mode, parse_days_of_month

    sch = (
        db.query(InvoiceSchedule)
        .filter(InvoiceSchedule.tenant_id == tenant.id, InvoiceSchedule.id == schedule_id)
        .first()
    )
    if not sch:
        flash(request, "Automatska faktura nije pronađena.", "error")
        return redirect("/racuni/raspored")
    tmpl = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == template_invoice_id)
        .first()
    )
    if not tmpl:
        flash(request, "Šablon fakture nije pronađen.", "error")
        return redirect("/racuni/raspored")
    customer_id = None
    if tmpl.buyer_pib:
        cust = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant.id, Customer.pib == tmpl.buyer_pib)
            .first()
        )
        if cust:
            customer_id = cust.id
    sch.name = name.strip() or sch.name
    sch.template_invoice_id = tmpl.id
    sch.customer_id = customer_id
    sch.days_of_month = format_days_of_month(parse_days_of_month(days_of_month))
    sch.contract_number = contract_number.strip() or None
    sch.period_mode = normalize_period_mode(period_mode)
    sch.email_to = email_to.strip() or None
    sch.auto_fiscalize = auto_fiscalize in ("1", "on", "true")
    sch.auto_email = auto_email in ("1", "on", "true")
    sch.active = active in ("1", "on", "true")
    if not tmpl.is_template and tmpl.status != "fiscalized":
        tmpl.is_template = True
    db.commit()
    flash(request, "Automatska faktura ažurirana.")
    return redirect("/racuni/raspored")


@router.post("/racuni/raspored/{schedule_id:int}/pokreni")
def schedules_run_one(
    request: Request,
    schedule_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni/raspored")
    from sepko.schedules import run_schedule

    sch = (
        db.query(InvoiceSchedule)
        .filter(InvoiceSchedule.tenant_id == tenant.id, InvoiceSchedule.id == schedule_id)
        .first()
    )
    if not sch:
        flash(request, "Automatska faktura nije pronađena.", "error")
        return redirect("/racuni/raspored")
    result = run_schedule(db, tenant, sch, force=True)
    flash(request, result.get("message") or "Gotovo.", "ok" if result.get("ok") else "error")
    return redirect("/racuni/raspored")


@router.post("/racuni/raspored/{schedule_id:int}/obrisi")
def schedules_delete(
    request: Request,
    schedule_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni/raspored")
    sch = (
        db.query(InvoiceSchedule)
        .filter(InvoiceSchedule.tenant_id == tenant.id, InvoiceSchedule.id == schedule_id)
        .first()
    )
    if not sch:
        flash(request, "Automatska faktura nije pronađena.", "error")
        return redirect("/racuni/raspored")
    sch.active = False
    db.commit()
    flash(request, "Automatska faktura deaktivirana.")
    return redirect("/racuni/raspored")


@router.post("/racuni/raspored/{schedule_id:int}/aktiviraj")
def schedules_activate(
    request: Request,
    schedule_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni/raspored")
    sch = (
        db.query(InvoiceSchedule)
        .filter(InvoiceSchedule.tenant_id == tenant.id, InvoiceSchedule.id == schedule_id)
        .first()
    )
    if not sch:
        flash(request, "Automatska faktura nije pronađena.", "error")
        return redirect("/racuni/raspored")
    sch.active = True
    db.commit()
    flash(request, "Automatska faktura aktivirana.")
    return redirect("/racuni/raspored")


@router.get("/racuni/kopiraj")
@router.get("/racuni/bulk-obrisi")
@router.get("/racuni/obrisi-odabrane")
@router.get("/racuni/bulk-fiskalizuj")
@router.get("/racuni/fiskalizuj-odabrane")
def invoices_bulk_get_redirect():
    """GET na POST-only bulk URL (refresh / pogrešan method) → lista, ne /racuni/{id}."""
    return redirect("/racuni")


@router.post("/racuni/kopiraj")
def invoices_copy(
    request: Request,
    csrf_token: str = Form(""),
    invoice_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni")
    seen: set[int] = set()
    ids: list[int] = []
    for raw in invoice_ids:
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)
    if not ids:
        flash(request, "Označite barem jednu fakturu.", "error")
        return redirect("/racuni")
    if len(ids) > 50:
        flash(request, "Najviše 50 faktura odjednom.", "error")
        return redirect("/racuni")
    created = copy_invoices(db, tenant, ids)
    if not created:
        flash(request, "Nije pronađena nijedna faktura za kopiranje.", "error")
        return redirect("/racuni")
    flash(request, f"Kopirano {len(created)} faktura kao nacrti (U pripremi).")
    return redirect("/racuni?status=pending")


@router.post("/racuni/bulk-obrisi")
@router.post("/racuni/obrisi-odabrane")  # alias (stari URL)
def invoices_bulk_delete(
    request: Request,
    csrf_token: str = Form(""),
    invoice_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni")
    seen: set[int] = set()
    ids: list[int] = []
    for raw in invoice_ids:
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)
    if not ids:
        flash(request, "Označite barem jednu fakturu.", "error")
        return redirect("/racuni")
    if len(ids) > 50:
        flash(request, "Najviše 50 faktura odjednom.", "error")
        return redirect("/racuni")
    result = delete_draft_invoices(db, tenant, ids)
    msg = f"Obrisano {result['ok']} nacrta."
    err_list = result.get("errors") or []
    if err_list:
        msg += " " + "; ".join(err_list[:5])
    flash(request, msg, "error" if result["ok"] == 0 else "ok")
    return redirect("/racuni")


@router.post("/racuni/bulk-fiskalizuj")
@router.post("/racuni/fiskalizuj-odabrane")  # alias
def invoices_bulk_fiscalize(
    request: Request,
    csrf_token: str = Form(""),
    invoice_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/racuni")
    seen: set[int] = set()
    ids: list[int] = []
    for raw in invoice_ids:
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)
    if not ids:
        flash(request, "Označite barem jednu fakturu.", "error")
        return redirect("/racuni")
    if len(ids) > 50:
        flash(request, "Najviše 50 faktura odjednom.", "error")
        return redirect("/racuni")
    result = fiscalize_invoices(db, tenant, ids)
    msg = (
        f"Fiskalizovano: {result['ok']}. "
        f"Preskočeno: {result['skipped']}. "
        f"Neuspješno: {result['failed']}."
    )
    err_list = result.get("errors") or []
    if err_list:
        msg += " " + "; ".join(err_list[:5])
    flash(request, msg, "error" if result["failed"] and not result["ok"] else "ok")
    return redirect("/racuni")


@router.post("/racuni/{invoice_id:int}/obrisi")
def invoice_delete(
    request: Request,
    invoice_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/racuni/{invoice_id}")
    invoice = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")
    try:
        delete_draft_invoice(db, tenant, invoice)
        db.commit()
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/racuni/{invoice_id}")
    flash(request, "Nacrt fakture je obrisan.")
    return redirect("/racuni")


def _parse_invoice_notes(notes: str | None) -> dict[str, str]:
    """Iz notes blob-a izvuci rok, fiskalnu napomenu, opis, popust %, ugovor i period."""
    due_date = ""
    fiscal_note = ""
    discount_pct = "0"
    contract_number = ""
    period = ""
    desc_parts: list[str] = []
    for line in (notes or "").splitlines():
        raw = line.strip()
        low = raw.lower()
        if low.startswith("rok plaćanja:"):
            due_date = raw.split(":", 1)[-1].strip()
        elif low.startswith("napomena fiskalizacija:"):
            fiscal_note = raw.split(":", 1)[-1].strip()
        elif low.startswith("broj ugovora:"):
            contract_number = raw.split(":", 1)[-1].strip()
        elif low.startswith("period:"):
            period = raw.split(":", 1)[-1].strip()
        elif low.startswith("popust na račun:"):
            raw_pct = raw.split(":", 1)[-1].strip().rstrip("%").strip()
            try:
                discount_pct = str(Decimal(raw_pct))
            except Exception:
                discount_pct = "0"
        elif low.startswith("raspored:") or low.startswith("trajna faktura:") or low.startswith("automatska faktura:"):
            continue
        elif raw:
            desc_parts.append(raw)
    due_iso = due_date
    due_display = due_date
    if due_date and "T" not in due_date and len(due_date) == 10 and due_date[4] == "-":
        try:
            due_display = datetime.strptime(due_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            due_iso = due_date
        except ValueError:
            pass
    elif due_date:
        cleaned = due_date.replace(" ", "").rstrip(".")
        try:
            due_iso = datetime.strptime(cleaned, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return {
        "due_date": due_display,
        "due_date_iso": due_iso if len(due_iso) == 10 and due_iso[4:5] == "-" else "",
        "fiscal_note": fiscal_note,
        "desc_note": "\n".join(desc_parts),
        "discount_pct": discount_pct,
        "contract_number": contract_number,
        "period": period,
    }


def _build_fiscalize_request_from_form(
    db: Session,
    tenant: Tenant,
    *,
    article_ids: list[str],
    qtys: list[str],
    prices: list[str],
    line_discounts: list[str],
    line_codes: list[str] | None = None,
    line_names: list[str] | None = None,
    line_vats: list[str] | None = None,
    discount_pct: str = "0",
    payment_method: str = "BANKNOTE",
    customer_id: str = "",
    buyer_pib: str = "",
    buyer_name: str = "",
    due_date: str = "",
    fiscal_note: str = "",
    notes: str = "",
    external_id: str | None = None,
) -> tuple[FiscalizeRequest | None, str | None]:
    """Zajednički parser forme nove/izmjene fakture. Vraća (req, None) ili (None, error)."""
    from sepko.money import parse_amount, parse_discount_pct, parse_nonneg_money

    line_codes = line_codes or []
    line_names = line_names or []
    line_vats = line_vats or []
    lines: list[InvoiceLineIn] = []
    for i, aid in enumerate(article_ids):
        qty = parse_amount(qtys[i] if i < len(qtys) and qtys[i] else "0", quantize=None) or Decimal("0")
        if qty <= 0:
            continue
        article = None
        if aid and str(aid).lstrip("-").isdigit():
            try:
                article = (
                    db.query(Article)
                    .filter(Article.tenant_id == tenant.id, Article.id == int(aid))
                    .first()
                )
            except (TypeError, ValueError):
                article = None

        disc = parse_discount_pct(line_discounts[i] if i < len(line_discounts) else "0")

        if article:
            unit_net = parse_nonneg_money(
                prices[i] if i < len(prices) and prices[i] else article.price_gross,
                quantize=None,
            )
            if unit_net is None:
                unit_net = Decimal(str(article.price_gross))
            vat_rate = article.vat_rate
            code = article.code
            name = article.name
            if unit_net <= 0:
                return None, f"Artikal „{article.name}” nema cijenu. Unesi VP cijenu u šifrarniku."
            if not (article.tax_rate_code or "").strip():
                return None, f"Artikal „{article.name}” nema dodijeljenu PDV stopu."
        else:
            code = (line_codes[i] if i < len(line_codes) else "") or ""
            name = (line_names[i] if i < len(line_names) else "") or ""
            if not code and not name:
                continue
            unit_net = parse_nonneg_money(
                prices[i] if i < len(prices) and prices[i] else "0",
                quantize=None,
            ) or Decimal("0")
            vat_rate = parse_amount(
                line_vats[i] if i < len(line_vats) and line_vats[i] else "",
                quantize="0.01",
            )
            if unit_net <= 0:
                return None, f"Stavka „{name or code}” nema cijenu."
            if vat_rate is None or vat_rate < 0:
                return None, f"Stavka „{name or code}” nema dodijeljen PDV."
            vat_rate = vat_rate

        rate = vat_rate / Decimal("100")
        line_net = (unit_net * qty * (Decimal("1") - disc / Decimal("100"))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        line_vat = (line_net * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross = (line_net + line_vat).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if disc > 0 and "(popust" not in name.lower():
            name = f"{name} (popust {disc}%)"
        eff_unit = (
            (line_net / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if qty
            else unit_net
        )
        lines.append(
            InvoiceLineIn(
                code=code or "STAVKA",
                name=name or "Stavka",
                quantity=qty,
                unit_price_net=eff_unit,
                vat_rate=vat_rate,
                total_gross=gross,
            )
        )

    if not lines:
        return None, "Dodajte barem jednu stavku."

    inv_discount = parse_discount_pct(discount_pct)
    if inv_discount > 0:
        factor = Decimal("1") - inv_discount / Decimal("100")
        adjusted: list[InvoiceLineIn] = []
        for ln in lines:
            line_net = (ln.unit_price_net * ln.quantity * factor).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            rate = ln.vat_rate / Decimal("100")
            line_vat = (line_net * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            gross = (line_net + line_vat).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            eff_unit = (
                (line_net / ln.quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if ln.quantity
                else ln.unit_price_net
            )
            adjusted.append(
                ln.model_copy(update={"unit_price_net": eff_unit, "total_gross": gross})
            )
        lines = adjusted

    total_gross = sum((ln.total_gross for ln in lines), Decimal("0"))
    total_net = Decimal("0")
    total_vat = Decimal("0")
    for ln in lines:
        net = (ln.unit_price_net * ln.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat = (ln.total_gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_net += net
        total_vat += vat

    payment_method = str(payment_method or "BANKNOTE")
    from sepko.efi import CASH_PAY_METHODS, normalize_pay_method

    try:
        payment_method = normalize_pay_method(payment_method)
    except ValueError:
        return None, "Nepoznat tip plaćanja."
    invoice_type = "CASH" if payment_method in CASH_PAY_METHODS else "NONCASH"

    buyer = None
    customer_id = (customer_id or "").strip()
    if customer_id:
        customer = _get_customer(db, tenant, int(customer_id))
        if customer:
            buyer = BuyerIn(
                pib=customer.pib,
                name=customer.name,
                address=customer.composed_address(),
            )
    else:
        buyer_pib = (buyer_pib or "").strip() or None
        buyer_name = (buyer_name or "").strip() or None
        if buyer_pib or buyer_name:
            buyer = BuyerIn(pib=buyer_pib, name=buyer_name)

    note_parts: list[str] = []
    due = (due_date or "").strip()
    if not due:
        due = (datetime.now(timezone.utc).date() + timedelta(days=15)).isoformat()
    note_parts.append(f"Rok plaćanja: {due}")
    fiscal_note = (fiscal_note or "").strip()
    if fiscal_note:
        note_parts.append(f"Napomena fiskalizacija: {fiscal_note}")
    desc = (notes or "").strip()
    if desc:
        note_parts.append(desc)
    if inv_discount > 0:
        note_parts.append(f"Popust na račun: {inv_discount}%")

    req = FiscalizeRequest(
        external_id=external_id,
        issue_datetime=datetime.now(timezone.utc),
        invoice_type=invoice_type,
        payment_method=payment_method,
        currency="EUR",
        buyer=buyer,
        lines=lines,
        totals=TotalsIn(net=total_net, vat=total_vat, gross=total_gross),
        notes="\n".join(note_parts) or None,
    )
    return req, None


def _invoice_editor_context(
    request: Request,
    db: Session,
    user,
    tenant: Tenant,
    *,
    invoice: Invoice | None = None,
    return_to: str | None = None,
    as_template: bool = False,
):
    articles = (
        db.query(Article)
        .filter(Article.tenant_id == tenant.id, Article.active.is_(True))
        .order_by(Article.code)
        .all()
    )
    customers = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.active.is_(True))
        .order_by(Customer.name)
        .all()
    )
    _, next_ord = preview_inv_num(db, tenant)
    day = cash_day_summary(db, tenant)
    due_default = (datetime.now(timezone.utc).date() + timedelta(days=15)).isoformat()

    prefill: dict | None = None
    if invoice is not None:
        parsed = _parse_invoice_notes(invoice.notes)
        articles_by_code = {a.code: a for a in articles}
        seen_ids = {a.id for a in articles}
        # i neaktivni artikli po šifri — da se stavke mogu urediti
        extra_codes = {
            ln.code for ln in invoice.lines if ln.code and ln.code not in articles_by_code
        }
        if extra_codes:
            for a in (
                db.query(Article)
                .filter(Article.tenant_id == tenant.id, Article.code.in_(extra_codes))
                .all()
            ):
                articles_by_code[a.code] = a
                if a.id not in seen_ids:
                    articles.append(a)
                    seen_ids.add(a.id)

        prefill_lines = []
        for ln in invoice.lines:
            art = articles_by_code.get(ln.code)
            # skinuti "(popust X%)" iz imena za prikaz
            name = ln.name or ""
            if " (popust " in name:
                name = name.split(" (popust ", 1)[0]
            prefill_lines.append(
                {
                    "id": art.id if art else f"x-{ln.id}",
                    "code": ln.code,
                    "name": name or (art.name if art else ln.code),
                    "unit": (art.unit if art else "kom"),
                    "price": float(ln.unit_price_net),
                    "vat": float(ln.vat_rate),
                    "tax": (art.tax_rate_code if art else "") or "",
                    "qty": float(ln.quantity),
                    "discount": 0,
                }
            )

        customer_id = ""
        customer_label = ""
        if invoice.buyer_pib:
            cust = (
                db.query(Customer)
                .filter(
                    Customer.tenant_id == tenant.id,
                    Customer.pib == invoice.buyer_pib,
                    Customer.active.is_(True),
                )
                .first()
            )
            if cust:
                customer_id = str(cust.id)
                customer_label = f"{cust.name} ({cust.pib})"
            elif invoice.buyer_name:
                customer_label = f"{invoice.buyer_name} ({invoice.buyer_pib})"
        elif invoice.buyer_name:
            customer_label = invoice.buyer_name

        prefill = {
            "customer_id": customer_id,
            "customer_label": customer_label,
            "buyer_pib": "" if customer_id else (invoice.buyer_pib or ""),
            "buyer_name": "" if customer_id else (invoice.buyer_name or ""),
            "manual_buyer": not customer_id and bool(invoice.buyer_pib or invoice.buyer_name),
            "payment_method": invoice.payment_method or "ORDER",
            "due_date": parsed["due_date_iso"] or due_default,
            "fiscal_note": parsed["fiscal_note"],
            "notes": parsed["desc_note"],
            "discount_pct": float(parsed["discount_pct"] or 0),
            "lines": prefill_lines,
        }

    return {
        "user": user,
        "tenant": tenant,
        "articles": articles,
        "customers": customers,
        "next_inv_num": (
            f"1-1-{next_ord}/{datetime.now(timezone.utc).year}"
            if invoice is None
            else display_inv_num(invoice)
        ),
        "next_ord": next_ord,
        "next_display_num": (
            f"1-1-{next_ord}/{datetime.now(timezone.utc).year}"
            if invoice is None
            else display_inv_num(invoice)
        ),
        "day": day,
        "due_date_default": (
            due_default if invoice is None else (prefill or {}).get("due_date", due_default)
        ),
        "editing": invoice,
        "form_action": f"/racuni/{invoice.id}" if invoice else "/racuni/novi",
        "prefill": prefill,
        "return_to": return_to or "",
        "as_template": bool(as_template or (invoice.is_template if invoice else False)),
    }


@router.get("/racuni/novi", response_class=HTMLResponse)
def invoice_new_page(
    request: Request,
    return_to: str | None = Query(None),
    as_template: int | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    safe_return = _safe_return_to(return_to)
    ctx = _invoice_editor_context(
        request,
        db,
        user,
        tenant,
        return_to=safe_return,
        as_template=bool(as_template),
    )
    # Bez artikala UI fakture nema šta da ponudi — vodi na šifrarnik, pa nazad.
    if not ctx["articles"]:
        flash(request, "Prvo dodaj barem jedan artikal, pa napravi fakturu.")
        next_path = "/racuni/novi"
        q: list[str] = []
        if as_template:
            q.append("as_template=1")
        if safe_return:
            q.append(f"return_to={quote(safe_return)}")
        if q:
            next_path = f"{next_path}?{'&'.join(q)}"
        return redirect(_artikli_url(next_path))
    return render(request, "invoice_new.html", ctx)


@router.post("/racuni/novi")
def invoice_new_submit(
    request: Request,
    csrf_token: str = Form(""),
    line_article_id: list[str] = Form(default=[]),
    line_qty: list[str] = Form(default=[]),
    line_price: list[str] = Form(default=[]),
    line_discount: list[str] = Form(default=[]),
    line_code: list[str] = Form(default=[]),
    line_name: list[str] = Form(default=[]),
    line_vat: list[str] = Form(default=[]),
    discount_pct: str = Form("0"),
    payment_method: str = Form("BANKNOTE"),
    customer_id: str = Form(""),
    buyer_pib: str = Form(""),
    buyer_name: str = Form(""),
    due_date: str = Form(""),
    fiscal_note: str = Form(""),
    notes: str = Form(""),
    action: str = Form("fiscalize"),
    return_to: str = Form(""),
    as_template: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    back = _safe_return_to(return_to) or "/racuni/novi"
    mark_template = as_template in ("1", "on", "true")

    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(back if back != "/racuni/novi" else "/racuni/novi")

    req, err = _build_fiscalize_request_from_form(
        db,
        tenant,
        article_ids=line_article_id,
        qtys=line_qty,
        prices=line_price,
        line_discounts=line_discount,
        line_codes=line_code,
        line_names=line_name,
        line_vats=line_vat,
        discount_pct=discount_pct,
        payment_method=payment_method,
        customer_id=customer_id,
        buyer_pib=buyer_pib,
        buyer_name=buyer_name,
        due_date=due_date,
        fiscal_note=fiscal_note,
        notes=notes,
    )
    if err or not req:
        flash(request, err or "Neispravna forma.", "error")
        q = []
        if mark_template:
            q.append("as_template=1")
        rt = _safe_return_to(return_to)
        if rt:
            q.append(f"return_to={quote(rt)}")
        return redirect("/racuni/novi" + (("?" + "&".join(q)) if q else ""))

    action = str(action or "fiscalize").strip().lower()
    if action == "save" or mark_template:
        # Šablon se uvijek čuva kao nacrt (bez fiskalizacije)
        inv = save_draft_invoice(db, tenant, req, is_template=mark_template)
        if mark_template:
            flash(request, "Šablon sačuvan. Možeš ga vezati na automatsku fakturu.")
            return redirect(_safe_return_to(return_to) or "/racuni/raspored")
        flash(request, "Faktura sačuvana kao nacrt (U pripremi).")
        return redirect(f"/racuni/{inv.id}")

    result = fiscalize_invoice(db, tenant, req)
    if result.status == "fiscalized":
        flash(request, f"Fiskalizovano {display_inv_num(result)}. JIKR: {result.jikr}")
        inv = (
            db.query(Invoice)
            .filter(Invoice.tenant_id == tenant.id, Invoice.external_id == result.external_id)
            .first()
        )
        if inv:
            return redirect(f"/racuni/{inv.id}")
        return redirect("/racuni")

    flash(request, result.error_message or "Fiskalizacija nije uspjela.", "error")
    return redirect("/racuni/novi")


@router.get("/racuni/{invoice_id:int}/izmijeni", response_class=HTMLResponse)
def invoice_edit_page(
    request: Request,
    invoice_id: int,
    return_to: str | None = Query(None),
    as_template: int | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")
    if not invoice_is_editable(invoice):
        flash(request, "Fiskalizovani račun se ne može mijenjati. Napravi novi šablon ili kopiraj kao nacrt.", "error")
        return redirect(_safe_return_to(return_to) or f"/racuni/{invoice_id}")
    ctx = _invoice_editor_context(
        request,
        db,
        user,
        tenant,
        invoice=invoice,
        return_to=_safe_return_to(return_to),
        as_template=bool(as_template) or bool(invoice.is_template),
    )
    return render(request, "invoice_new.html", ctx)


@router.post("/racuni/{invoice_id:int}")
def invoice_edit_submit(
    request: Request,
    invoice_id: int,
    csrf_token: str = Form(""),
    line_article_id: list[str] = Form(default=[]),
    line_qty: list[str] = Form(default=[]),
    line_price: list[str] = Form(default=[]),
    line_discount: list[str] = Form(default=[]),
    line_code: list[str] = Form(default=[]),
    line_name: list[str] = Form(default=[]),
    line_vat: list[str] = Form(default=[]),
    discount_pct: str = Form("0"),
    payment_method: str = Form("BANKNOTE"),
    customer_id: str = Form(""),
    buyer_pib: str = Form(""),
    buyer_name: str = Form(""),
    due_date: str = Form(""),
    fiscal_note: str = Form(""),
    notes: str = Form(""),
    action: str = Form("save"),
    return_to: str = Form(""),
    as_template: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    back = _safe_return_to(return_to)
    mark_template = as_template in ("1", "on", "true")

    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")
    if not invoice_is_editable(invoice):
        flash(request, "Fiskalizovani račun se ne može mijenjati.", "error")
        return redirect(back or f"/racuni/{invoice_id}")

    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/racuni/{invoice_id}/izmijeni")

    req, err = _build_fiscalize_request_from_form(
        db,
        tenant,
        article_ids=line_article_id,
        qtys=line_qty,
        prices=line_price,
        line_discounts=line_discount,
        line_codes=line_code,
        line_names=line_name,
        line_vats=line_vat,
        discount_pct=discount_pct,
        payment_method=payment_method,
        customer_id=customer_id,
        buyer_pib=buyer_pib,
        buyer_name=buyer_name,
        due_date=due_date,
        fiscal_note=fiscal_note,
        notes=notes,
        external_id=invoice.external_id,
    )
    if err or not req:
        flash(request, err or "Neispravna forma.", "error")
        return redirect(f"/racuni/{invoice_id}/izmijeni")

    try:
        update_draft_invoice(
            db,
            tenant,
            invoice,
            req,
            is_template=True if mark_template else None,
        )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(back or f"/racuni/{invoice_id}")

    action = str(action or "save").strip().lower()
    if mark_template or action == "save":
        if mark_template:
            flash(request, "Šablon ažuriran.")
            return redirect(back or "/racuni/raspored")
        flash(request, "Izmjene sačuvane (U pripremi).")
        return redirect(back or f"/racuni/{invoice_id}")

    if action == "fiscalize":
        try:
            result = fiscalize_saved_invoice(db, tenant, invoice)
        except Exception as exc:
            flash(request, f"Greška: {exc}", "error")
            return redirect(f"/racuni/{invoice_id}")
        if result.status == "fiscalized":
            flash(request, f"Fiskalizovano {display_inv_num(result)}. JIKR: {result.jikr}")
        else:
            flash(request, result.error_message or "Fiskalizacija nije uspjela.", "error")
        return redirect(f"/racuni/{invoice_id}")

    flash(request, "Izmjene sačuvane (U pripremi).")
    return redirect(back or f"/racuni/{invoice_id}")


@router.post("/racuni/{invoice_id:int}/fiskalizuj")
def invoice_fiscalize_one(
    request: Request,
    invoice_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/racuni/{invoice_id}")
    invoice = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")
    if invoice.status == "fiscalized":
        flash(request, "Račun je već fiskalizovan.")
        return redirect(f"/racuni/{invoice_id}")
    try:
        result = fiscalize_saved_invoice(db, tenant, invoice)
    except Exception as exc:
        flash(request, f"Greška: {exc}", "error")
        return redirect(f"/racuni/{invoice_id}")
    if result.status == "fiscalized":
        flash(request, f"Fiskalizovano {display_inv_num(result)}. JIKR: {result.jikr}")
    else:
        flash(request, result.error_message or "Fiskalizacija nije uspjela.", "error")
    return redirect(f"/racuni/{invoice_id}")


@router.get("/racuni/{invoice_id:int}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    invoice = (
        db.query(Invoice)
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")

    readonly = not invoice_is_editable(invoice)
    parsed = _parse_invoice_notes(invoice.notes)
    due_date = parsed["due_date"]
    fiscal_note = parsed["fiscal_note"]
    desc_note = parsed["desc_note"]
    contract_number = parsed.get("contract_number") or ""
    period = parsed.get("period") or ""

    pay_labels = {
        "BANKNOTE": "Gotovina",
        "CARD": "Kartica",
        "BUSINESSCARD": "Poslovna kartica",
        "ORDER": "Virman",
        "ADVANCE": "Avans",
        "OTHER": "Drugo bezgotovinsko",
        "ACCOUNT": "Na račun",
    }
    buyer_customer: Customer | None = None
    if invoice.buyer_pib:
        buyer_customer = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant.id,
                Customer.pib == invoice.buyer_pib,
            )
            .first()
        )
    return render(
        request,
        "invoice_view.html",
        {
            "user": user,
            "tenant": tenant,
            "invoice": invoice,
            "readonly": readonly,
            "due_date": due_date,
            "fiscal_note": fiscal_note,
            "desc_note": desc_note,
            "contract_number": contract_number,
            "period": period,
            "pay_label": pay_labels.get(invoice.payment_method, invoice.payment_method),
            "buyer_customer": buyer_customer,
        },
    )


@router.get("/racuni/{invoice_id:int}/pdf", response_class=HTMLResponse)
def invoice_pdf(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    return _render_invoice_print(request, invoice_id, db, force="a4")


@router.get("/racuni/{invoice_id:int}/stampa", response_class=HTMLResponse)
def invoice_print(
    request: Request,
    invoice_id: int,
    fmt: str = Query(""),
    auto: str = Query(""),
    back: str = Query(""),
    db: Session = Depends(get_db),
):
    return _render_invoice_print(
        request,
        invoice_id,
        db,
        force=fmt,
        auto_print=auto in ("1", "true", "yes"),
        back_url=back,
    )


@router.get("/racuni/{invoice_id:int}/escpos")
def invoice_escpos(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    ctx = _invoice_print_context(request, invoice_id, db)
    if ctx is None:
        return redirect("/login" if not request.session.get("user_id") else "/racuni")
    if ctx.get("missing"):
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")
    from sepko.efi import CASH_PAY_METHODS
    from sepko.escpos import EscPosOptions, build_receipt

    invoice = ctx["invoice"]
    ui = ctx["ui"]
    try:
        blank_start = int(ui.blank_lines_start or "0")
    except ValueError:
        blank_start = 0
    try:
        blank_end = int(ui.blank_lines_end or "2")
    except ValueError:
        blank_end = 2
    raw = build_receipt(
        seller_name=ctx["tenant"].name,
        seller_pib=ctx["tenant"].pib,
        inv_num=display_inv_num(invoice),
        issue_dt=ctx["issue_dt"],
        buyer_name=invoice.buyer_name,
        lines=ctx["line_rows"],
        total_gross=invoice.total_gross,
        pay_label=ctx["pay_label"],
        ikof=invoice.ikof,
        jikr=invoice.jikr,
        qr_url=invoice.qr_url,
        options=EscPosOptions(
            blank_start=blank_start,
            blank_end=blank_end,
            paper_cut=ui.paper_cut or "partial",
            open_drawer=ui.open_drawer or "never",
            is_cash=invoice.payment_method in CASH_PAY_METHODS,
        ),
    )
    return Response(content=raw, media_type="application/octet-stream")


def _invoice_print_context(request: Request, invoice_id: int, db: Session) -> dict | None:
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return None
    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.tenant_id == tenant.id, Invoice.id == invoice_id)
        .first()
    )
    if not invoice:
        return {"missing": True, "user": user, "tenant": tenant}

    ui = load_tenant_ui(tenant)
    company = load_tenant_company(tenant)
    fiscal = load_tenant_fiscal(tenant)

    notes = invoice.notes or ""
    due_date = ""
    desc_parts: list[str] = []
    for line in notes.splitlines():
        raw = line.strip()
        if raw.lower().startswith("rok plaćanja:"):
            due_date = raw.split(":", 1)[-1].strip()
        elif raw.lower().startswith("napomena fiskalizacija:"):
            continue
        elif raw.lower().startswith("popust na račun:"):
            continue
        elif raw:
            desc_parts.append(raw)
    if due_date and len(due_date) == 10 and due_date[4] == "-":
        try:
            due_date = datetime.strptime(due_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            pass

    pay_labels = {
        "BANKNOTE": "Gotovina",
        "CARD": "Kartica",
        "BUSINESSCARD": "Poslovna kartica",
        "ORDER": "Virman",
        "ADVANCE": "Avans",
        "OTHER": "Drugo bezgotovinsko",
        "ACCOUNT": "Na račun",
        "OTHER-CASH": "Ostalo gotovina",
    }

    buyer_pdv = ""
    buyer_logo_url = ""
    if invoice.buyer_pib:
        cust = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant.id, Customer.pib == invoice.buyer_pib)
            .first()
        )
        if cust and cust.pdv_number:
            buyer_pdv = cust.pdv_number
        if cust and cust.logo_filename:
            buyer_logo_url = f"/kupci/{cust.id}/logo"

    article_units: dict[str, str] = {}
    codes = [ln.code for ln in invoice.lines if ln.code]
    if codes:
        for art in (
            db.query(Article)
            .filter(Article.tenant_id == tenant.id, Article.code.in_(codes))
            .all()
        ):
            article_units[art.code] = art.unit or "kom"

    line_rows = []
    net_before = Decimal("0")
    exempt_map: dict[str, Decimal] = {}
    for ln in invoice.lines:
        qty = Decimal(str(ln.quantity))
        vp = Decimal(str(ln.unit_price_net))
        rate = Decimal(str(ln.vat_rate))
        net_val = (qty * vp).quantize(Decimal("0.0001"))
        vat_amt = (net_val * rate / Decimal("100")).quantize(Decimal("0.01"))
        unit_gross = (vp * (Decimal("1") + rate / Decimal("100"))).quantize(Decimal("0.0001"))
        gross = Decimal(str(ln.total_gross))
        net_before += net_val
        if rate == 0:
            exempt_map["Oslobođeno PDV-a"] = exempt_map.get("Oslobođeno PDV-a", Decimal("0")) + net_val
        from sepko.money import format_amount

        qty_fmt = f"{int(qty)}" if ui.qty_no_decimals else format_amount(qty, 3)
        line_rows.append(
            {
                "code": ln.code,
                "name": ln.name,
                "unit": (article_units.get(ln.code) or "kom").lower(),
                "qty": qty_fmt,
                "vp": format_amount(vp, 4),
                "net": format_amount(net_val, 4),
                "discount": "0%",
                "vat_label": f"{rate:.0f}%" if rate else "0%",
                "vat_amt": format_amount(vat_amt, 2),
                "unit_gross": format_amount(unit_gross, 4),
                "gross": format_amount(gross, 2),
            }
        )

    exempt_rows = [
        {"label": label, "amount": format_amount(amt, 2)}
        for label, amt in exempt_map.items()
    ]

    issuer_name = (user.full_name or user.email or "").strip()
    op = fiscal.operator_code or ""
    issuer_label = f"{issuer_name} ({op})" if op else issuer_name or "—"

    issue_dt = ""
    if invoice.issue_datetime:
        issue_dt = invoice.issue_datetime.strftime("%d.%m.%Y %H:%M:%S")

    from sepko.qrutil import qr_data_uri

    try:
        qr_px = max(120, min(280, int(ui.qr_width_px or "180")))
    except ValueError:
        qr_px = 180

    qr_uri = qr_data_uri(invoice.qr_url or "") if invoice.qr_url else None
    qr_fallback = None
    if invoice.qr_url and not qr_uri:
        qr_fallback = (
            f"https://api.qrserver.com/v1/create-qr-code/"
            f"?size={qr_px}x{qr_px}&data={quote(invoice.qr_url, safe='')}"
        )

    return {
        "user": user,
        "tenant": tenant,
        "invoice": invoice,
        "ui": ui,
        "company": company,
        "due_date": due_date,
        "desc_note": "\n".join(desc_parts),
        "pay_label": pay_labels.get(invoice.payment_method, invoice.payment_method),
        "buyer_pdv": buyer_pdv,
        "buyer_logo_url": buyer_logo_url,
        "line_rows": line_rows,
        "exempt_rows": exempt_rows,
        "issuer_label": issuer_label,
        "issue_dt": issue_dt,
        "currency": invoice.currency or "EUR",
        "qr_data_uri": qr_uri,
        "qr_fallback": qr_fallback,
        "qr_size": qr_px,
        "totals": {
            "net_before": net_before.quantize(Decimal("0.01")),
            "discount": Decimal("0.00"),
        },
    }


def _render_invoice_print(
    request: Request,
    invoice_id: int,
    db: Session,
    force: str = "",
    *,
    auto_print: bool = False,
    back_url: str = "",
):
    ctx = _invoice_print_context(request, invoice_id, db)
    if ctx is None:
        return redirect("/login")
    if ctx.get("missing"):
        flash(request, "Račun nije pronađen.", "error")
        return redirect("/racuni")
    ui = ctx["ui"]
    kind = (force or ui.printer_type or "a4").strip().lower()
    template = "invoice_thermal.html" if kind == "thermal" else "invoice_pdf.html"
    ctx["print_agent_url"] = ui.print_agent_url or "ws://127.0.0.1:17890/ws"
    ctx["escpos_url"] = f"/racuni/{invoice_id}/escpos"
    ctx["printer_name"] = ui.printer_name or ""
    ctx["auto_print"] = auto_print
    back = (back_url or "").strip()
    if back.startswith("/") and not back.startswith("//"):
        ctx["back_url"] = back
    else:
        ctx["back_url"] = f"/racuni/{invoice_id}"
    return render(request, template, ctx)


@router.get("/dnevnik", response_class=HTMLResponse)
def audit_dnevnik(request: Request, db: Session = Depends(get_db)):
    """Stari URL — dnevnik je u platform adminu."""
    try:
        _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    flash(request, "Revizijski dnevnik je u platform adminu: Audit → Aktivnost tenanata.", "warn")
    return redirect("/")


@router.get("/informacije", response_class=HTMLResponse)
def info_page(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    fiscal = load_tenant_fiscal(tenant)
    from sepko import __version__
    from sepko.licenses import license_alert, license_days_left, license_label

    alert = license_alert(tenant)
    days = license_days_left(tenant.license_until)

    return render(
        request,
        "info.html",
        {
            "user": user,
            "tenant": tenant,
            "fiscal": fiscal,
            "version": __version__,
            "partner_mode": get_settings().partner_mode,
            "license_alert": alert,
            "license_days": days,
            "license_label": license_label(tenant.license_type),
        },
    )


def _require_admin(request: Request, db: Session) -> tuple[User, Tenant] | None:
    """Vrati (user, tenant) ili None ako nije admin (flash već postavljen). Redirect login ako nema sesije."""
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return None
    if user.role != "admin":
        flash(request, "Samo admin može otvoriti podešavanja.", "error")
        return None
    return user, tenant


@router.get("/podesavanja", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    pair = _require_admin(request, db)
    if pair is None:
        return redirect("/login" if not request.session.get("user_id") else "/")
    user, tenant = pair
    keys = db.query(ApiKey).filter(ApiKey.tenant_id == tenant.id, ApiKey.active.is_(True)).all()
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "tenant": tenant,
            "section": "osnovna",
            "partner_mode": get_settings().partner_mode,
            "api_key_prefixes": [k.key_prefix for k in keys],
            "fiscal": load_tenant_fiscal(tenant),
            "ui": load_tenant_ui(tenant),
            "company": load_tenant_company(tenant),
            "operators": [],
            "editing_op": None,
        },
    )


@router.get("/podesavanja/stampa", response_class=HTMLResponse)
def settings_print_page(request: Request, db: Session = Depends(get_db)):
    pair = _require_admin(request, db)
    if pair is None:
        return redirect("/login" if not request.session.get("user_id") else "/")
    user, tenant = pair
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "tenant": tenant,
            "section": "stampa",
            "partner_mode": get_settings().partner_mode,
            "api_key_prefixes": [],
            "fiscal": load_tenant_fiscal(tenant),
            "ui": load_tenant_ui(tenant),
            "operators": [],
            "editing_op": None,
        },
    )


@router.get("/podesavanja/operateri", response_class=HTMLResponse)
def settings_operators_page(
    request: Request,
    edit: int | None = Query(None),
    db: Session = Depends(get_db),
):
    pair = _require_admin(request, db)
    if pair is None:
        return redirect("/login" if not request.session.get("user_id") else "/")
    user, tenant = pair
    operators = (
        db.query(User)
        .filter(User.tenant_id == tenant.id)
        .order_by(User.active.desc(), User.full_name, User.email)
        .all()
    )
    editing_op = next((o for o in operators if o.id == edit), None) if edit else None
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "tenant": tenant,
            "section": "operateri",
            "partner_mode": get_settings().partner_mode,
            "api_key_prefixes": [],
            "fiscal": load_tenant_fiscal(tenant),
            "ui": load_tenant_ui(tenant),
            "operators": operators,
            "editing_op": editing_op,
        },
    )


@router.post("/podesavanja")
def settings_save_basic(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(...),
    pib: str = Form(...),
    language: str = Form("cnr"),
    max_invoice_amount: str = Form("1000000.00"),
    default_opening_cash: str = Form("0,00"),
    allow_opening_cash_override: str = Form(""),
    pin_length: str = Form("4"),
    auto_login: str = Form(""),
    pin_only_login: str = Form(""),
    a4_item_name_own_line: str = Form(""),
    a4_signature_lines: str = Form(""),
    qty_no_decimals: str = Form(""),
    company_address: str = Form(""),
    company_address2: str = Form(""),
    company_pdv_number: str = Form(""),
    company_bank_account: str = Form(""),
    db: Session = Depends(get_db),
):
    pair = _require_admin(request, db)
    if pair is None:
        return redirect("/login" if not request.session.get("user_id") else "/")
    _user, tenant = pair
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/podesavanja")

    tenant.name = name.strip()
    tenant.pib = pib.strip()
    # mode / EFI / tokeni / website — samo platform admin (/admin/tenanti/…)
    company = load_tenant_company(tenant)
    save_tenant_company(
        tenant,
        TenantCompany(
            address=company_address.strip(),
            address2=company_address2.strip(),
            pdv_number=company_pdv_number.strip(),
            bank_account=company_bank_account.strip(),
            website=company.website,
        ),
    )
    ui = load_tenant_ui(tenant)
    ui.language = normalize_ui_language(language)
    ui.max_invoice_amount = max_invoice_amount.strip() or "1000000.00"
    from decimal import Decimal
    from sepko.money import parse_nonneg_money

    opening = parse_nonneg_money(default_opening_cash, quantize="0.01")
    ui.default_opening_cash = f"{(opening if opening is not None else Decimal('0')):.2f}"
    ui.allow_opening_cash_override = allow_opening_cash_override in ("1", "on", "true")
    try:
        ui.pin_length = int(pin_length)
    except ValueError:
        ui.pin_length = 4
    if ui.pin_length not in (4, 5, 6):
        ui.pin_length = 4
    ui.auto_login = auto_login in ("1", "on", "true")
    ui.pin_only_login = pin_only_login in ("1", "on", "true")
    ui.a4_item_name_own_line = a4_item_name_own_line in ("1", "on", "true")
    ui.a4_signature_lines = a4_signature_lines in ("1", "on", "true")
    ui.qty_no_decimals = qty_no_decimals in ("1", "on", "true")
    save_tenant_ui(tenant, ui)
    db.commit()
    flash(request, "Osnovna podešavanja sačuvana.")
    return redirect("/podesavanja")


@router.post("/podesavanja/stampa")
def settings_save_print(
    request: Request,
    csrf_token: str = Form(""),
    print_enabled: str = Form(""),
    printer_number: str = Form("1"),
    printer_name: str = Form(""),
    printer_type: str = Form(""),
    receipt_width_px: str = Form("576"),
    text_size: str = Form("12"),
    qr_width_px: str = Form("180"),
    blank_lines_start: str = Form("0"),
    blank_lines_end: str = Form("2"),
    max_print_height: str = Form(""),
    paper_cut: str = Form("partial"),
    open_drawer: str = Form("never"),
    print_pause_sec: str = Form("0"),
    print_agent_url: str = Form("ws://127.0.0.1:17890/ws"),
    db: Session = Depends(get_db),
):
    pair = _require_admin(request, db)
    if pair is None:
        return redirect("/login" if not request.session.get("user_id") else "/")
    _user, tenant = pair
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/podesavanja/stampa")

    ui = load_tenant_ui(tenant)
    ui.print_enabled = print_enabled in ("1", "on", "true")
    ui.printer_number = printer_number.strip() or "1"
    ui.printer_name = printer_name.strip()
    ui.printer_type = printer_type.strip()
    ui.receipt_width_px = receipt_width_px.strip() or "576"
    ui.text_size = text_size.strip() or "12"
    ui.qr_width_px = qr_width_px.strip() or "180"
    ui.blank_lines_start = blank_lines_start.strip() or "0"
    ui.blank_lines_end = blank_lines_end.strip() or "2"
    ui.max_print_height = max_print_height.strip()
    ui.paper_cut = paper_cut if paper_cut in ("none", "partial", "full") else "partial"
    ui.open_drawer = open_drawer if open_drawer in ("never", "cash", "always") else "never"
    ui.print_pause_sec = print_pause_sec.strip() or "0"
    ui.print_agent_url = print_agent_url.strip() or "ws://127.0.0.1:17890/ws"
    save_tenant_ui(tenant, ui)
    db.commit()
    flash(request, "Podešavanja štampe sačuvana.")
    return redirect("/podesavanja/stampa")


@router.post("/podesavanja/operateri/{user_id}")
def settings_operator_save(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    full_name: str = Form(...),
    role: str = Form("kasir"),
    active: str = Form(""),
    db: Session = Depends(get_db),
):
    pair = _require_admin(request, db)
    if pair is None:
        return redirect("/login" if not request.session.get("user_id") else "/")
    user, tenant = pair
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/podesavanja/operateri")
    op = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.id == user_id)
        .first()
    )
    if not op:
        flash(request, "Operater nije pronađen.", "error")
        return redirect("/podesavanja/operateri")
    op.full_name = full_name.strip()
    op.role = role if role in ("admin", "kasir") else "kasir"
    want_active = active in ("1", "on", "true")
    if op.id == user.id and not want_active:
        flash(request, "Ne možeš deaktivirati sebe.", "error")
        return redirect(f"/podesavanja/operateri?edit={user_id}")
    op.active = want_active
    db.commit()
    flash(request, "Operater sačuvan.")
    return redirect("/podesavanja/operateri")
