"""Zvanični RegisterInvoiceRequest + SOAP koverta (Poreska / CIS)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import uuid

from lxml import etree

from sepko.efi import build_same_taxes, exempt_from_vat_code, load_tenant_company
from sepko.models import Tenant
from sepko.pu.iic import _PODGORICA, pu_datetime
from sepko.pu.xmldsig import DSIG_NS
from sepko.schemas import FiscalizeRequest

EFI_NS = "https://efi.tax.gov.me/fs/schema"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"


def _q(value: Decimal | int | str, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    d = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return f"{d:.{places}f}"


def _qty(value: Decimal | int | str) -> str:
    d = Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = f"{d:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def build_register_invoice_request(
    tenant: Tenant,
    request: FiscalizeRequest,
    *,
    inv_num: str,
    inv_ord_num: int,
    fiscal,
    iic: str,
    iic_signature: str,
    request_uuid: str | None = None,
    send_datetime: datetime | None = None,
) -> etree._Element:
    company = load_tenant_company(tenant)
    send_dt = send_datetime or datetime.now(tz=_PODGORICA)
    req_id = request_uuid or str(uuid.uuid4())

    nsmap = {None: EFI_NS, "ns2": DSIG_NS}
    root = etree.Element(
        f"{{{EFI_NS}}}RegisterInvoiceRequest",
        nsmap=nsmap,
        Id="Request",
        Version="1",
    )
    etree.SubElement(
        root,
        f"{{{EFI_NS}}}Header",
        SendDateTime=pu_datetime(send_dt),
        UUID=req_id,
    )
    inv_attrs = {
        "BusinUnitCode": fiscal.busin_unit_code,
        "IssueDateTime": pu_datetime(request.issue_datetime),
        "InvNum": inv_num,
        "InvOrdNum": str(inv_ord_num),
        "InvType": request.inv_type,
        "TypeOfInv": request.invoice_type,
        "IsIssuerInVAT": "true" if fiscal.is_issuer_in_vat else "false",
        "IsReverseCharge": "false",
        "OperatorCode": fiscal.operator_code,
        "SoftCode": fiscal.soft_code,
        "TCRCode": fiscal.tcr_code,
        "TotPriceWoVAT": _q(request.totals.net, 2),
        "TotVATAmt": _q(request.totals.vat, 2),
        "TotPrice": _q(request.totals.gross, 2),
        "IIC": iic,
        "IICSignature": iic_signature,
    }
    iic_ref = (getattr(request, "iic_ref", None) or "").strip()
    if iic_ref:
        inv_attrs["IICRef"] = iic_ref
    invoice = etree.SubElement(root, f"{{{EFI_NS}}}Invoice", inv_attrs)

    seller_attrs = {"IDType": "TIN", "IDNum": tenant.pib or "", "Name": tenant.name or ""}
    if company.address:
        seller_attrs["Address"] = company.address
    etree.SubElement(invoice, f"{{{EFI_NS}}}Seller", seller_attrs)

    if request.buyer and ((request.buyer.pib or "").strip() or (request.buyer.name or "").strip()):
        buyer_attrs = {
            "IDType": "TIN",
            "IDNum": (request.buyer.pib or "").strip(),
            "Name": (request.buyer.name or "").strip(),
        }
        if request.buyer.address:
            buyer_attrs["Address"] = request.buyer.address
        etree.SubElement(invoice, f"{{{EFI_NS}}}Buyer", buyer_attrs)

    items_el = etree.SubElement(invoice, f"{{{EFI_NS}}}Items")
    for line in request.lines:
        qty = Decimal(str(line.quantity))
        upb = Decimal(str(line.unit_price_net))
        gross = Decimal(str(line.total_gross)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pb = (upb * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vr = Decimal(str(line.vat_rate))
        va = (gross - pb).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        item_attrs = {
            "N": line.name or "",
            "C": line.code or "",
            "U": line.unit or "KOM",
            "Q": _qty(qty),
            "UPB": _q(upb, 4),
            "UPA": _q(upb, 4),
            "R": "0",
            "RR": "true",
            "PB": _q(pb, 2),
            "VR": _q(vr, 2),
            "VA": _q(va, 2),
            "PA": _q(gross, 2),
        }
        ex = exempt_from_vat_code(getattr(line, "tax_rate_code", None))
        if ex:
            item_attrs["EX"] = ex
        etree.SubElement(items_el, f"{{{EFI_NS}}}I", item_attrs)

    taxes_el = etree.SubElement(invoice, f"{{{EFI_NS}}}SameTaxes")
    for row in build_same_taxes(list(request.lines)):
        st_attrs = {
            "NumOfItems": str(row["numOfItems"]),
            "PriceBefVAT": row["priceBeforeVAT"],
            "VATRate": row["vatRate"],
            "VATAmt": row["vatAmt"],
        }
        if row.get("exemptFromVAT"):
            st_attrs["ExemptFromVAT"] = row["exemptFromVAT"]
        etree.SubElement(taxes_el, f"{{{EFI_NS}}}SameTax", st_attrs)

    pays = etree.SubElement(invoice, f"{{{EFI_NS}}}PayMethods")
    etree.SubElement(
        pays,
        f"{{{EFI_NS}}}PayMethod",
        Type=request.payment_method,
        Amt=_q(request.totals.gross, 2),
    )
    if request.notes:
        note = etree.SubElement(invoice, f"{{{EFI_NS}}}Note")
        note.text = request.notes
    return root


def wrap_soap(request_el: etree._Element) -> str:
    envelope = etree.Element(f"{{{SOAP_NS}}}Envelope", nsmap={"SOAP-ENV": SOAP_NS})
    etree.SubElement(envelope, f"{{{SOAP_NS}}}Header")
    body = etree.SubElement(envelope, f"{{{SOAP_NS}}}Body")
    body.append(request_el)
    xml = etree.tostring(envelope, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>' + xml
