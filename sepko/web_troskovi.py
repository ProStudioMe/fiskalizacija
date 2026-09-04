"""Web UI — troškovnik."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sepko.audit import write_audit
from sepko.db import get_db
from sepko.models import Expense, ExpenseCategory
from sepko.ulazne import d, ensure_expense_categories, month_expense_summary, parse_date
from sepko.web_auth import AuthRequired
from sepko.web_security import flash, redirect, validate_csrf
from sepko.web_templates import render

router = APIRouter(tags=["troskovi"])


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


@router.get("/troskovi", response_class=HTMLResponse)
def troskovi_home(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    today = date.today()
    y = year or today.year
    m = month or today.month
    ensure_expense_categories(db, tenant)
    db.commit()
    categories = (
        db.query(ExpenseCategory)
        .filter(ExpenseCategory.tenant_id == tenant.id, ExpenseCategory.active.is_(True))
        .order_by(ExpenseCategory.name)
        .all()
    )
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    expenses = (
        db.query(Expense)
        .filter(
            Expense.tenant_id == tenant.id,
            Expense.expense_date >= start,
            Expense.expense_date < end,
        )
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .all()
    )
    summary = month_expense_summary(db, tenant, year=y, month=m)
    total = sum(e.amount or Decimal("0") for e in expenses)
    return render(
        request,
        "troskovi.html",
        {
            "user": user,
            "tenant": tenant,
            "categories": categories,
            "expenses": expenses,
            "summary": summary,
            "year": y,
            "month": m,
            "total": total,
        },
    )


@router.post("/troskovi")
def troskovi_create(
    request: Request,
    csrf_token: str = Form(""),
    amount: str = Form(...),
    expense_date: str = Form(""),
    category_id: str = Form(""),
    description: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/troskovi")
    amt = d(amount)
    if amt <= 0:
        flash(request, "Iznos mora biti veći od nule.", "error")
        return redirect("/troskovi")
    cid = int(category_id) if category_id.strip().isdigit() else None
    if cid:
        cat = (
            db.query(ExpenseCategory)
            .filter(ExpenseCategory.tenant_id == tenant.id, ExpenseCategory.id == cid)
            .first()
        )
        if not cat:
            cid = None
    exp = Expense(
        tenant_id=tenant.id,
        category_id=cid,
        amount=amt,
        expense_date=parse_date(expense_date) or date.today(),
        description=(description or "").strip()[:512] or None,
        notes=(notes or "").strip() or None,
    )
    db.add(exp)
    write_audit(
        db,
        "expense.create",
        tenant_id=tenant.id,
        entity_type="expense",
        detail=f"{amt} {description}",
    )
    db.commit()
    flash(request, "Trošak sačuvan.")
    return redirect("/troskovi")


@router.post("/troskovi/{expense_id}/obrisi")
def troskovi_delete(
    request: Request,
    expense_id: int,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    if not validate_csrf(request, csrf_token):
        flash(request, "Nevažeći CSRF token.", "error")
        return redirect("/troskovi")
    exp = (
        db.query(Expense)
        .filter(Expense.tenant_id == tenant.id, Expense.id == expense_id)
        .first()
    )
    if exp:
        db.delete(exp)
        db.commit()
        flash(request, "Trošak obrisan.")
    return redirect("/troskovi")
