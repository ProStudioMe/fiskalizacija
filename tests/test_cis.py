"""Compat uvoz — PU adapter je u sepko.pu."""
from sepko.cis import CisPartnerAdapter, cis_service_url
from sepko.pu.adapter import PuPartnerAdapter


def test_cis_alias_is_pu_adapter():
    assert CisPartnerAdapter is PuPartnerAdapter
    assert callable(cis_service_url)
