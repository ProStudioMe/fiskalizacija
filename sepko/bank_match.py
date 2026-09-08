"""Uparivanje bankovnih stavki sa komitentima (port logike iz Finasije).

Redoslijed: zapamćeni ključ → PIB u opisu → broj fakture → fuzzy naziv.
"""
from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session

from sepko.efi import display_inv_num, parse_display_inv_num
from sepko.models import BankTransaction, Customer, CustomerPayment, Invoice, InvoiceStatus, Tenant

CONFIDENT = 88
REVIEW_MIN = 70

ACCOUNT_RE = re.compile(r"\b(\d{18}|9\d{2}-\d{5}-\d{2})\b")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[\"'`]", "", text)
    text = re.sub(r"\b(d\.?o\.?o\.?|doo|do\.o\.)\b", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def counterparty_key(description: str | None) -> str:
    desc = (description or "").strip()
    if not desc:
        return ""
    head = desc.split("|", 1)[0].strip()
    key = _fold(head)
    parts = key.split()
    if len(parts) > 6:
        key = " ".join(parts[:6])
    return key


def account_from_text(text: str | None) -> str:
    m = ACCOUNT_RE.search(text or "")
    return m.group(1) if m else ""


def extract_pibs(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\b(\d{8,12})\b", text or "")))


def normalize_invoice_ref(ref: str) -> str:
    s = (ref or "").strip().upper()
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    m = re.match(r"^(\d-\d-\d+)/(\d{2,4})$", s)
    if m:
        num, year = m.group(1), m.group(2)
        if len(year) == 2:
            year = f"20{year}"
        return f"{num}/{year}"
    m = re.match(r"^(\d-\d-\d+)$", s)
    if m:
        return m.group(1)
    return s


def extract_invoice_refs(text: str) -> list[str]:
    text = text or ""
    raw: list[str] = []
    raw += re.findall(r"\b\d\s*-\s*\d\s*-\s*\d+\s*/\s*\d{2,4}\b", text)
    for a, y in re.findall(r"\b(\d\s*-\s*\d\s*-\s*\d+)\s+(\d{4})\b", text):
        raw.append(f"{a}/{y}")
    for a, y in re.findall(r"\b(\d\s*-\s*\d\s*-\s*\d+)\s*[-_]\s*(\d{4})(?:_\d+-\d+)?", text):
        raw.append(f"{a}/{y}")
    for a, n, y in re.findall(r"\b(\d)\s*-\s+-?\s*(\d+)\s*/\s*(\d{2,4})\b", text):
        raw.append(f"{a}-1-{n}/{y}")
    for n in re.findall(
        r"(?:rn|ra[cčć]un|rachun|faktura|invoice|br\.?)\s*[:=]?\s*(\d\s*-\s*\d\s*-\s*\d+)\b",
        text,
        flags=re.IGNORECASE,
    ):
        raw.append(n)
    out: list[str] = []
    for r in raw:
        n = normalize_invoice_ref(r)
        if n and n not in out:
            out.append(n)
    return out


def _name_score(a: str, b: str) -> float:
    fa, fb = _fold(a), _fold(b)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 100.0
    if fa in fb or fb in fa:
        return 92.0
    ratio = SequenceMatcher(None, fa, fb).ratio() * 100
    # token overlap
    ta, tb = set(fa.split()), set(fb.split())
    if ta and tb:
        inter = len(ta & tb) / max(len(ta | tb), 1) * 100
        ratio = max(ratio, inter)
    return round(ratio, 1)


def _learned(tenant: Tenant) -> dict[str, Any]:
    raw = {}
    try:
        data = json.loads(tenant.settings_json or "{}")
        raw = data.get("finansije", {}).get("learned_matches") or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def remember_match(tenant: Tenant, *, key: str, customer_id: int, pib: str) -> None:
    if not key:
        return
    data = {}
    try:
        data = json.loads(tenant.settings_json or "{}")
    except Exception:
        data = {}
    fin = data.setdefault("finansije", {})
    learned = fin.setdefault("learned_matches", {})
    learned[key] = {"customer_id": customer_id, "pib": pib}
    tenant.settings_json = json.dumps(data, ensure_ascii=False)


def _invoice_maps(db: Session, tenant: Tenant) -> tuple[dict[str, list[tuple[int, str]]], dict[str, list[tuple[int, str]]]]:
    """full_ref → [(customer_id, broj)], prefix → …"""
    by_full: dict[str, list[tuple[int, str]]] = {}
    by_prefix: dict[str, list[tuple[int, str]]] = {}
    customers = {
        c.pib: c.id
        for c in db.query(Customer).filter(Customer.tenant_id == tenant.id, Customer.active.is_(True)).all()
    }
    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.fiscalized.value,
            Invoice.is_template.is_(False),
            Invoice.buyer_pib.isnot(None),
        )
        .all()
    )
    for inv in invoices:
        cid = customers.get(inv.buyer_pib or "")
        if not cid:
            continue
        broj = display_inv_num(inv)
        full = normalize_invoice_ref(broj)
        if not full or full == "-":
            continue
        by_full.setdefault(full, []).append((cid, broj))
        by_prefix.setdefault(full.split("/", 1)[0], []).append((cid, broj))
        # also inv_ord_num/year form
        if inv.inv_ord_num and inv.issue_datetime:
            alt = f"1-1-{inv.inv_ord_num}/{inv.issue_datetime.year}"
            by_full.setdefault(normalize_invoice_ref(alt), []).append((cid, broj))
    return by_full, by_prefix


