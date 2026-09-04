from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from sepko.config import get_settings
from sepko.db import SessionLocal
from sepko.efi import UI_LANGUAGES, display_inv_num, load_tenant_ui, normalize_ui_language
from sepko.i18n import get_translations_map, list_languages, t as i18n_t
from sepko.web_security import ensure_csrf, pop_flash

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["display_inv_num"] = display_inv_num

# BCP 47 / HTML lang iz naših UI kodova
_HTML_LANG = {
    "cnr": "sr-ME",
    "cnr-cyrl": "sr-ME-Cyrl",
    "sr": "sr",
    "sr-cyrl": "sr-Cyrl",
    "sq": "sq",
    "tr": "tr",
    "ru": "ru",
    "uk": "uk",
    "en": "en",
}


def _ui_languages() -> list[tuple[str, str]]:
    db = SessionLocal()
    try:
        return list_languages(db)
    except Exception:
        return list(UI_LANGUAGES)
    finally:
        db.close()


def html_lang(code: str) -> str:
    return _HTML_LANG.get(code or "cnr", "sr-ME")


def render(request: Request, name: str, context: dict | None = None):
    ctx = {
        "request": request,
        "csrf_token": ensure_csrf(request),
        "partner_mode": get_settings().partner_mode,
        "ui_languages": _ui_languages(),
        "is_admin_portal": False,
    }
    flash, flash_type = pop_flash(request)
    ctx["flash"] = flash
    ctx["flash_type"] = flash_type
    if context:
        ctx.update(context)

    # Prevodi za jezik tenanta (ako postoji u kontekstu)
    tenant = ctx.get("tenant")
    lang = "cnr"
    if tenant is not None:
        try:
            lang = normalize_ui_language(load_tenant_ui(tenant).language)
        except Exception:
            lang = "cnr"
    db = SessionLocal()
    try:
        bundle = get_translations_map(db, lang)
    except Exception:
        bundle = {}
    finally:
        db.close()
    ctx["lang"] = lang
    ctx["html_lang"] = html_lang(lang)
    ctx["i18n"] = bundle
    ctx["t"] = lambda key, default=None: i18n_t(bundle, key, default)

    return templates.TemplateResponse(request, name, ctx)
