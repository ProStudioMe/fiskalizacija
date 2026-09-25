"""Per-tenant fiscal channel: Navira vs EXTFISK vs Poreska CIS."""
from __future__ import annotations

import json

from sepko.config import Settings
from sepko.efi import TenantFiscal, load_tenant_fiscal, save_tenant_fiscal
from sepko.models import Tenant
from sepko.partner import (
    HttpPartnerAdapter,
    MockPartnerAdapter,
    get_partner_adapter,
    resolve_fiscal_channel,
)
from sepko.pu.adapter import PuPartnerAdapter
from sepko.extfisk import ExtfiskPartnerAdapter


def _tenant(channel: str = "", **extra) -> Tenant:
    data = {
        "busin_unit_code": "ps000bu001",
        "tcr_code": "ps000cr001",
        "soft_code": "sepko00001",
        "operator_code": "op00000011",
        "fiscal_channel": channel,
    }
    data.update(extra)
    return Tenant(
        id=1,
        slug="prostudio",
        name="PROSTUDIO.ME DOO",
        pib="03462668",
        status="active",
        mode="test",
        settings_json=json.dumps(data, ensure_ascii=False),
    )


def _settings(mode: str = "mock") -> Settings:
    return Settings(secret_key="test-secret-key-for-unit", partner_mode=mode)


def test_inherit_global_mode():
    tenant = _tenant("")
    assert resolve_fiscal_channel(tenant, _settings("navira")) == "navira"
    assert resolve_fiscal_channel(tenant, _settings("extfisk")) == "extfisk"
    assert resolve_fiscal_channel(tenant, _settings("poreska")) == "poreska"
    assert resolve_fiscal_channel(tenant, _settings("http")) == "navira"


def test_tenant_overrides_global():
    tenant = _tenant("poreska")
    assert resolve_fiscal_channel(tenant, _settings("navira")) == "poreska"
    assert isinstance(get_partner_adapter(tenant, _settings("navira")), PuPartnerAdapter)


def test_adapter_for_each_channel():
    settings = _settings("mock")
    assert isinstance(get_partner_adapter(_tenant("navira"), settings), HttpPartnerAdapter)
    assert isinstance(get_partner_adapter(_tenant("extfisk"), settings), ExtfiskPartnerAdapter)
    assert isinstance(get_partner_adapter(_tenant("poreska"), settings), PuPartnerAdapter)
    assert isinstance(get_partner_adapter(_tenant("mock"), settings), MockPartnerAdapter)


def test_save_load_fiscal_channel():
    tenant = Tenant(slug="x", name="X", pib="1", settings_json="{}")
    save_tenant_fiscal(tenant, TenantFiscal(fiscal_channel="navira"))
    loaded = load_tenant_fiscal(tenant)
    assert loaded.fiscal_channel == "navira"
    save_tenant_fiscal(tenant, TenantFiscal(fiscal_channel="poreska"))
    assert load_tenant_fiscal(tenant).fiscal_channel == "poreska"