def propose_customer_match(
    db: Session,
    tenant: Tenant,
    *,
    description: str,
    amount: Decimal | float,
) -> dict[str, Any]:
    """Vrati predlog komitenta za uplatu (amount > 0)."""
    amt = float(amount or 0)
    desc = description or ""
    blob = desc

    customers = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant.id, Customer.active.is_(True))
        .all()
    )
    by_pib = {c.pib: c for c in customers}
    by_id = {c.id: c for c in customers}
    learned = _learned(tenant)

    # 1) Learned account / name
    acc = account_from_text(blob)
    for key in (f"acc:{acc}" if acc else "", counterparty_key(blob)):
        if not key:
            continue
        hit = learned.get(key)
        if hit and hit.get("customer_id") in by_id:
            c = by_id[int(hit["customer_id"])]
            return {
                "status": "suggested",
                "auto": True,
                "score": 100,
                "customer_id": c.id,
                "pib": c.pib,
                "name": c.name,
                "reason": f"Zapamćeno ({key})",
                "candidates": [{"customer_id": c.id, "pib": c.pib, "name": c.name, "score": 100}],
            }

    if amt <= 0:
        return {"status": "needs_review", "auto": False, "score": 0, "customer_id": None, "candidates": [], "reason": "Odliv — nije uplata komitenta"}

    candidates: list[dict[str, Any]] = []

    # 2) PIB
    for pib in extract_pibs(blob):
        c = by_pib.get(pib)
        if c:
            candidates.append(
                {
                    "customer_id": c.id,
                    "pib": c.pib,
                    "name": c.name,
                    "score": 100.0,
                    "reason": f"PIB {pib}",
                }
            )

    # 3) Invoice refs
    by_full, by_prefix = _invoice_maps(db, tenant)
    for ref in extract_invoice_refs(blob):
        n = normalize_invoice_ref(ref)
        matches = by_full.get(n) if "/" in n else []
        exact = bool(matches)
        if not matches:
            matches = by_prefix.get(n.split("/", 1)[0] if "/" in n else n, [])
        uniq: dict[int, str] = {}
        for cid, broj in matches:
            uniq.setdefault(cid, broj)
        if len(uniq) == 1:
            cid, broj = next(iter(uniq.items()))
            c = by_id.get(cid)
            if c:
                candidates.append(
                    {
                        "customer_id": c.id,
                        "pib": c.pib,
                        "name": c.name,
                        "score": 100.0 if exact else 92.0,
                        "reason": f"Faktura {broj}",
                        "invoice_ref": broj,
                    }
                )
        elif len(uniq) > 1:
            for cid, broj in uniq.items():
                c = by_id.get(cid)
                if c:
                    candidates.append(
                        {
                            "customer_id": c.id,
                            "pib": c.pib,
                            "name": c.name,
                            "score": 92.0,
                            "reason": f"Faktura {broj} (više pogodaka)",
                            "invoice_ref": broj,
                        }
                    )

    # 4) Fuzzy name
    for c in customers:
        sc = _name_score(c.name, blob.split("|", 1)[0])
        if sc >= REVIEW_MIN:
            candidates.append(
                {
                    "customer_id": c.id,
                    "pib": c.pib,
                    "name": c.name,
                    "score": sc,
                    "reason": f"Naziv ~{sc:.0f}%",
                }
            )

    # Dedupe by customer_id keep best
    best: dict[int, dict[str, Any]] = {}
    for cand in candidates:
        cid = int(cand["customer_id"])
        prev = best.get(cid)
        if not prev or cand["score"] > prev["score"]:
            best[cid] = cand
    ranked = sorted(best.values(), key=lambda x: -x["score"])

    if not ranked:
        return {
            "status": "needs_review",
            "auto": False,
            "score": 0,
            "customer_id": None,
            "candidates": [],
            "reason": "Nema pogodaka — odaberi komitenta ručno",
        }

    top = ranked[0]
    second = ranked[1]["score"] if len(ranked) > 1 else 0
    if top["score"] >= CONFIDENT and (top["score"] - second) >= 8:
        auto = top["score"] >= 100 or str(top.get("reason", "")).startswith("PIB") or str(
            top.get("reason", "")
        ).startswith("Zapamćeno") or (
            str(top.get("reason", "")).startswith("Faktura") and top["score"] >= 100
        )
        return {
            "status": "suggested",
            "auto": auto and top["score"] >= 100,
            "score": top["score"],
            "customer_id": top["customer_id"],
            "pib": top["pib"],
            "name": top["name"],
            "reason": top.get("reason") or "",
            "candidates": ranked[:5],
            "invoice_ref": top.get("invoice_ref"),
        }

    return {
        "status": "needs_review",
        "auto": False,
        "score": top["score"],
        "customer_id": top["customer_id"],
        "pib": top["pib"],
        "name": top["name"],
        "reason": top.get("reason") or "Više sličnih — potvrdi",
        "candidates": ranked[:5],
    }


