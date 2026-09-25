"""Kompatibilni uvoz — PU CIS živi u sepko.pu (odvojeno od Navire/EXTFISK)."""
from sepko.pu.adapter import PuPartnerAdapter as CisPartnerAdapter
from sepko.pu.adapter import cis_service_url
from sepko.pu.invoice import build_register_invoice_request, wrap_soap

__all__ = [
    "CisPartnerAdapter",
    "cis_service_url",
    "build_register_invoice_request",
    "wrap_soap",
]
