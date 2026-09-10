"""Izvoz liste faktura (Excel / PDF / štampa HTML)."""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def _amt(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _esc(s: Any) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _amt_txt(v: Any) -> str:
    n = _amt(v)
    if n is None:
        return "—"
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _find_pdf_font(font_path: str = "") -> tuple[str, str] | None:
    candidates: list[tuple[str, str]] = []
    explicit = (font_path or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return (str(p), str(p))
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates.extend(
        [
            (str(windir / "Fonts" / "arial.ttf"), str(windir / "Fonts" / "arialbd.ttf")),
            (str(windir / "Fonts" / "calibri.ttf"), str(windir / "Fonts" / "calibrib.ttf")),
        ]
    )
    candidates.extend(
        [
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ),
        ]
    )
    for regular, bold in candidates:
        if Path(regular).is_file():
            bold_path = bold if Path(bold).is_file() else regular
            return regular, bold_path
    return None


# Kolone izvoza (ključ u row/headers, širina Excel, širina PDF mm)
_EXPORT_COLS: tuple[tuple[str, int, float, bool], ...] = (
    ("number", 16, 28, False),
    ("status", 13, 24, False),
    ("doc", 18, 32, False),
    ("client", 28, 48, False),
    ("date", 16, 28, False),
    ("net", 12, 26, True),
    ("vat", 12, 24, True),
    ("amount", 12, 26, True),
    ("pay", 14, 26, False),
)


def build_invoices_xlsx(
    *,
    rows: list[dict[str, Any]],
    tenant_name: str,
    tenant_pib: str,
    headers: dict[str, str],
    brand: str = "ProRačun",
) -> bytes:
    """rows: number, status, doc, client, date, net, vat, amount, pay."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Fakture"

    bold = Font(bold=True, size=12)
    hdr = Font(bold=True, size=10)
    fill = PatternFill("solid", fgColor="F0F0F0")
    thin = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    right = Alignment(horizontal="right")

    ws["A1"] = tenant_name
    ws["A1"].font = bold
    ws["A2"] = f"PIB {tenant_pib} · {brand}"
    ws["A3"] = headers.get("subtitle", "")

    start = 5
    money_cols: list[int] = []
    for i, (key, width, _pdf_w, is_money) in enumerate(_EXPORT_COLS, 1):
        cell = ws.cell(start, i, headers.get(key, key))
        cell.font = hdr
        cell.fill = fill
        cell.border = thin
        if is_money:
            cell.alignment = right
            money_cols.append(i)
        ws.column_dimensions[get_column_letter(i)].width = width

    sum_net = sum_vat = sum_gross = 0.0
    for r_i, row in enumerate(rows, start + 1):
        vals: list[Any] = []
        for key, _w, _pw, is_money in _EXPORT_COLS:
            if is_money:
                vals.append(_amt(row.get(key)))
            else:
                vals.append(row.get(key) or "")
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r_i, c, v if v is not None else "")
            cell.border = thin
            if c in money_cols and isinstance(v, (int, float)):
                cell.number_format = "#,##0.00"
                cell.alignment = right
        n, v, g = _amt(row.get("net")), _amt(row.get("vat")), _amt(row.get("amount"))
        if n is not None:
            sum_net += n
        if v is not None:
            sum_vat += v
        if g is not None:
            sum_gross += g

    sum_row = start + 1 + len(rows)
    # label u koloni datuma (5), iznosi u net/vat/amount (6/7/8)
    label_cell = ws.cell(sum_row, 5, f"{headers.get('total', 'Ukupno')} ({len(rows)})")
    label_cell.font = Font(bold=True)
    for col_i, total in ((6, sum_net), (7, sum_vat), (8, sum_gross)):
        cell = ws.cell(sum_row, col_i, round(total, 2))
        cell.font = Font(bold=True)
        cell.number_format = "#,##0.00"
        cell.alignment = right

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_invoices_pdf(
    *,
    rows: list[dict[str, Any]],
    tenant_name: str,
    tenant_pib: str,
    headers: dict[str, str],
    brand: str = "ProRačun",
    font_path: str = "",
) -> bytes | None:
    """Pravi A4 landscape PDF (fpdf2). None ako nema fonta / fpdf2."""
    fonts = _find_pdf_font(font_path)
    if fonts is None:
        return None
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    try:
        regular, bold = fonts
        pdf = FPDF(orientation="L", format="A4", unit="mm")
        pdf.set_auto_page_break(auto=True, margin=12)
        pdf.add_page()
        pdf.add_font("Inv", "", regular)
        pdf.add_font("Inv", "B", bold)
        nxt = {"new_x": "LMARGIN", "new_y": "NEXT"}

        pdf.set_font("Inv", "B", 13)
        pdf.cell(0, 7, str(tenant_name or ""), **nxt)
        pdf.set_font("Inv", "", 9)
        pdf.set_text_color(90, 90, 90)
        sub = f"PIB {tenant_pib} · {brand}"
        if headers.get("subtitle"):
            sub = f"{sub} · {headers['subtitle']}"
        pdf.cell(0, 5, sub, **nxt)
        pdf.set_text_color(17, 17, 17)
        pdf.ln(2)

        widths = tuple(c[2] for c in _EXPORT_COLS)
        keys = tuple(c[0] for c in _EXPORT_COLS)
        money_idx = {i for i, c in enumerate(_EXPORT_COLS) if c[3]}

        pdf.set_font("Inv", "B", 7)
        pdf.set_fill_color(240, 240, 240)
        for w, key in zip(widths, keys):
            pdf.cell(w, 6, str(headers.get(key, key))[:36], border=1, fill=True)
        pdf.ln()

        pdf.set_font("Inv", "", 7)
        sum_net = sum_vat = sum_gross = 0.0
        for row in rows:
            n, v, g = _amt(row.get("net")), _amt(row.get("vat")), _amt(row.get("amount"))
            if n is not None:
                sum_net += n
            if v is not None:
                sum_vat += v
            if g is not None:
                sum_gross += g
            vals = []
            for i, key in enumerate(keys):
                if i in money_idx:
                    val = _amt(row.get(key))
                    vals.append(f"{_amt_txt(val)} €" if val is not None else "—")
                else:
                    # skraćenja po širini
                    maxlen = 22 if key != "client" else 36
                    vals.append(str(row.get(key) or "")[:maxlen])
            for i, (w, val) in enumerate(zip(widths, vals)):
                pdf.cell(w, 5.5, val, border=1, align="R" if i in money_idx else "L")
            pdf.ln()

        pdf.set_font("Inv", "B", 8)
        # label kroz prvih 5 kolona
        pdf.cell(sum(widths[:5]), 6, f"{headers.get('total', 'Ukupno')} ({len(rows)})", border=1)
        pdf.cell(widths[5], 6, f"{_amt_txt(sum_net)} €", border=1, align="R")
        pdf.cell(widths[6], 6, f"{_amt_txt(sum_vat)} €", border=1, align="R")
        pdf.cell(widths[7], 6, f"{_amt_txt(sum_gross)} €", border=1, align="R")
        pdf.cell(widths[8], 6, "", border=1)
        return bytes(pdf.output())
    except Exception:
        return None


def build_invoices_pdf_html(
    *,
    rows: list[dict[str, Any]],
    tenant_name: str,
    tenant_pib: str,
    headers: dict[str, str],
    brand: str = "ProRačun",
    auto_print: bool = False,
) -> str:
    """HTML za pregled / štampu — bez auto print dijaloga (osim ako se eksplicitno traži)."""
    body_rows = []
    sum_net = sum_vat = sum_gross = 0.0
    for row in rows:
        n, v, g = _amt(row.get("net")), _amt(row.get("vat")), _amt(row.get("amount"))
        if n is not None:
            sum_net += n
        if v is not None:
            sum_vat += v
        if g is not None:
            sum_gross += g
        body_rows.append(
            "<tr>"
            f"<td>{_esc(row.get('number'))}</td>"
            f"<td>{_esc(row.get('status'))}</td>"
            f"<td>{_esc(row.get('doc'))}</td>"
            f"<td>{_esc(row.get('client'))}</td>"
            f"<td>{_esc(row.get('date'))}</td>"
            f"<td class='num'>{_esc(_amt_txt(n))} €</td>"
            f"<td class='num'>{_esc(_amt_txt(v))} €</td>"
            f"<td class='num'>{_esc(_amt_txt(g))} €</td>"
            f"<td>{_esc(row.get('pay'))}</td>"
            "</tr>"
        )
    onload = ' onload="window.print()"' if auto_print else ""
    print_lbl = _esc(headers.get("print", "Štampaj"))
    close_lbl = _esc(headers.get("close", "Zatvori"))
    subtitle = _esc(headers.get("subtitle", ""))
    return f"""<!DOCTYPE html>
<html lang="sr-ME">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(headers.get("title", "Fakture"))}</title>
<style>
  body {{ font-family: system-ui, Segoe UI, sans-serif; color: #111; margin: 1.5rem; font-size: 13px; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 0.25rem; }}
  .meta {{ color: #555; margin-bottom: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ border-bottom: 1px solid #ddd; padding: 0.4rem 0.5rem; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f4; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  tfoot td {{ font-weight: 700; border-top: 2px solid #222; }}
  .toolbar {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 12px; }}
  .toolbar button {{ padding: 8px 14px; cursor: pointer; font: inherit; border-radius: 8px; border: 1px solid #ccc; background: #fff; }}
  .toolbar button.primary {{ background: #4f46e5; color: #fff; border-color: #4f46e5; }}
  @media print {{ .noprint {{ display: none !important; }} body {{ margin: 0; }} }}
</style>
</head>
<body{onload}>
<div class="toolbar noprint">
  <button type="button" class="primary" onclick="window.print()">{print_lbl}</button>
  <button type="button" onclick="window.close()">{close_lbl}</button>
</div>
<h1>{_esc(tenant_name)}</h1>
<div class="meta">PIB {_esc(tenant_pib)} · {_esc(brand)}{" · " + subtitle if subtitle else ""}</div>
<table>
<thead>
<tr>
  <th>{_esc(headers.get("number", "Broj"))}</th>
  <th>{_esc(headers.get("status", "Status"))}</th>
  <th>{_esc(headers.get("doc", "Tip"))}</th>
  <th>{_esc(headers.get("client", "Klijent"))}</th>
  <th>{_esc(headers.get("date", "Datum"))}</th>
  <th class="num">{_esc(headers.get("net", "Bez PDV"))}</th>
  <th class="num">{_esc(headers.get("vat", "PDV"))}</th>
  <th class="num">{_esc(headers.get("amount", "Ukupno"))}</th>
  <th>{_esc(headers.get("pay", "Plaćanje"))}</th>
</tr>
</thead>
<tbody>
{"".join(body_rows) if body_rows else f'<tr><td colspan="9">{_esc(headers.get("empty", "Nema faktura."))}</td></tr>'}
</tbody>
<tfoot>
<tr>
  <td colspan="5">{_esc(headers.get("total", "Ukupno"))} ({len(rows)})</td>
  <td class="num">{_esc(_amt_txt(sum_net))} €</td>
  <td class="num">{_esc(_amt_txt(sum_vat))} €</td>
  <td class="num">{_esc(_amt_txt(sum_gross))} €</td>
  <td></td>
</tr>
</tfoot>
</table>
</body>
</html>
"""
