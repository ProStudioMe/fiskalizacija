"""Izvještaji — bogat mjesečni pregled (KPI, grafikoni, tabele)."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import Date, cast, desc, func
from sqlalchemy.orm import Session

from sepko.db import get_db
from sepko.models import (
    CashDeposit,
    Customer,
    CustomerPayment,
    Expense,
    IncomingInvoice,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
)
from sepko.finansije import customer_ledger, customer_summary
from sepko.ulazne import d, month_expense_summary
from sepko.web_auth import AuthRequired
from sepko.web_security import redirect
from sepko.web_templates import render

router = APIRouter(tags=["izvjestaji"])

_PAY_LABELS = {
    "BANKNOTE": "Gotovina",
    "CARD": "Kartica",
    "ORDER": "Virman",
    "OTHER-CASH": "Ostalo (gotovina)",
    "ACCOUNT": "Račun",
    "SVOUCHER": "Vaučer",
    "COMPANY": "Kompanija",
    "ADVANCE": "Avans",
}
_STATUS_LABELS = {
    "fiscalized": "Fiskalizovane",
    "draft": "Nacrti",
    "pending": "U toku",
    "failed": "Greške",
}
_IN_STATUS = {
    "draft": "Nacrt",
    "recorded": "Knjižene",
    "paid": "Plaćene",
    "disputed": "Sporne",
}
_IN_SOURCE = {"manual": "Ručno", "qr": "QR", "mail": "Mail"}
_WEEKDAYS = ("Ned", "Pon", "Uto", "Sri", "Čet", "Pet", "Sub")
_MONTHS = (
    "",
    "Januar",
    "Februar",
    "Mart",
    "April",
    "Maj",
    "Jun",
    "Jul",
    "Avgust",
    "Septembar",
    "Oktobar",
    "Novembar",
    "Decembar",
)


def _auth(request: Request, db: Session):
    from sepko.web_auth import get_current_user, resolve_tenant

    user = get_current_user(request, db)
    tenant = resolve_tenant(user, db)
    return user, tenant


def _period(year: int | None, month: int | None) -> tuple[int, int, date, date]:
    today = date.today()
    y = year or today.year
    m = month or today.month
    if m < 1 or m > 12:
        m = today.month
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return y, m, start, end


def _prev_month(y: int, m: int) -> tuple[int, int, date, date]:
    if m == 1:
        return _period(y - 1, 12)
    return _period(y, m - 1)


def _money(v) -> float:
    return float(d(v))


def _dec(v) -> Decimal:
    return d(v).quantize(Decimal("0.01"))


def _chart_bars(series: list[dict], value_key: str = "value") -> list[dict]:
    vals = [float(x.get(value_key) or 0) for x in series]
    peak = max(vals) if vals else 0
    out = []
    for row, val in zip(series, vals):
        item = dict(row)
        item["value"] = val
        item["height_pct"] = round((val / peak) * 100, 1) if peak > 0 else 0
        out.append(item)
    return out


def _pct_change(curr: float, prev: float) -> float | None:
    if prev == 0:
        return None if curr == 0 else 100.0
    return round(((curr - prev) / abs(prev)) * 100, 1)


def _out_filters(tenant_id: int, start_dt: datetime, end_dt: datetime):
    return (
        Invoice.tenant_id == tenant_id,
        Invoice.status == InvoiceStatus.fiscalized.value,
        Invoice.is_template.is_(False),
        Invoice.fiscalized_at >= start_dt,
        Invoice.fiscalized_at < end_dt,
    )


def _sum_out(db: Session, tenant_id: int, start_dt: datetime, end_dt: datetime) -> tuple[int, float, float, float]:
    f = _out_filters(tenant_id, start_dt, end_dt)
    cnt = db.query(func.count(Invoice.id)).filter(*f).scalar() or 0
    gross = _money(db.query(func.coalesce(func.sum(Invoice.total_gross), 0)).filter(*f).scalar())
    vat = _money(db.query(func.coalesce(func.sum(Invoice.total_vat), 0)).filter(*f).scalar())
    net = _money(db.query(func.coalesce(func.sum(Invoice.total_net), 0)).filter(*f).scalar())
    return int(cnt), gross, vat, net


def _sum_in(db: Session, tenant_id: int, start: date, end: date) -> tuple[int, float, float, float]:
    f = (
        IncomingInvoice.tenant_id == tenant_id,
        IncomingInvoice.issue_date >= start,
        IncomingInvoice.issue_date < end,
    )
    cnt = db.query(func.count(IncomingInvoice.id)).filter(*f).scalar() or 0
    gross = _money(
        db.query(func.coalesce(func.sum(IncomingInvoice.total_gross), 0)).filter(*f).scalar()
    )
    vat = _money(db.query(func.coalesce(func.sum(IncomingInvoice.total_vat), 0)).filter(*f).scalar())
    net = _money(db.query(func.coalesce(func.sum(IncomingInvoice.total_net), 0)).filter(*f).scalar())
    return int(cnt), gross, vat, net


def _sum_exp(db: Session, tenant_id: int, start: date, end: date) -> float:
    return _money(
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(
            Expense.tenant_id == tenant_id,
            Expense.expense_date >= start,
            Expense.expense_date < end,
        )
        .scalar()
    )


@router.get("/izvjestaji", response_class=HTMLResponse)
def izvjestaji_hub(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    y, m, start, end = _period(year, month)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    days_in_month = monthrange(y, m)[1]

    py, pm, p_start, p_end = _prev_month(y, m)
    p_start_dt = datetime(p_start.year, p_start.month, p_start.day, tzinfo=timezone.utc)
    p_end_dt = datetime(p_end.year, p_end.month, p_end.day, tzinfo=timezone.utc)

    out_base = _out_filters(tenant.id, start_dt, end_dt)
    in_base = (
        IncomingInvoice.tenant_id == tenant.id,
        IncomingInvoice.issue_date >= start,
        IncomingInvoice.issue_date < end,
    )
    exp_base = (
        Expense.tenant_id == tenant.id,
        Expense.expense_date >= start,
        Expense.expense_date < end,
    )

    out_count, out_gross, out_vat, out_net = _sum_out(db, tenant.id, start_dt, end_dt)
    in_count, in_gross, in_vat, in_net = _sum_in(db, tenant.id, start, end)
    exp_total = _sum_exp(db, tenant.id, start, end)
    exp_summary = month_expense_summary(db, tenant, year=y, month=m)

    p_out_count, p_out_gross, p_out_vat, _p_out_net = _sum_out(db, tenant.id, p_start_dt, p_end_dt)
    p_in_count, p_in_gross, p_in_vat, _ = _sum_in(db, tenant.id, p_start, p_end)
    p_exp_total = _sum_exp(db, tenant.id, p_start, p_end)

    avg_out = (out_gross / out_count) if out_count else 0.0
    max_out = _money(
        db.query(func.coalesce(func.max(Invoice.total_gross), 0)).filter(*out_base).scalar()
    )
    min_out = _money(
        db.query(func.coalesce(func.min(Invoice.total_gross), 0)).filter(*out_base).scalar()
    ) if out_count else 0.0

    # Dnevni izlaz
    day_rows = (
        db.query(
            cast(Invoice.fiscalized_at, Date).label("day"),
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*out_base)
        .group_by(cast(Invoice.fiscalized_at, Date))
        .all()
    )
    by_day = {r.day: (_money(r.gross), int(r.cnt)) for r in day_rows if r.day}
    daily = []
    for day_n in range(1, days_in_month + 1):
        day = date(y, m, day_n)
        gross, cnt = by_day.get(day, (0.0, 0))
        daily.append({"label": f"{day_n}", "value": gross, "count": cnt})
    daily_chart = _chart_bars(daily)
    best_day = max(daily, key=lambda x: x["value"]) if daily else None
    active_days = sum(1 for x in daily if x["value"] > 0)

    # Dnevni ulaz
    in_day_rows = (
        db.query(
            IncomingInvoice.issue_date.label("day"),
            func.coalesce(func.sum(IncomingInvoice.total_gross), 0).label("gross"),
            func.count(IncomingInvoice.id).label("cnt"),
        )
        .filter(*in_base)
        .group_by(IncomingInvoice.issue_date)
        .all()
    )
    in_by_day = {r.day: (_money(r.gross), int(r.cnt)) for r in in_day_rows if r.day}
    daily_in = []
    for day_n in range(1, days_in_month + 1):
        day = date(y, m, day_n)
        gross, cnt = in_by_day.get(day, (0.0, 0))
        daily_in.append({"label": f"{day_n}", "value": gross, "count": cnt})
    daily_in_chart = _chart_bars(daily_in)

    # Dan u sedmici
    wd_map = {i: 0.0 for i in range(7)}
    wd_cnt = {i: 0 for i in range(7)}
    for day, (gross, cnt) in by_day.items():
        # Python: Monday=0 … Sunday=6 → shift to Sunday=0
        py_wd = day.weekday()  # Mon=0
        sun0 = (py_wd + 1) % 7
        wd_map[sun0] += gross
        wd_cnt[sun0] += cnt
    weekday_chart = _chart_bars(
        [
            {"label": _WEEKDAYS[i], "value": wd_map[i], "count": wd_cnt[i]}
            for i in range(7)
        ]
    )

    # 12-mjesečni trend (završava na izabranom mjesecu)
    trend = []
    ty, tm = y, m
    for _ in range(12):
        ts, te = _period(ty, tm)[2:]
        ts_dt = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
        te_dt = datetime(te.year, te.month, te.day, tzinfo=timezone.utc)
        _, g, _, _ = _sum_out(db, tenant.id, ts_dt, te_dt)
        _, ig, _, _ = _sum_in(db, tenant.id, ts, te)
        eg = _sum_exp(db, tenant.id, ts, te)
        trend.append(
            {
                "label": f"{_MONTHS[tm][:3]} {ty}",
                "month": tm,
                "year": ty,
                "out": g,
                "inn": ig,
                "exp": eg,
            }
        )
        ty, tm, _, _ = _prev_month(ty, tm)
    trend.reverse()
    peak_trend = max([x["out"] for x in trend] + [x["inn"] for x in trend] + [0.01])
    for row in trend:
        row["out_pct"] = round((row["out"] / peak_trend) * 100, 1)
        row["in_pct"] = round((row["inn"] / peak_trend) * 100, 1)

    # Plaćanje
    pay_rows = (
        db.query(
            Invoice.payment_method,
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*out_base)
        .group_by(Invoice.payment_method)
        .order_by(desc("gross"))
        .all()
    )
    by_pay = [
        {
            "label": _PAY_LABELS.get(r.payment_method or "", r.payment_method or "—"),
            "value": _money(r.gross),
            "count": int(r.cnt),
            "share": round((_money(r.gross) / out_gross) * 100, 1) if out_gross else 0,
        }
        for r in pay_rows
    ]
    pay_chart = _chart_bars(by_pay)

    # Tip računa
    type_rows = (
        db.query(
            Invoice.invoice_type,
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*out_base)
        .group_by(Invoice.invoice_type)
        .order_by(desc("gross"))
        .all()
    )
    def _type_label(raw: str | None) -> str:
        t = (raw or "").upper().replace("-", "_")
        if t in ("CASH",):
            return "Gotovina"
        if t in ("NONCASH", "NON_CASH"):
            return "Bezgotovinsko"
        return raw or "—"

    by_type = [
        {
            "label": _type_label(r.invoice_type),
            "value": _money(r.gross),
            "count": int(r.cnt),
        }
        for r in type_rows
    ]
    type_chart = _chart_bars(by_type)

    # Status svih (izdatih u periodu, ne samo fiskalizovanih)
    status_rows = (
        db.query(
            Invoice.status,
            func.count(Invoice.id).label("cnt"),
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
        )
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.is_template.is_(False),
            Invoice.issue_datetime >= start_dt,
            Invoice.issue_datetime < end_dt,
        )
        .group_by(Invoice.status)
        .all()
    )
    by_status = [
        {
            "label": _STATUS_LABELS.get(r.status or "", r.status or "—"),
            "status": r.status or "",
            "count": int(r.cnt),
            "value": _money(r.gross),
        }
        for r in status_rows
    ]
    status_chart = _chart_bars(by_status)

    # PDV stope (stavke)
    vat_rows = (
        db.query(
            InvoiceLine.vat_rate,
            func.coalesce(func.sum(InvoiceLine.total_gross), 0).label("gross"),
            func.count(InvoiceLine.id).label("cnt"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(*out_base)
        .group_by(InvoiceLine.vat_rate)
        .order_by(InvoiceLine.vat_rate)
        .all()
    )
    vat_table = []
    for r in vat_rows:
        rate = float(r.vat_rate or 0)
        gross = _money(r.gross)
        # bruto = neto * (1+rate/100) → neto = bruto / (1+r), pdv = bruto - neto
        denom = 1 + (rate / 100.0)
        net = round(gross / denom, 2) if denom else gross
        vat_amt = round(gross - net, 2)
        vat_table.append(
            {
                "rate": rate,
                "count": int(r.cnt),
                "gross": gross,
                "net": net,
                "vat": vat_amt,
            }
        )
    vat_chart = _chart_bars(
        [{"label": f"{int(v['rate']) if v['rate'] == int(v['rate']) else v['rate']}%", "value": v["vat"]} for v in vat_table]
    )

    # Top komitenti / artikli
    top_buyers = (
        db.query(
            Invoice.buyer_name,
            Invoice.buyer_pib,
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.coalesce(func.sum(Invoice.total_vat), 0).label("vat"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*out_base)
        .group_by(Invoice.buyer_name, Invoice.buyer_pib)
        .order_by(desc("gross"))
        .limit(15)
        .all()
    )
    buyers_table = [
        {
            "name": (r.buyer_name or "Bez kupca").strip() or "Bez kupca",
            "pib": r.buyer_pib or "",
            "gross": _money(r.gross),
            "vat": _money(r.vat),
            "count": int(r.cnt),
            "share": round((_money(r.gross) / out_gross) * 100, 1) if out_gross else 0,
            "customer_id": None,
        }
        for r in top_buyers
    ]
    buyer_pibs = [b["pib"] for b in buyers_table if b["pib"]]
    if buyer_pibs:
        cust_map = {
            c.pib: c.id
            for c in db.query(Customer)
            .filter(Customer.tenant_id == tenant.id, Customer.pib.in_(buyer_pibs))
            .all()
        }
        for b in buyers_table:
            b["customer_id"] = cust_map.get(b["pib"])

    top_arts = (
        db.query(
            InvoiceLine.name,
            InvoiceLine.code,
            func.coalesce(func.sum(InvoiceLine.total_gross), 0).label("gross"),
            func.coalesce(func.sum(InvoiceLine.quantity), 0).label("qty"),
            func.count(InvoiceLine.id).label("lines"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(*out_base)
        .group_by(InvoiceLine.name, InvoiceLine.code)
        .order_by(desc("gross"))
        .limit(15)
        .all()
    )
    articles_table = [
        {
            "name": r.name,
            "code": r.code or "",
            "gross": _money(r.gross),
            "qty": float(r.qty or 0),
            "lines": int(r.lines),
            "share": round((_money(r.gross) / out_gross) * 100, 1) if out_gross else 0,
        }
        for r in top_arts
    ]

    # Najveći računi
    biggest = (
        db.query(Invoice)
        .filter(*out_base)
        .order_by(Invoice.total_gross.desc())
        .limit(10)
        .all()
    )
    biggest_table = [
        {
            "id": inv.id,
            "num": inv.inv_num or inv.external_id,
            "buyer": inv.buyer_name or "—",
            "pib": inv.buyer_pib or "",
            "gross": _money(inv.total_gross),
            "pay": _PAY_LABELS.get(inv.payment_method or "", inv.payment_method or "—"),
            "when": inv.fiscalized_at.strftime("%d.%m.%Y") if inv.fiscalized_at else "",
        }
        for inv in biggest
    ]

    # Dobavljači (ulaz)
    top_suppliers = (
        db.query(
            IncomingInvoice.supplier_name,
            IncomingInvoice.supplier_pib,
            func.coalesce(func.sum(IncomingInvoice.total_gross), 0).label("gross"),
            func.coalesce(func.sum(IncomingInvoice.total_vat), 0).label("vat"),
            func.count(IncomingInvoice.id).label("cnt"),
        )
        .filter(*in_base)
        .group_by(IncomingInvoice.supplier_name, IncomingInvoice.supplier_pib)
        .order_by(desc("gross"))
        .limit(15)
        .all()
    )
    suppliers_table = [
        {
            "name": (r.supplier_name or "Bez dobavljača").strip() or "Bez dobavljača",
            "pib": r.supplier_pib or "",
            "gross": _money(r.gross),
            "vat": _money(r.vat),
            "count": int(r.cnt),
            "share": round((_money(r.gross) / in_gross) * 100, 1) if in_gross else 0,
        }
        for r in top_suppliers
    ]

    in_status_rows = (
        db.query(
            IncomingInvoice.status,
            func.count(IncomingInvoice.id).label("cnt"),
            func.coalesce(func.sum(IncomingInvoice.total_gross), 0).label("gross"),
        )
        .filter(*in_base)
        .group_by(IncomingInvoice.status)
        .all()
    )
    in_status = [
        {
            "label": _IN_STATUS.get(r.status or "", r.status or "—"),
            "count": int(r.cnt),
            "value": _money(r.gross),
        }
        for r in in_status_rows
    ]
    in_status_chart = _chart_bars(in_status)

    in_source_rows = (
        db.query(
            IncomingInvoice.source,
            func.count(IncomingInvoice.id).label("cnt"),
            func.coalesce(func.sum(IncomingInvoice.total_gross), 0).label("gross"),
        )
        .filter(*in_base)
        .group_by(IncomingInvoice.source)
        .all()
    )
    in_source = [
        {
            "label": _IN_SOURCE.get(r.source or "", r.source or "—"),
            "count": int(r.cnt),
            "value": _money(r.gross),
        }
        for r in in_source_rows
    ]
    in_source_chart = _chart_bars(in_source)

    exp_chart = _chart_bars(
        [{"label": s["category"], "value": float(s["total"])} for s in exp_summary]
    )

    # Blagajna / depoziti
    cash_rows = (
        db.query(
            CashDeposit.operation,
            func.coalesce(func.sum(CashDeposit.amount), 0).label("amt"),
            func.count(CashDeposit.id).label("cnt"),
        )
        .filter(
            CashDeposit.tenant_id == tenant.id,
            CashDeposit.change_datetime >= start_dt,
            CashDeposit.change_datetime < end_dt,
        )
        .group_by(CashDeposit.operation)
        .all()
    )
    cash_ops = {
        r.operation: {"amount": _money(r.amt), "count": int(r.cnt)} for r in cash_rows
    }
    cash_initial = cash_ops.get("INITIAL", {"amount": 0.0, "count": 0})
    cash_withdraw = cash_ops.get("WITHDRAW", {"amount": 0.0, "count": 0})

    op_rows = (
        db.query(
            CashDeposit.operator_code,
            func.count(CashDeposit.id).label("cnt"),
            func.coalesce(func.sum(CashDeposit.amount), 0).label("amt"),
        )
        .filter(
            CashDeposit.tenant_id == tenant.id,
            CashDeposit.change_datetime >= start_dt,
            CashDeposit.change_datetime < end_dt,
        )
        .group_by(CashDeposit.operator_code)
        .order_by(desc("cnt"))
        .limit(10)
        .all()
    )
    operators_table = [
        {
            "code": r.operator_code or "—",
            "count": int(r.cnt),
            "amount": _money(r.amt),
        }
        for r in op_rows
        if r.operator_code
    ]

    # Uplate kupaca
    pay_start = start.isoformat()
    pay_end = end.isoformat()
    payments_total = _money(
        db.query(func.coalesce(func.sum(CustomerPayment.amount), 0))
        .filter(
            CustomerPayment.tenant_id == tenant.id,
            CustomerPayment.paid_at >= pay_start,
            CustomerPayment.paid_at < pay_end,
        )
        .scalar()
    )
    pay_by_cust = (
        db.query(
            Customer.name,
            Customer.pib,
            func.coalesce(func.sum(CustomerPayment.amount), 0).label("amt"),
            func.count(CustomerPayment.id).label("cnt"),
        )
        .join(Customer, Customer.id == CustomerPayment.customer_id)
        .filter(
            CustomerPayment.tenant_id == tenant.id,
            CustomerPayment.paid_at >= pay_start,
            CustomerPayment.paid_at < pay_end,
        )
        .group_by(Customer.name, Customer.pib)
        .order_by(desc("amt"))
        .limit(10)
        .all()
    )
    payments_table = [
        {
            "name": r.name,
            "pib": r.pib or "",
            "amount": _money(r.amt),
            "count": int(r.cnt),
        }
        for r in pay_by_cust
    ]

    promet_saldo = _dec(out_gross) - _dec(in_gross)
    pdv_saldo = _dec(out_vat) - _dec(in_vat)
    netto_after_exp = _dec(out_gross) - _dec(in_gross) - _dec(exp_total)

    return render(
        request,
        "reports.html",
        {
            "user": user,
            "tenant": tenant,
            "year": y,
            "month": m,
            "month_label": _MONTHS[m],
            "prev_label": f"{_MONTHS[pm]} {py}",
            "years": list(range(y - 3, y + 2)),
            "out_count": out_count,
            "out_gross": _dec(out_gross),
            "out_vat": _dec(out_vat),
            "out_net": _dec(out_net),
            "in_count": in_count,
            "in_gross": _dec(in_gross),
            "in_vat": _dec(in_vat),
            "in_net": _dec(in_net),
            "exp_total": _dec(exp_total),
            "exp_summary": exp_summary,
            "exp_chart": exp_chart,
            "promet_saldo": promet_saldo,
            "pdv_saldo": pdv_saldo,
            "netto_after_exp": netto_after_exp,
            "avg_out": _dec(avg_out),
            "max_out": _dec(max_out),
            "min_out": _dec(min_out),
            "active_days": active_days,
            "best_day": best_day,
            "delta_out": _pct_change(out_gross, p_out_gross),
            "delta_in": _pct_change(in_gross, p_in_gross),
            "delta_exp": _pct_change(exp_total, p_exp_total),
            "delta_out_cnt": _pct_change(float(out_count), float(p_out_count)),
            "p_out_gross": _dec(p_out_gross),
            "p_in_gross": _dec(p_in_gross),
            "p_exp_total": _dec(p_exp_total),
            "p_out_count": p_out_count,
            "p_in_count": p_in_count,
            "p_out_vat": _dec(p_out_vat),
            "p_in_vat": _dec(p_in_vat),
            "daily_chart": daily_chart,
            "daily_in_chart": daily_in_chart,
            "weekday_chart": weekday_chart,
            "trend": trend,
            "pay_chart": pay_chart,
            "type_chart": type_chart,
            "status_chart": status_chart,
            "vat_table": vat_table,
            "vat_chart": vat_chart,
            "buyers_table": buyers_table,
            "articles_table": articles_table,
            "biggest_table": biggest_table,
            "suppliers_table": suppliers_table,
            "in_status_chart": in_status_chart,
            "in_source_chart": in_source_chart,
            "cash_initial": cash_initial,
            "cash_withdraw": cash_withdraw,
            "operators_table": operators_table,
            "payments_total": _dec(payments_total),
            "payments_table": payments_table,
        },
    )


@router.get("/izvjestaji/komitenti", response_class=HTMLResponse)
def izvjestaji_komitenti_lista(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        user, tenant = _auth(request, db)
    except AuthRequired:
        return redirect("/login")

    y, m, start, end = _period(year, month)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    term = (q or "").strip()[:64]

    cust_q = db.query(Customer).filter(Customer.tenant_id == tenant.id, Customer.active.is_(True))
    if term:
        like = f"%{term}%"
        cust_q = cust_q.filter(
            (Customer.name.ilike(like))
            | (Customer.pib.ilike(like))
            | (Customer.city.ilike(like))
        )
    customers = cust_q.order_by(Customer.name).limit(200).all()

    # Period promet po PIB-u
    period_rows = (
        db.query(
            Invoice.buyer_pib,
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*_out_filters(tenant.id, start_dt, end_dt))
        .group_by(Invoice.buyer_pib)
        .all()
    )
    period_by_pib = {r.buyer_pib: (_money(r.gross), int(r.cnt)) for r in period_rows}

    rows = []
    for c in customers:
        gross, cnt = period_by_pib.get(c.pib, (0.0, 0))
        summary = customer_summary(db, tenant, c)
        rows.append(
            {
                "id": c.id,
                "name": c.name,
                "pib": c.pib,
                "city": c.city or "",
                "email": c.email or "",
                "period_gross": gross,
                "period_count": cnt,
                "dug": summary["dug"],
                "fakturisano": summary["fakturisano"],
                "uplaceno": summary["uplaceno"],
            }
        )
    # Komitenti sa prometom u periodu ali bez šifarnika — ne prikazujemo ovdje
    rows.sort(key=lambda r: (-r["period_gross"], r["name"].lower()))

    return render(
        request,
        "reports_customers.html",
        {
            "user": user,
            "tenant": tenant,
            "year": y,
            "month": m,
            "month_label": _MONTHS[m],
            "years": list(range(y - 3, y + 2)),
            "q": term,
            "rows": rows,
        },
    )


@router.get("/izvjestaji/komitenti/{customer_id}", response_class=HTMLResponse)
def izvjestaji_komitent(
    request: Request,
    customer_id: int,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
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
        return redirect("/izvjestaji/komitenti")

    y, m, start, end = _period(year, month)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

    out_f = (
        Invoice.tenant_id == tenant.id,
        Invoice.buyer_pib == customer.pib,
        Invoice.status == InvoiceStatus.fiscalized.value,
        Invoice.is_template.is_(False),
        Invoice.fiscalized_at >= start_dt,
        Invoice.fiscalized_at < end_dt,
    )

    inv_count = db.query(func.count(Invoice.id)).filter(*out_f).scalar() or 0
    inv_gross = _money(
        db.query(func.coalesce(func.sum(Invoice.total_gross), 0)).filter(*out_f).scalar()
    )
    inv_vat = _money(
        db.query(func.coalesce(func.sum(Invoice.total_vat), 0)).filter(*out_f).scalar()
    )
    inv_net = _money(
        db.query(func.coalesce(func.sum(Invoice.total_net), 0)).filter(*out_f).scalar()
    )

    pay_start, pay_end = start.isoformat(), end.isoformat()
    paid_period = _money(
        db.query(func.coalesce(func.sum(CustomerPayment.amount), 0))
        .filter(
            CustomerPayment.tenant_id == tenant.id,
            CustomerPayment.customer_id == customer.id,
            CustomerPayment.paid_at >= pay_start,
            CustomerPayment.paid_at < pay_end,
        )
        .scalar()
    )

    # Dnevni promet
    day_rows = (
        db.query(
            cast(Invoice.fiscalized_at, Date).label("day"),
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*out_f)
        .group_by(cast(Invoice.fiscalized_at, Date))
        .all()
    )
    by_day = {r.day: (_money(r.gross), int(r.cnt)) for r in day_rows if r.day}
    days_in_month = monthrange(y, m)[1]
    daily = []
    for day_n in range(1, days_in_month + 1):
        day = date(y, m, day_n)
        g, c = by_day.get(day, (0.0, 0))
        daily.append({"label": f"{day_n}", "value": g, "count": c})
    daily_chart = _chart_bars(daily)

    # 12-mjesečni trend za ovog komitenta
    trend = []
    ty, tm = y, m
    for _ in range(12):
        ts, te = _period(ty, tm)[2:]
        ts_dt = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
        te_dt = datetime(te.year, te.month, te.day, tzinfo=timezone.utc)
        g = _money(
            db.query(func.coalesce(func.sum(Invoice.total_gross), 0))
            .filter(
                Invoice.tenant_id == tenant.id,
                Invoice.buyer_pib == customer.pib,
                Invoice.status == InvoiceStatus.fiscalized.value,
                Invoice.is_template.is_(False),
                Invoice.fiscalized_at >= ts_dt,
                Invoice.fiscalized_at < te_dt,
            )
            .scalar()
        )
        trend.append({"label": f"{_MONTHS[tm][:3]} {ty}", "value": g})
        ty, tm, _, _ = _prev_month(ty, tm)
    trend.reverse()
    trend_chart = _chart_bars(trend)

    pay_rows = (
        db.query(
            Invoice.payment_method,
            func.coalesce(func.sum(Invoice.total_gross), 0).label("gross"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(*out_f)
        .group_by(Invoice.payment_method)
        .order_by(desc("gross"))
        .all()
    )
    pay_chart = _chart_bars(
        [
            {
                "label": _PAY_LABELS.get(r.payment_method or "", r.payment_method or "—"),
                "value": _money(r.gross),
                "count": int(r.cnt),
            }
            for r in pay_rows
        ]
    )

    art_rows = (
        db.query(
            InvoiceLine.name,
            InvoiceLine.code,
            func.coalesce(func.sum(InvoiceLine.total_gross), 0).label("gross"),
            func.coalesce(func.sum(InvoiceLine.quantity), 0).label("qty"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(*out_f)
        .group_by(InvoiceLine.name, InvoiceLine.code)
        .order_by(desc("gross"))
        .limit(20)
        .all()
    )
    articles = [
        {
            "name": r.name,
            "code": r.code or "",
            "gross": _money(r.gross),
            "qty": float(r.qty or 0),
        }
        for r in art_rows
    ]

    invoices = (
        db.query(Invoice)
        .filter(*out_f)
        .order_by(Invoice.fiscalized_at.desc())
        .limit(50)
        .all()
    )
    invoices_table = [
        {
            "id": inv.id,
            "num": inv.inv_num or inv.external_id,
            "gross": _money(inv.total_gross),
            "vat": _money(inv.total_vat),
            "pay": _PAY_LABELS.get(inv.payment_method or "", inv.payment_method or "—"),
            "when": inv.fiscalized_at.strftime("%d.%m.%Y") if inv.fiscalized_at else "",
        }
        for inv in invoices
    ]

    payments = (
        db.query(CustomerPayment)
        .filter(
            CustomerPayment.tenant_id == tenant.id,
            CustomerPayment.customer_id == customer.id,
            CustomerPayment.paid_at >= pay_start,
            CustomerPayment.paid_at < pay_end,
        )
        .order_by(CustomerPayment.paid_at.desc())
        .limit(50)
        .all()
    )
    payments_table = [
        {
            "id": p.id,
            "amount": _money(p.amount),
            "when": p.paid_at or "",
            "note": p.note or "",
            "invoice_id": p.invoice_id,
        }
        for p in payments
    ]

    summary = customer_summary(db, tenant, customer)
    ledger = customer_ledger(db, tenant, customer)
    # Ledger stavke u periodu (za pregled)
    ledger_period = [
        r
        for r in ledger
        if r.get("Datum") and start.isoformat() <= r["Datum"] < end.isoformat()
    ]

    avg = (inv_gross / inv_count) if inv_count else 0.0

    return render(
        request,
        "reports_customer.html",
        {
            "user": user,
            "tenant": tenant,
            "customer": customer,
            "year": y,
            "month": m,
            "month_label": _MONTHS[m],
            "years": list(range(y - 3, y + 2)),
            "inv_count": inv_count,
            "inv_gross": _dec(inv_gross),
            "inv_vat": _dec(inv_vat),
            "inv_net": _dec(inv_net),
            "paid_period": _dec(paid_period),
            "avg_inv": _dec(avg),
            "summary": summary,
            "daily_chart": daily_chart,
            "trend_chart": trend_chart,
            "pay_chart": pay_chart,
            "articles": articles,
            "invoices_table": invoices_table,
            "payments_table": payments_table,
            "ledger_period": ledger_period,
        },
    )


@router.get("/izvjestaji/pregled", response_class=HTMLResponse)
def izvjestaji_pregled(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    try:
        _auth(request, db)
    except AuthRequired:
        return redirect("/login")
    q = []
    if year:
        q.append(f"year={int(year)}")
    if month:
        q.append(f"month={int(month)}")
    suffix = ("?" + "&".join(q)) if q else ""
    return redirect(f"/izvjestaji{suffix}")
