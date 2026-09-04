"""ESC/POS bajtovi za termalne štampače (80mm).

Web preglednik ne vidi USB/Serial. Lokalni agent (`sepko.print_agent`)
šalje ove bajtove na štampač; fallback je window.print() + CSS @media print.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

ESC = b"\x1b"
GS = b"\x1d"


def _txt(value: str) -> bytes:
    return (value or "").encode("cp1250", errors="replace")


def _line(left: str, right: str, width: int = 42) -> str:
    left = left or ""
    right = right or ""
    gap = width - len(left) - len(right)
    if gap < 1:
        return (left + " " + right)[:width]
    return left + (" " * gap) + right


@dataclass
class EscPosOptions:
    blank_start: int = 0
    blank_end: int = 2
    paper_cut: str = "partial"  # none | partial | full
    open_drawer: str = "never"  # never | cash | always
    is_cash: bool = False
    width: int = 42


def build_receipt(
    *,
    seller_name: str,
    seller_pib: str,
    inv_num: str,
    issue_dt: str,
    buyer_name: str | None,
    lines: list[dict[str, Any]],
    total_gross: Decimal,
    pay_label: str,
    ikof: str | None,
    jikr: str | None,
    qr_url: str | None,
    options: EscPosOptions | None = None,
) -> bytes:
    opt = options or EscPosOptions()
    out = bytearray()
    out += ESC + b"@"  # init
    out += ESC + b"a" + b"\x01"  # center
    out += ESC + b"E" + b"\x01"  # bold
    out += _txt(seller_name) + b"\n"
    out += ESC + b"E" + b"\x00"
    out += _txt(f"PIB: {seller_pib}") + b"\n"
    out += _txt("FISKALNI RACUN") + b"\n"
    out += ESC + b"a" + b"\x00"  # left
    out += b"-" * opt.width + b"\n"
    for _ in range(max(0, opt.blank_start)):
        out += b"\n"
    out += _txt(f"Broj: {inv_num}") + b"\n"
    if issue_dt:
        out += _txt(f"Vrijeme: {issue_dt}") + b"\n"
    if buyer_name:
        out += _txt(f"Kupac: {buyer_name}") + b"\n"
    out += b"-" * opt.width + b"\n"
    for row in lines:
        name = str(row.get("name") or "")
        qty = str(row.get("qty") or "")
        gross = str(row.get("gross") or "")
        out += _txt(name[: opt.width]) + b"\n"
        out += _txt(_line(f"  {qty} x", gross, opt.width)) + b"\n"
    out += b"-" * opt.width + b"\n"
    out += ESC + b"E" + b"\x01"
    out += _txt(_line("UKUPNO", f"{total_gross:.2f} EUR", opt.width)) + b"\n"
    out += ESC + b"E" + b"\x00"
    out += _txt(_line(pay_label, f"{total_gross:.2f} EUR", opt.width)) + b"\n"
    if ikof:
        out += _txt(f"IKOF: {ikof}") + b"\n"
    if jikr:
        out += _txt(f"JIKR: {jikr}") + b"\n"
    if qr_url:
        out += _qr(qr_url)
    for _ in range(max(0, opt.blank_end)):
        out += b"\n"
    if opt.open_drawer == "always" or (opt.open_drawer == "cash" and opt.is_cash):
        out += ESC + b"p" + b"\x00" + b"\x19" + b"\xfa"  # drawer pin 2
    if opt.paper_cut == "full":
        out += GS + b"V" + b"\x00"
    elif opt.paper_cut == "partial":
        out += GS + b"V" + b"\x01"
    return bytes(out)


def _qr(data: str) -> bytes:
    payload = data.encode("utf-8")
    # GS ( k — QR model 2, store + print (Epson)
    store_len = len(payload) + 3
    p_l = store_len % 256
    p_h = store_len // 256
    out = bytearray()
    out += GS + b"(k" + bytes([4, 0, 49, 65, 50, 0])  # model 2
    out += GS + b"(k" + bytes([3, 0, 49, 67, 4])  # size 4
    out += GS + b"(k" + bytes([3, 0, 49, 69, 48])  # error L
    out += GS + b"(k" + bytes([p_l, p_h, 49, 80, 48]) + payload
    out += GS + b"(k" + bytes([3, 0, 49, 81, 48])  # print
    out += b"\n"
    return bytes(out)
