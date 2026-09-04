"""Web UI — ulazne fakture i dobavljači."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.models import IncomingInvoice, IncomingInvoiceStatus, Supplier
from sepko.ulazne import (
    create_from_qr,
    create_incoming_invoice,
    d,
    expense_from_incoming,
    get_or_create_supplier,
    parse_date,
)
from sepko.web_auth import AuthRequired
from sepko.web_security import flash, redirect, validate_csrf
from sepko.web_templates import render

router = APIRouter(tags=["ulazne"])


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


@router.get("/ulazne", response_class=HTMLResponse)
def ulazne_list(
    request: Request,
    status: str = "",
    q: str = "",
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    query = db.query(IncomingInvoice).filter(IncomingInvoice.tenant_id == tenant.id)
    if status.strip():
        query = query.filter(IncomingInvoice.status == status.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            (IncomingInvoice.number.ilike(like))
            | (IncomingInvoice.supplier_name.ilike(like))
            | (IncomingInvoice.supplier_pib.ilike(like))
            | (IncomingInvoice.ikof.ilike(like))
        )
    invoices = query.order_by(IncomingInvoice.id.desc()).limit(100).all()
    return render(
        request,
        "ulazne_list.html",
        {
            "user": user,
            "tenant": tenant,
            "invoices": invoices,
            "filter_status": status,
            "filter_q": q,
        },
    )


@router.get("/ulazne/nova", response_class=HTMLResponse)
def ulazne_new_form(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    suppliers = (
        db.query(Supplier)
        .filter(Supplier.tenant_id == tenant.id, Supplier.active.is_(True))
        .order_by(Supplier.name)
        .all()
    )
    return render(
        request,
        "ulazne_form.html",
        {
            "user": user,
            "tenant": tenant,
            "suppliers": suppliers,
            "invoice": None,
            "prefill": {},
        },
    )


@router.get("/ulazne/qr", response_class=HTMLResponse)
def ulazne_qr_form(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    return render(
        request,
        "ulazne_qr.html",
        {"user": user, "tenant": tenant},
    )


@router.post("/ulazne/qr")
def ulazne_qr_submit(
    request: Request,
    csrf_token: str = Form(""),
    qr_raw: str = Form(""),
    fetch_portal: str = Form("1"),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/ulazne/qr")
    try:
        inv, draft = create_from_qr(
            db, tenant, qr_raw, fetch=fetch_portal not in ("0", "false", "off")
        )
        db.commit()
        msg = f"Ulazna #{inv.id} unesena iz QR."
        if draft.fetch_error:
            msg += f" ({draft.fetch_error})"
        flash(request, msg)
        return redirect(f"/ulazne/{inv.id}")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect("/ulazne/qr")
    except Exception as exc:
        db.rollback()
        flash(request, f"Greška: {exc}", "error")
        return redirect("/ulazne/qr")


@router.post("/ulazne")
def ulazne_create(
    request: Request,
    csrf_token: str = Form(""),
    number: str = Form(""),
    issue_date: str = Form(""),
    supplier_id: str = Form(""),
    supplier_pib: str = Form(""),
    supplier_name: str = Form(""),
    total_net: str = Form("0"),
    total_vat: str = Form("0"),
    total_gross: str = Form("0"),
    notes: str = Form(""),
    status: str = Form("recorded"),
    line_name: str = Form(""),
    line_qty: str = Form("1"),
    line_price: str = Form("0"),
    line_vat: str = Form("21"),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/ulazne/nova")
    try:
        sid = int(supplier_id) if supplier_id.strip().isdigit() else None
        lines = []
        if line_name.strip():
            qty = Decimal(str(line_qty or "1").replace(",", "."))
            from sepko.money import parse_amount, parse_nonneg_money

            price = parse_nonneg_money(line_price, quantize=None) or Decimal("0")
            vat = parse_amount(line_vat, quantize="0.01") or Decimal("21")
            net = (qty * price).quantize(Decimal("0.01"))
            gross = (net * (1 + vat / Decimal("100"))).quantize(Decimal("0.01"))
            lines.append(
                {
                    "code": "",
                    "name": line_name.strip(),
                    "quantity": qty,
                    "unit_price_net": price,
                    "vat_rate": vat,
                    "total_gross": gross,
                }
            )
        inv = create_incoming_invoice(
            db,
            tenant,
            number=number.strip(),
            issue_date=parse_date(issue_date),
            supplier_id=sid,
            supplier_pib=supplier_pib.strip() or None,
            supplier_name=supplier_name.strip() or None,
            status=status.strip() or IncomingInvoiceStatus.recorded.value,
            notes=notes.strip() or None,
            lines=lines or None,
            total_net=d(total_net),
            total_vat=d(total_vat),
            total_gross=d(total_gross),
        )
        db.commit()
        flash(request, f"Ulazna faktura #{inv.id} sačuvana.")
        return redirect(f"/ulazne/{inv.id}")
    except Exception as exc:
        db.rollback()
        flash(request, f"Greška: {exc}", "error")
        return redirect("/ulazne/nova")


@router.get("/ulazne/{invoice_id}", response_class=HTMLResponse)
def ulazne_view(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    inv = (
        db.query(IncomingInvoice)
        .filter(IncomingInvoice.tenant_id == tenant.id, IncomingInvoice.id == invoice_id)
        .first()
    )
    if not inv:
        flash(request, "Ulazna faktura nije pronađena.", "error")
        return redirect("/ulazne")
    return render(
        request,
        "ulazne_view.html",
        {"user": user, "tenant": tenant, "invoice": inv},
    )


@router.post("/ulazne/{invoice_id}/status")
def ulazne_set_status(
    request: Request,
    invoice_id: int,
    csrf_token: str = Form(""),
    status: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect(f"/ulazne/{invoice_id}")
    inv = (
        db.query(IncomingInvoice)
        .filter(IncomingInvoice.tenant_id == tenant.id, IncomingInvoice.id == invoice_id)
        .first()
    )
    if not inv:
        flash(request, "Nije pronađeno.", "error")
        return redirect("/ulazne")
    allowed = {s.value for s in IncomingInvoiceStatus}
    if status not in allowed:
        flash(request, "Nepoznat status.", "error")
        return redirect(f"/ulazne/{invoice_id}")
    inv.status = status
    db.commit()
    flash(request, f"Status: {status}")
    return redirect(f"/ulazne/{invoice_id}")


@router.post("/ulazne/{invoice_id}/trosak")
def ulazne_to_expense(
    request: Request,
    invoice_id: int,
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
        return redirect(f"/ulazne/{invoice_id}")
    inv = (
        db.query(IncomingInvoice)
        .filter(IncomingInvoice.tenant_id == tenant.id, IncomingInvoice.id == invoice_id)
        .first()
    )
    if not inv:
        flash(request, "Nije pronađeno.", "error")
        return redirect("/ulazne")
    cid = int(category_id) if category_id.strip().isdigit() else None
    try:
        exp = expense_from_incoming(db, tenant, inv, category_id=cid)
        db.commit()
        flash(request, f"Trošak #{exp.id} kreiran iz ulazne.")
        return redirect("/troskovi")
    except Exception as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/ulazne/{invoice_id}")


@router.get("/dobavljaci", response_class=HTMLResponse)
def suppliers_list(request: Request, db: Session = Depends(get_db)):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    rows = (
        db.query(Supplier)
        .filter(Supplier.tenant_id == tenant.id)
        .order_by(Supplier.name)
        .all()
    )
    return render(
        request,
        "dobavljaci.html",
        {"user": user, "tenant": tenant, "suppliers": rows},
    )


@router.post("/dobavljaci")
def suppliers_create(
    request: Request,
    csrf_token: str = Form(""),
    pib: str = Form(...),
    name: str = Form(...),
    street: str = Form(""),
    city: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/dobavljaci")
    try:
        s = get_or_create_supplier(db, tenant, pib=pib, name=name, street=street or None, city=city or None)
        if email:
            s.email = email.strip()[:255]
        if phone:
            s.phone = phone.strip()[:64]
        db.commit()
        flash(request, "Dobavljač sačuvan.")
    except Exception as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return redirect("/dobavljaci")
