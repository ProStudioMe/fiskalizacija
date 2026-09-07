"""Web UI — finansijske kartice, izvodi, mail."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from sepko.brand import MAIL_FROM_NAME
from sepko.db import get_db
from sepko.finansije import (
    MailSettings,
    build_kartica_html,
    customers_with_balance,
    customer_ledger,
    customer_summary,
    fetch_izvodi_from_imap,
    load_mail_settings,
    match_bank_tx_to_expense,
    match_bank_tx_to_incoming,
    safe_filename,
    save_mail_settings,
    send_firm_mail,
)
from sepko.models import (
    BankStatement,
    BankTransaction,
    Customer,
    CustomerPayment,
    ExpenseCategory,
    IncomingInvoice,
    Invoice,
)
from sepko.ulazne import ensure_expense_categories
from sepko.web_auth import AuthRequired
from sepko.web_security import flash, redirect, validate_csrf
from sepko.web_templates import render

router = APIRouter(tags=["finansije"])


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


@router.get("/finansije", response_class=HTMLResponse)
def finansije_home(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    cards = customers_with_balance(db, tenant)
    statements = (
        db.query(BankStatement)
        .filter(BankStatement.tenant_id == tenant.id)
        .order_by(BankStatement.statement_day.desc(), BankStatement.id.desc())
        .limit(30)
        .all()
    )
    pending_tx = (
        db.query(BankTransaction)
        .filter(
            BankTransaction.tenant_id == tenant.id,
            BankTransaction.status == "needs_review",
        )
        .order_by(BankTransaction.id.desc())
        .limit(40)
        .all()
    )
    incoming = (
        db.query(IncomingInvoice)
        .filter(
            IncomingInvoice.tenant_id == tenant.id,
            IncomingInvoice.status.in_(["draft", "recorded"]),
        )
        .order_by(IncomingInvoice.id.desc())
        .limit(50)
        .all()
    )
    ensure_expense_categories(db, tenant)
    db.commit()
    categories = (
        db.query(ExpenseCategory)
        .filter(ExpenseCategory.tenant_id == tenant.id, ExpenseCategory.active.is_(True))
        .order_by(ExpenseCategory.name)
        .all()
    )
    mail = load_mail_settings(tenant)
    total_fakturisano = round(sum(float(c.get("fakturisano") or 0) for c in cards), 2)
    total_uplaceno = round(sum(float(c.get("uplaceno") or 0) for c in cards), 2)
    total_dug = round(sum(float(c.get("dug") or 0) for c in cards), 2)
    return render(
        request,
        "finansije.html",
        {
            "user": user,
            "tenant": tenant,
            "cards": cards,
            "statements": statements,
            "pending_tx": pending_tx,
            "incoming_options": incoming,
            "categories": categories,
            "mail_enabled": mail.enabled,
            "cards_count": len(cards),
            "statements_count": len(statements),
            "pending_count": len(pending_tx),
            "total_fakturisano": total_fakturisano,
            "total_uplaceno": total_uplaceno,
            "total_dug": total_dug,
        },
    )


@router.get("/finansije/kartica/{customer_id}", response_class=HTMLResponse)
def kartica_view(request: Request, customer_id: int, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.id == customer_id)
        .first()
    )
    if not customer:
        flash(request, "Komitent nije pronađen.", "error")
        return redirect("/finansije")
    summary = customer_summary(db, tenant, customer)
    ledger = customer_ledger(db, tenant, customer)
    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.buyer_pib == customer.pib,
            Invoice.status == "fiscalized",
        )
        .order_by(Invoice.issue_datetime.desc())
        .limit(50)
        .all()
    )
    return render(
        request,
        "kartica.html",
        {
            "user": user,
            "tenant": tenant,
            "customer": customer,
            "summary": summary,
            "ledger": ledger,
            "invoices": invoices,
            "mail": load_mail_settings(tenant),
        },
    )


@router.get("/finansije/kartica/{customer_id}/print", response_class=HTMLResponse)
def kartica_print(request: Request, customer_id: int, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.id == customer_id)
        .first()
    )
    if not customer:
        return HTMLResponse("Nije pronađeno", status_code=404)
    summary = customer_summary(db, tenant, customer)
    ledger = customer_ledger(db, tenant, customer)
    html = build_kartica_html(
        naziv=customer.name,
        pib=customer.pib,
        summary=summary,
        ledger=ledger,
        auto_print=True,
    )
    return HTMLResponse(html)


@router.post("/finansije/kartica/{customer_id}/uplata")
def kartica_add_payment(
    request: Request,
    customer_id: int,
    csrf_token: str = Form(""),
    amount: str = Form(...),
    paid_at: str = Form(""),
    note: str = Form(""),
    invoice_id: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/finansije/kartica/{customer_id}")
    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.id == customer_id)
        .first()
    )
    if not customer:
        flash(request, "Komitent nije pronađen.", "error")
        return redirect("/finansije")
    try:
        from sepko.money import parse_nonneg_money

        amt = parse_nonneg_money(amount, quantize="0.01")
        if amt is None:
            raise ValueError("bad amount")
    except Exception:
        flash(request, "Neispravan iznos.", "error")
        return redirect(f"/finansije/kartica/{customer_id}")
    if amt <= 0:
        flash(request, "Iznos mora biti veći od 0.", "error")
        return redirect(f"/finansije/kartica/{customer_id}")
    inv_id = None
    if invoice_id.strip().isdigit():
        inv = (
            db.query(Invoice)
            .filter(Invoice.tenant_id == tenant.id, Invoice.id == int(invoice_id))
            .first()
        )
        if inv:
            inv_id = inv.id
    day = (paid_at or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    db.add(
        CustomerPayment(
            tenant_id=tenant.id,
            customer_id=customer.id,
            invoice_id=inv_id,
            amount=amt,
            paid_at=day[:10],
            note=(note or "").strip() or None,
        )
    )
    db.commit()
    flash(request, f"Uplata {amt:.2f} € evidentirana.")
    return redirect(f"/finansije/kartica/{customer_id}")


@router.post("/finansije/kartica/{customer_id}/posalji")
async def kartica_send_mail(
    request: Request,
    customer_id: int,
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    form = await request.form()
    if not validate_csrf(request, form.get("csrf_token")):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/finansije/kartica/{customer_id}")

    customer = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.id == customer_id)
        .first()
    )
    if not customer:
        flash(request, "Komitent nije pronađen.", "error")
        return redirect("/finansije")

    to_raw = str(form.get("to_email") or customer.email or "")
    to_addrs = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
    include_kartica = str(form.get("attach_kartica") or "") in ("1", "on", "true")
    subject = str(form.get("subject") or f"Finansijska kartica — {customer.name}")
    body = str(
        form.get("body")
        or f"Poštovani,\n\nu prilogu je finansijska kartica za {customer.name}.\n\nSrdačan pozdrav"
    )

    summary = customer_summary(db, tenant, customer)
    ledger = customer_ledger(db, tenant, customer)
    attachments: list[tuple[str, bytes, str]] = []
    if include_kartica:
        html = build_kartica_html(
            naziv=customer.name,
            pib=customer.pib,
            summary=summary,
            ledger=ledger,
            auto_print=False,
        )
        slug = safe_filename(customer.name)
        attachments.append((f"kartica_{slug}.html", html.encode("utf-8"), "text/html"))

    # PDF fakture (postojeći Sepko PDF endpoint sadržaj — generiši jednostavan HTML snapshot po fakturi)
    inv_ids = form.getlist("invoice_ids")
    for raw in inv_ids:
        if not str(raw).isdigit():
            continue
        inv = (
            db.query(Invoice)
            .filter(Invoice.tenant_id == tenant.id, Invoice.id == int(raw))
            .first()
        )
        if not inv:
            continue
        # Ako postoji lokalni PDF fajl u data/fakture — prikači
        from sepko.efi import display_inv_num

        name = safe_filename(display_inv_num(inv)) + ".html"
        html_inv = (
            f"<!DOCTYPE html><html><body style='font-family:sans-serif'>"
            f"<h2>Faktura {display_inv_num(inv)}</h2>"
            f"<p>{inv.buyer_name or ''} · PIB {inv.buyer_pib or '—'}</p>"
            f"<p>Datum: {inv.issue_datetime.strftime('%d.%m.%Y %H:%M') if inv.issue_datetime else '—'}</p>"
            f"<p><strong>Ukupno: {float(inv.total_gross):.2f} €</strong></p>"
            f"<p>IKOF: {inv.ikof or '—'} · JIKR: {inv.jikr or '—'}</p>"
            f"<p><a href='{inv.qr_url or '#'}'>Verifikacija</a></p>"
            f"</body></html>"
        )
        attachments.append((name, html_inv.encode("utf-8"), "text/html"))

    mail = load_mail_settings(tenant)
    result = send_firm_mail(
        mail=mail,
        to_addrs=to_addrs,
        subject=subject,
        body_text=body,
        body_html=None,
        attachments=attachments,
    )
    flash(request, result["message"], "ok" if result["ok"] else "error")
    return redirect(f"/finansije/kartica/{customer_id}")


@router.post("/finansije/izvodi/povuci")
def izvodi_pull(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/finansije")
    result = fetch_izvodi_from_imap(db, tenant)
    flash(request, result.get("message") or "Gotovo.", "ok" if result.get("ok") else "error")
    return redirect("/finansije")


@router.get("/finansije/izvodi/{statement_id}/fajl")
def izvod_file(request: Request, statement_id: int, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    stmt = (
        db.query(BankStatement)
        .filter(BankStatement.tenant_id == tenant.id, BankStatement.id == statement_id)
        .first()
    )
    if not stmt or not stmt.file_path:
        flash(request, "Fajl nije pronađen.", "error")
        return redirect("/finansije")
    path = Path(stmt.file_path)
    if not path.exists():
        flash(request, "Fajl nedostaje na disku.", "error")
        return redirect("/finansije")
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "message/rfc822"
    return Response(path.read_bytes(), media_type=media, headers={
        "Content-Disposition": f'inline; filename="{path.name}"'
    })


@router.get("/podesavanja/mail", response_class=HTMLResponse)
def settings_mail_page(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if user.role != "admin":
        flash(request, "Samo admin.", "error")
        return redirect("/")
    mail = load_mail_settings(tenant)
    return render(
        request,
        "settings_mail.html",
        {"user": user, "tenant": tenant, "mail": mail, "section": "mail"},
    )


@router.post("/podesavanja/mail")
def settings_mail_save(
    request: Request,
    csrf_token: str = Form(""),
    imap_host: str = Form(""),
    imap_port: str = Form("993"),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    imap_folder: str = Form("INBOX"),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_from_name: str = Form(MAIL_FROM_NAME),
    mail_since_date: str = Form("2026-01-01"),
    izvod_subjects: str = Form(""),
    faktura_subjects: str = Form("Faktura"),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if user.role != "admin":
        flash(request, "Samo admin.", "error")
        return redirect("/")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/podesavanja/mail")

    current = load_mail_settings(tenant)
    mail = MailSettings(
        imap_host=imap_host.strip() or current.imap_host,
        imap_port=int(imap_port or 993),
        imap_user=imap_user.strip(),
        imap_password=imap_password.strip() or current.imap_password,
        imap_folder=imap_folder.strip() or "INBOX",
        smtp_host=smtp_host.strip() or imap_host.strip() or current.smtp_host,
        smtp_port=int(smtp_port or 587),
        smtp_user=smtp_user.strip() or imap_user.strip(),
        smtp_password=smtp_password.strip() or current.smtp_password,
        smtp_from=smtp_from.strip() or imap_user.strip(),
        smtp_from_name=smtp_from_name.strip() or MAIL_FROM_NAME,
        mail_since_date=(mail_since_date or "2026-01-01")[:10],
        izvod_subjects=izvod_subjects.strip() or current.izvod_subjects,
        faktura_subjects=faktura_subjects.strip() or "Faktura",
        enabled=bool(imap_user.strip() and (imap_password.strip() or current.imap_password)),
    )
    save_mail_settings(tenant, mail)
    db.commit()
    flash(request, "Mail podešavanja sačuvana.")
    return redirect("/podesavanja/mail")


@router.post("/finansije/tx/{tx_id}/match-ulazna")
def finansije_match_incoming(
    request: Request,
    tx_id: int,
    csrf_token: str = Form(""),
    incoming_id: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/finansije")
    tx = (
        db.query(BankTransaction)
        .filter(BankTransaction.tenant_id == tenant.id, BankTransaction.id == tx_id)
        .first()
    )
    if not tx:
        flash(request, "Stavka nije pronađena.", "error")
        return redirect("/finansije")
    try:
        iid = int(incoming_id)
        match_bank_tx_to_incoming(db, tenant, tx, iid)
        db.commit()
        flash(request, f"Stavka #{tx_id} povezana sa ulaznom #{iid}.")
    except Exception as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect("/finansije")


@router.post("/finansije/tx/{tx_id}/match-trosak")
def finansije_match_expense(
    request: Request,
    tx_id: int,
    csrf_token: str = Form(""),
    category_id: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/finansije")
    tx = (
        db.query(BankTransaction)
        .filter(BankTransaction.tenant_id == tenant.id, BankTransaction.id == tx_id)
        .first()
    )
    if not tx:
        flash(request, "Stavka nije pronađena.", "error")
        return redirect("/finansije")
    try:
        cid = int(category_id) if category_id.strip().isdigit() else None
        _tx, exp = match_bank_tx_to_expense(db, tenant, tx, category_id=cid)
        db.commit()
        flash(request, f"Trošak #{exp.id} kreiran iz bankovne stavke.")
    except Exception as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect("/finansije")


@router.post("/finansije/tx/{tx_id}/ignore")
def finansije_ignore_tx(
    request: Request,
    tx_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/finansije")
    tx = (
        db.query(BankTransaction)
        .filter(BankTransaction.tenant_id == tenant.id, BankTransaction.id == tx_id)
        .first()
    )
    if tx:
        tx.status = "ignored"
        db.commit()
        flash(request, "Stavka ignorisana.")
    return redirect("/finansije")
