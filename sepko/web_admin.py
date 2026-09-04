"""Platform admin — tenanti, licence, prevodi, audit. Prefix /admin."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from sepko.catalog import ensure_tax_rates
from sepko.db import get_db
from sepko.efi import (
    FISCAL_TOKEN_LABELS,
    FISCAL_TOKEN_PROVIDERS,
    TenantFiscal,
    load_tenant_fiscal,
    save_tenant_fiscal,
)
from sepko.i18n import (
    export_language_map,
    import_language_map,
    list_languages,
    upsert_translation,
)
from sepko.licenses import (
    LICENSE_LABELS,
    LICENSE_TYPES,
    default_license_period,
    license_alert,
    license_days_left,
    license_label,
)
from sepko.models import (
    AdminAuditLog,
    AuditLog,
    Language,
    Tenant,
    TenantStatus,
    Translation,
    TranslationKey,
    User,
)
from sepko.web_auth import (
    AdminAuthRequired,
    AuthRequired,
    get_current_superadmin,
    hash_password,
    login_user,
    logout_user,
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

router = APIRouter(prefix="/admin", tags=["admin"])

_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _admin_ctx(user: User, extra: dict | None = None) -> dict:
    ctx = {
        "user": user,
        "tenant": None,
        "is_admin_portal": True,
        "license_labels": LICENSE_LABELS,
    }
    if extra:
        ctx.update(extra)
    return ctx


def _admin(request: Request, db: Session) -> User:
    return get_current_superadmin(request, db)


def _write_audit(
    db: Session,
    actor: User,
    action: str,
    *,
    tenant_id: int | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            actor_user_id=actor.id,
            actor_email=actor.email,
            tenant_id=tenant_id,
            action=action,
            detail=detail,
        )
    )


def _slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = _SLUG_RE.sub("-", s.lower()).strip("-")
    return (s or "firma")[:64]


def _unique_slug(db: Session, slug: str, *, exclude_id: int | None = None) -> str:
    base = slug or "firma"
    candidate = base
    n = 2
    while True:
        q = db.query(Tenant).filter(Tenant.slug == candidate)
        if exclude_id is not None:
            q = q.filter(Tenant.id != exclude_id)
        if q.first() is None:
            return candidate
        suffix = f"-{n}"
        candidate = (base[: 64 - len(suffix)] + suffix)
        n += 1


def _parse_date(raw: str | None) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_superadmin(request, db)
    except (AuthRequired, AdminAuthRequired):
        return render(request, "admin/login.html", {"user": None, "is_admin_portal": True})
    return redirect("/admin/tenanti")


@router.post("/login")
def admin_login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/admin/login")
    if login_rate_limited(request):
        flash(request, "Previše pokušaja. Sačekajte minut.", "error")
        return redirect("/admin/login")

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if (
        not user
        or user.role != "superadmin"
        or not user.active
        or not verify_password(password, user.password_hash)
    ):
        record_login_attempt(request)
        flash(request, "Pogrešan email ili lozinka.", "error")
        return redirect("/admin/login")

    clear_login_attempts(request)
    login_user(request, user)
    rotate_csrf(request)
    flash(request, "Uspješna prijava u platformu.")
    return redirect("/admin/tenanti")


@router.post("/logout")
def admin_logout(
    request: Request,
    csrf_token: str = Form(""),
):
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/admin")
    logout_user(request)
    return redirect("/admin/login")


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def admin_home(request: Request, db: Session = Depends(get_db)):
    _admin(request, db)
    return redirect("/admin/tenanti")


@router.get("/tenanti", response_class=HTMLResponse)
def tenants_list(
    request: Request,
    q: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    query = db.query(Tenant)
    q_val = (q or "").strip()
    if q_val:
        term = f"%{q_val}%"
        query = query.filter(
            or_(Tenant.name.ilike(term), Tenant.pib.ilike(term), Tenant.slug.ilike(term))
        )
    status_val = (status or "").strip()
    if status_val in {s.value for s in TenantStatus}:
        query = query.filter(Tenant.status == status_val)
    rows = query.order_by(Tenant.name).all()
    today = date.today()
    cards = []
    for t in rows:
        days = license_days_left(t.license_until, today=today)
        alert = license_alert(t, today=today)
        cards.append({"tenant": t, "days": days, "alert": alert, "license_label": license_label(t.license_type)})
    return render(
        request,
        "admin/tenants.html",
        _admin_ctx(
            user,
            {
                "rows": cards,
                "q": q_val,
                "status": status_val,
                "today": today,
            },
        ),
    )


@router.get("/tenanti/novi", response_class=HTMLResponse)
def tenant_new_form(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    start, until = default_license_period("trial")
    return render(
        request,
        "admin/tenant_new.html",
        _admin_ctx(user, {"license_from": start, "license_until": until, "license_types": LICENSE_TYPES}),
    )


@router.post("/tenanti")
def tenant_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(...),
    pib: str = Form(...),
    slug: str = Form(""),
    mode: str = Form("test"),
    license_type: str = Form("trial"),
    license_from: str = Form(""),
    license_until: str = Form(""),
    admin_email: str = Form(...),
    admin_password: str = Form(...),
    admin_name: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/admin/tenanti/novi")

    name_c = name.strip()
    pib_c = pib.strip()
    email_c = admin_email.strip().lower()
    password_c = admin_password.strip()
    if not name_c or not pib_c or not email_c:
        flash(request, "Naziv, PIB i email admina su obavezni.", "error")
        return redirect("/admin/tenanti/novi")
    if len(password_c) < 8:
        flash(request, "Lozinka admina mora imati najmanje 8 znakova.", "error")
        return redirect("/admin/tenanti/novi")
    if db.query(User).filter(User.email == email_c).first():
        flash(request, "Email admina je već zauzet.", "error")
        return redirect("/admin/tenanti/novi")

    kind = license_type if license_type in LICENSE_TYPES else "trial"
    start = _parse_date(license_from)
    until = _parse_date(license_until)
    if start is None or until is None:
        start, until = default_license_period(kind)
    slug_c = _unique_slug(db, (slug.strip().lower() or _slugify(name_c)))
    mode_c = "prod" if mode == "prod" else "test"
    status = TenantStatus.trial.value if kind == "trial" else TenantStatus.active.value

    tenant = Tenant(
        slug=slug_c,
        name=name_c,
        pib=pib_c,
        status=status,
        mode=mode_c,
        license_type=kind,
        license_from=start,
        license_until=until,
    )
    db.add(tenant)
    db.flush()
    db.add(
        User(
            tenant_id=tenant.id,
            email=email_c,
            password_hash=hash_password(password_c),
            full_name=(admin_name or "").strip() or name_c,
            role="admin",
            active=True,
        )
    )
    ensure_tax_rates(db, tenant)
    _write_audit(
        db,
        user,
        "tenant.create",
        tenant_id=tenant.id,
        detail=f"{name_c} PIB {pib_c} slug={slug_c} licenca={kind} do {until}",
    )
    db.commit()
    flash(request, f"Tenant {name_c} je kreiran.")
    return redirect(f"/admin/tenanti/{tenant.id}")


@router.get("/tenanti/{tenant_id}", response_class=HTMLResponse)
def tenant_detail(request: Request, tenant_id: int, db: Session = Depends(get_db)):
    user = _admin(request, db)
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        flash(request, "Tenant nije pronađen.", "error")
        return redirect("/admin/tenanti")
    fiscal = load_tenant_fiscal(tenant)
    operators = (
        db.query(User)
        .filter(User.tenant_id == tenant.id)
        .order_by(User.id)
        .all()
    )
    today = date.today()
    return render(
        request,
        "admin/tenant_detail.html",
        _admin_ctx(
            user,
            {
                "view": tenant,
                "fiscal": fiscal,
                "operators": operators,
                "license_types": LICENSE_TYPES,
                "license_labels": LICENSE_LABELS,
                "token_providers": [(c, FISCAL_TOKEN_LABELS[c]) for c in FISCAL_TOKEN_PROVIDERS],
                "days": license_days_left(tenant.license_until, today=today),
                "alert": license_alert(tenant, today=today),
                "license_label": license_label(tenant.license_type),
            },
        ),
    )


@router.post("/tenanti/{tenant_id}/efi")
def tenant_efi_update(
    request: Request,
    tenant_id: int,
    csrf_token: str = Form(""),
    busin_unit_code: str = Form(""),
    tcr_code: str = Form(""),
    soft_code: str = Form(""),
    operator_code: str = Form(""),
    is_issuer_in_vat: str = Form("true"),
    token_provider: str = Form(""),
    telekom_token: str = Form(""),
    posta_token: str = Form(""),
    clear_telekom_token: str = Form(""),
    clear_posta_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/admin/tenanti/{tenant_id}")
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        flash(request, "Tenant nije pronađen.", "error")
        return redirect("/admin/tenanti")

    prev = load_tenant_fiscal(tenant)
    save_tenant_fiscal(
        tenant,
        TenantFiscal(
            busin_unit_code=busin_unit_code.strip(),
            tcr_code=tcr_code.strip(),
            soft_code=soft_code.strip(),
            operator_code=operator_code.strip(),
            is_issuer_in_vat=is_issuer_in_vat in ("true", "on", "1", "da"),
            token_provider=token_provider.strip().lower(),
        ),
        telekom_token_new=telekom_token,
        posta_token_new=posta_token,
        clear_telekom_token=clear_telekom_token in ("1", "on", "true"),
        clear_posta_token=clear_posta_token in ("1", "on", "true"),
    )
    cur = load_tenant_fiscal(tenant)
    bits = []
    if prev.token_provider != cur.token_provider:
        bits.append(f"kanal {prev.token_provider or '—'}→{cur.token_provider or '—'}")
    if clear_telekom_token in ("1", "on", "true"):
        bits.append("telekom uklonjen")
    elif telekom_token.strip():
        bits.append("telekom postavljen")
    if clear_posta_token in ("1", "on", "true"):
        bits.append("pošta uklonjena")
    elif posta_token.strip():
        bits.append("pošta postavljena")
    _write_audit(
        db,
        user,
        "tenant.efi",
        tenant_id=tenant.id,
        detail="; ".join(bits) if bits else "EFI/kodovi ažurirani",
    )
    db.commit()
    flash(request, "EFI kodovi i tokeni sačuvani.")
    return redirect(f"/admin/tenanti/{tenant.id}")


@router.post("/tenanti/{tenant_id}")
def tenant_update(
    request: Request,
    tenant_id: int,
    csrf_token: str = Form(""),
    name: str = Form(...),
    pib: str = Form(...),
    slug: str = Form(...),
    mode: str = Form("test"),
    license_type: str = Form("trial"),
    license_from: str = Form(""),
    license_until: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/admin/tenanti/{tenant_id}")
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        flash(request, "Tenant nije pronađen.", "error")
        return redirect("/admin/tenanti")

    kind = license_type if license_type in LICENSE_TYPES else tenant.license_type
    start = _parse_date(license_from)
    until = _parse_date(license_until)
    slug_c = _unique_slug(db, slug.strip().lower() or tenant.slug, exclude_id=tenant.id)
    old_lic = f"{tenant.license_type}:{tenant.license_until}"
    tenant.name = name.strip() or tenant.name
    tenant.pib = pib.strip() or tenant.pib
    tenant.slug = slug_c
    tenant.mode = "prod" if mode == "prod" else "test"
    tenant.license_type = kind
    if start is not None:
        tenant.license_from = start
    if until is not None:
        tenant.license_until = until
    new_lic = f"{tenant.license_type}:{tenant.license_until}"
    if old_lic != new_lic:
        _write_audit(
            db,
            user,
            "license.update",
            tenant_id=tenant.id,
            detail=f"{old_lic} → {new_lic}",
        )
    db.commit()
    flash(request, "Tenant je sačuvan.")
    return redirect(f"/admin/tenanti/{tenant.id}")


@router.post("/tenanti/{tenant_id}/status")
def tenant_status(
    request: Request,
    tenant_id: int,
    csrf_token: str = Form(""),
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/admin/tenanti/{tenant_id}")
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        flash(request, "Tenant nije pronađen.", "error")
        return redirect("/admin/tenanti")
    allowed = {s.value for s in TenantStatus}
    new_status = status.strip()
    if new_status not in allowed:
        flash(request, "Nepoznat status.", "error")
        return redirect(f"/admin/tenanti/{tenant_id}")
    old = tenant.status
    tenant.status = new_status
    action = "tenant.suspend" if new_status == TenantStatus.suspended.value else "tenant.status"
    if new_status == TenantStatus.active.value:
        action = "tenant.activate"
    _write_audit(db, user, action, tenant_id=tenant.id, detail=f"{old} → {new_status}")
    db.commit()
    flash(request, f"Status: {new_status}.")
    return redirect(f"/admin/tenanti/{tenant.id}")


@router.get("/prevodi", response_class=HTMLResponse)
def translations_page(
    request: Request,
    lang: str = Query("en"),
    q: str | None = Query(None),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    languages = list_languages(db, active_only=False)
    codes = {c for c, _ in languages}
    lang_c = lang if lang in codes else "en"
    q_val = (q or "").strip()
    keys_q = db.query(TranslationKey).order_by(TranslationKey.category, TranslationKey.key)
    if q_val:
        term = f"%{q_val}%"
        keys_q = keys_q.filter(
            or_(
                TranslationKey.key.ilike(term),
                TranslationKey.description.ilike(term),
                TranslationKey.category.ilike(term),
            )
        )
    keys = keys_q.all()
    values = {
        r.key: r.value
        for r in db.query(Translation).filter(Translation.language_code == lang_c).all()
    }
    cnr_values = {
        r.key: r.value
        for r in db.query(Translation).filter(Translation.language_code == "cnr").all()
    }
    return render(
        request,
        "admin/translations.html",
        _admin_ctx(
            user,
            {
                "languages": languages,
                "lang": lang_c,
                "q": q_val,
                "keys": keys,
                "values": values,
                "cnr_values": cnr_values,
            },
        ),
    )


@router.post("/prevodi")
def translations_save(
    request: Request,
    csrf_token: str = Form(""),
    lang: str = Form("en"),
    q: str = Form(""),
    keys: list[str] = Form(default=[]),
    values: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/admin/prevodi")
    if not db.get(Language, lang):
        flash(request, "Nepoznat jezik.", "error")
        return redirect("/admin/prevodi")
    n = 0
    for key, value in zip(keys, values):
        if not db.get(TranslationKey, key):
            continue
        upsert_translation(db, key, lang, value)
        n += 1
    _write_audit(db, user, "i18n.update", detail=f"lang={lang} keys={n}")
    db.commit()
    flash(request, f"Sačuvano {n} prevoda ({lang}).")
    qs = f"?lang={quote(lang)}"
    if q.strip():
        qs += f"&q={quote(q.strip())}"
    return redirect(f"/admin/prevodi{qs}")


@router.post("/prevodi/kljuc")
def translation_key_create(
    request: Request,
    csrf_token: str = Form(""),
    key: str = Form(...),
    category: str = Form("ui"),
    description: str = Form(""),
    cnr_value: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/admin/prevodi")
    key_c = key.strip()
    if not _KEY_RE.match(key_c):
        flash(request, "Ključ: slova, brojevi, tačka, crtica, donja crta.", "error")
        return redirect("/admin/prevodi")
    if db.get(TranslationKey, key_c):
        flash(request, "Ključ već postoji.", "error")
        return redirect("/admin/prevodi")
    db.add(
        TranslationKey(
            key=key_c,
            category=(category or "ui").strip() or "ui",
            description=(description or "").strip() or None,
        )
    )
    db.flush()
    if cnr_value.strip():
        upsert_translation(db, key_c, "cnr", cnr_value.strip())
    _write_audit(db, user, "i18n.key", detail=key_c)
    db.commit()
    flash(request, f"Ključ {key_c} je dodat.")
    return redirect("/admin/prevodi")


@router.get("/prevodi/export")
def translations_export(
    request: Request,
    lang: str = Query("en"),
    db: Session = Depends(get_db),
):
    _admin(request, db)
    if not db.get(Language, lang):
        flash(request, "Nepoznat jezik.", "error")
        return redirect("/admin/prevodi")
    payload = {
        "language": lang,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "translations": export_language_map(db, lang),
    }
    filename = f"sepko-i18n-{lang}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/prevodi/import")
async def translations_import(
    request: Request,
    csrf_token: str = Form(""),
    lang: str = Form("en"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/admin/prevodi")
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        flash(request, "Fajl nije validan JSON.", "error")
        return redirect("/admin/prevodi")

    mapping: dict[str, str] = {}
    lang_c = lang
    if isinstance(data, dict) and isinstance(data.get("translations"), dict):
        mapping = {str(k): str(v) for k, v in data["translations"].items()}
        if isinstance(data.get("language"), str) and data["language"]:
            lang_c = data["language"]
    elif isinstance(data, dict):
        mapping = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    else:
        flash(request, "Očekivan JSON objekat {key: value} ili {language, translations}.", "error")
        return redirect("/admin/prevodi")

    if not db.get(Language, lang_c):
        flash(request, f"Jezik {lang_c} nije u šifrarniku.", "error")
        return redirect("/admin/prevodi")
    n = import_language_map(db, lang_c, mapping)
    _write_audit(db, user, "i18n.import", detail=f"lang={lang_c} n={n}")
    db.commit()
    flash(request, f"Uvezeno {n} prevoda ({lang_c}).")
    return redirect(f"/admin/prevodi?lang={quote(lang_c)}")


@router.get("/audit", response_class=HTMLResponse)
def audit_list(
    request: Request,
    source: str = Query("platform"),
    tenant_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    src = (source or "platform").strip().lower()
    if src not in ("platform", "tenant"):
        src = "platform"

    tenants_all = (
        db.query(Tenant).order_by(Tenant.name).limit(500).all()
    )
    tenants_map = {t.id: t for t in tenants_all}

    if src == "tenant":
        q = db.query(AuditLog).order_by(AuditLog.id.desc())
        if tenant_id:
            q = q.filter(AuditLog.tenant_id == tenant_id)
        rows = q.limit(300).all()
        need_ids = {r.tenant_id for r in rows if r.tenant_id} - set(tenants_map)
        if need_ids:
            for t in db.query(Tenant).filter(Tenant.id.in_(need_ids)).all():
                tenants_map[t.id] = t
    else:
        q = db.query(AdminAuditLog).order_by(AdminAuditLog.id.desc())
        if tenant_id:
            q = q.filter(AdminAuditLog.tenant_id == tenant_id)
        rows = q.limit(300).all()
        need_ids = {r.tenant_id for r in rows if r.tenant_id} - set(tenants_map)
        if need_ids:
            for t in db.query(Tenant).filter(Tenant.id.in_(need_ids)).all():
                tenants_map[t.id] = t

    return render(
        request,
        "admin/audit.html",
        _admin_ctx(
            user,
            {
                "rows": rows,
                "tenants": tenants_map,
                "tenants_all": tenants_all,
                "source": src,
                "tenant_id": tenant_id,
            },
        ),
    )