def apply_customer_payment(
    db: Session,
    tenant: Tenant,
    tx: BankTransaction,
    customer: Customer,
    *,
    invoice_id: int | None = None,
    remember: bool = True,
) -> CustomerPayment:
    """Poveži uplatu (credit) sa komitentom → CustomerPayment + matched."""
    amt = abs(Decimal(str(tx.amount or 0)))
    if amt <= 0:
        raise ValueError("Iznos uplate mora biti > 0")

    # Prefer invoice from description if not given
    if invoice_id is None:
        proposal = propose_customer_match(db, tenant, description=tx.description or "", amount=amt)
        ref = proposal.get("invoice_ref")
        if ref:
            parsed = parse_display_inv_num(ref)
            if parsed:
                ord_num, year = parsed
                inv = (
                    db.query(Invoice)
                    .filter(
                        Invoice.tenant_id == tenant.id,
                        Invoice.buyer_pib == customer.pib,
                        Invoice.inv_ord_num == ord_num,
                        Invoice.status == InvoiceStatus.fiscalized.value,
                    )
                    .all()
                )
                for candidate in inv:
                    if candidate.issue_datetime and candidate.issue_datetime.year == year:
                        invoice_id = candidate.id
                        break

    pay = CustomerPayment(
        tenant_id=tenant.id,
        customer_id=customer.id,
        invoice_id=invoice_id,
        bank_tx_id=tx.id,
        amount=amt,
        paid_at=(tx.tx_date or "")[:10] or None,
        note=(tx.description or "")[:500] or None,
    )
    db.add(pay)
    tx.customer_id = customer.id
    tx.status = "matched"

    if remember:
        ck = counterparty_key(tx.description)
        if ck:
            remember_match(tenant, key=ck, customer_id=customer.id, pib=customer.pib)
        acc = account_from_text(tx.description or "")
        if acc:
            remember_match(tenant, key=f"acc:{acc}", customer_id=customer.id, pib=customer.pib)

    from sepko.audit import write_audit

    write_audit(
        db,
        "bank_tx.match_customer",
        tenant_id=tenant.id,
        entity_type="bank_tx",
        entity_id=tx.id,
        detail=f"customer #{customer.id} {customer.pib}",
    )
    return pay


def auto_match_new_transactions(db: Session, tenant: Tenant, txs: list[BankTransaction]) -> int:
    """Za nove credit stavke — auto-poveži ako je score 100."""
    done = 0
    for tx in txs:
        if tx.status != "needs_review":
            continue
        amt = float(tx.amount or 0)
        if amt <= 0:
            continue
        proposal = propose_customer_match(
            db, tenant, description=tx.description or tx.raw_text or "", amount=amt
        )
        if not proposal.get("auto") or not proposal.get("customer_id"):
            continue
        customer = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant.id, Customer.id == int(proposal["customer_id"]))
            .first()
        )
        if not customer:
            continue
        apply_customer_payment(db, tenant, tx, customer, remember=False)
        done += 1
    return done
