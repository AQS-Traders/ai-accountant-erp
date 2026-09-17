"""
ERP AI Agent — Service Catalog Tests
=====================================
State coverage for the service-catalog domain (Phase 3).

Database contract (public.services):
* service_code NOT NULL, UNIQUE per org, NO trigger → repository generates
* billing_unit is the service_unit_code enum (HOUR/DAY/MONTH/FIXED/ITEM)
* standard_rate >= 0 (CHECK); cost_rate NULL or >= 0 (CHECK)
* NO stock semantics — a service is never inventory, never a fixed asset

All tests are OFFLINE; live verification is performed by
scripts/test_domain_tools_live.py (services section).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.services import service_service
from app.tools import get_handler, list_tools
from app.reasoning import EconomicEvent, build_event_profile, classify_economic_event
from app.planner import plan

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ===================================================================
# Service catalog business logic
# ===================================================================


class TestServiceService:

    @pytest.mark.asyncio
    async def test_duplicate_name_is_reused_not_created(self):
        existing = [{"id": "s1", "name": "Tax Consulting", "service_code": "SRV-1"}]
        with patch_search(existing), \
             patch_create({"id": "nope"}) as create:
            result = await service_service.create(ORG, name="tax consulting")
        assert result["reused"] is True
        assert result["id"] == "s1"
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_service_is_created(self):
        with patch_search([]), \
             patch_create({"id": "s2", "name": "Audit Support"}) as create:
            result = await service_service.create(
                ORG, name="Audit Support", standard_rate=15000, billing_unit="DAY",
            )
        assert result["name"] == "Audit Support"
        create.assert_awaited_once()
        # billing unit + rate passed through to the repository contract
        kwargs = create.call_args.kwargs
        assert kwargs["billing_unit"] == "DAY"
        assert kwargs["standard_rate"] == 15000.0

    @pytest.mark.asyncio
    async def test_blank_name_is_rejected(self):
        with pytest.raises(ValueError, match="name is required"):
            await service_service.create(ORG, name="   ")

    @pytest.mark.asyncio
    async def test_negative_standard_rate_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            await service_service.create(ORG, name="X", standard_rate=-1)

    @pytest.mark.asyncio
    async def test_negative_cost_rate_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            await service_service.create(ORG, name="X", cost_rate=-5)

    @pytest.mark.asyncio
    async def test_invalid_billing_unit_is_rejected(self):
        with pytest.raises(ValueError, match="billing_unit"):
            await service_service.create(ORG, name="X", billing_unit="KILOGRAM")

    @pytest.mark.asyncio
    async def test_valid_billing_units_accepted(self):
        for unit in ("HOUR", "DAY", "MONTH", "FIXED", "ITEM"):
            with patch_search([]), \
                 patch_create({"id": "s", "name": "X"}) as create:
                await service_service.create(ORG, name="X", billing_unit=unit)
            assert create.call_args.kwargs["billing_unit"] == unit

    @pytest.mark.asyncio
    async def test_code_generation_falls_back_when_rpc_unavailable(self):
        async def rpc_fail(*a, **kw):
            raise RuntimeError("rpc down")
        with patch("app.repositories.service_repository.call_rpc", side_effect=rpc_fail):
            code = await service_service.repo.generate_service_code(ORG)
        assert code.startswith("SRV-")

    @pytest.mark.asyncio
    async def test_empty_catalog_search_is_valid_information(self):
        with patch_search([]):
            result = await service_service.search(ORG, query="Nothing")
        assert result == []  # [] = no service exists — NOT a failure


# ===================================================================
# Helpers
# ===================================================================


def patch_search(rows):
    return patch.object(service_service.repo, "search_services", return_value=rows)


def patch_create(row):
    return patch.object(service_service.repo, "create_service", return_value=row)


# ===================================================================
# Reasoning + planner integration
# ===================================================================


class TestServiceReasoning:

    def test_event_mapping(self):
        assert classify_economic_event("create_service") is EconomicEvent.RECORD_CREATION
        assert classify_economic_event("search_service") is EconomicEvent.REPORTING

    def test_service_creation_has_no_party_and_no_inventory(self):
        """A service catalog record is master data: no party ledger, no
        stock, no journal — creating one must never touch inventory."""
        profile = build_event_profile("create_service")
        assert profile.affected["party_ledger"] is False
        assert profile.affected["inventory"] is False
        assert profile.affected["journal"] is False


class TestServicePlanning:

    def test_create_service_intent_and_tools(self):
        p = plan("Add a new service called Tax Consulting with rate 5000/hr")
        assert p.intent == "create_service"
        assert p.economic_event == EconomicEvent.RECORD_CREATION.value
        assert "create_service" in p.potential_tools
        assert "search_service" in p.potential_tools  # dependency-first
        assert p.requires_clarification is False

    def test_service_sale_is_not_confused_with_service_creation(self):
        """A service SALE must remain a sale event — the word 'service'
        must not redirect it to catalog creation."""
        p = plan("Sold a website maintenance service to TechVision for Rs.30,000 cash")
        assert p.intent == "record_cash_sale"
        assert p.economic_event == EconomicEvent.DISPOSAL.value


# ===================================================================
# Control-plane wiring
# ===================================================================


class TestServiceControlPlaneWiring:

    def test_new_tools_are_registered_with_correct_read_only_flags(self):
        tools = set(list_tools())
        assert {"search_service", "create_service"} <= tools
        assert get_handler("search_service")["read_only"] is True
        assert get_handler("create_service")["read_only"] is False

    def test_entity_contract_covers_new_tools(self):
        from app.entity_contract import _TOOL_ENTITY_MAP
        assert _TOOL_ENTITY_MAP["create_service"] == ("service", "created")
        assert _TOOL_ENTITY_MAP["search_service"] == ("service", "fetched")

    def test_read_only_service_search_is_always_allowed(self):
        from app.permissions import _ALWAYS_ALLOWED
        assert "search_service" in _ALWAYS_ALLOWED
