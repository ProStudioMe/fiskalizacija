from __future__ import annotations

from datetime import date
from decimal import Decimal

from sepko.brand import LICENSE_ITEM
from sepko.license_invoice import (
    LicenseInvoiceDraft,
    PlatformBilling,
    fmt_date,
    render_license_invoice_html,
    validate_email_list,
)


def test_validate_email_list():
    assert validate_email_list("finansije@prostudio.me") == ["finansije@prostudio.me"]
    assert validate_email_list("bad") == []
    assert validate_email_list("a@b.me, c@d.me") == ["a@b.me", "c@d.me"]


def test_default_license_item_is_proracun():
    assert PlatformBilling().item_name == LICENSE_ITEM
    assert PlatformBilling().issuer_name == "PROSTUDIO.ME DOO"
    assert PlatformBilling().issuer_email == "finansije@prostudio.me"
    assert "ProRačun" in LICENSE_ITEM


def test_proforma_html_uses_prostudio_and_label():
    billing = PlatformBilling()
    draft = LicenseInvoiceDraft(
        number="PF-26-0001",
        buyer_name="PHILIA DOO",
        to_email="admin@philia.me",
        issue_date=date(2026, 9, 7),
        due_date=date(2026, 9, 14),
        amount=Decimal("90.00"),
        qty=Decimal("6"),
        qty_unit="mj",
        item_name="ProRačun Basic WEB",
        payment_method="Transakcioni Račun, Virman",
        message="Poštovani,\nu prilogu je profaktura.",
        iban="CKB: 510-1",
        issuer_name=billing.issuer_name,
        issuer_address=billing.issuer_address,
        issuer_email=billing.issuer_email,
        issuer_pib=billing.issuer_pib,
        issuer_phone=billing.issuer_phone,
        kind="proforma",
    )
    html = render_license_invoice_html(draft)
    assert "Profaktura PF-26-0001" in html
    assert "PHILIA DOO" in html
    assert "PROSTUDIO.ME DOO" in html
    assert "prostudio.me" in html
    assert "90,00" in html
    assert fmt_date(draft.due_date) == "14.09.2026"
