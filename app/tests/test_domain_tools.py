"""
ERP AI Agent — Domain Tools Tests (Product Catalog + Fixed-Asset Lifecycle)
============================================================================
State coverage for the newly implemented trusted execution domains.
All tests are OFFLINE (database/engine mocked at module boundaries) —
live verification is performed separately by scripts/test_domain_tools_live.py.

Verified dimensions:
* product catalog: duplicate reuse, validation, code generation fallback
* fixed-asset lifecycle: acquisition (capitalised, never expensed),
  depreciation math + salvage cap + status guards, disposal gain/loss
  (balanced journals), double-disposal refusal, missing-asset states
* reasoning integration: DEPRECIATION event, asset impact maps,
  prohibited inventory movement
* planner: new intents, dependency-first tool lists, consolidated
  clarification for asset acquisitions
* control-plane wiring: tool registry + entity contract + permissions
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import fixed_asset_service as fas
from app.services import product_service
from app.tools import get_handler, list_tools
from app.reasoning import EconomicEvent, build_event_profile, classify_economic_event
from app.planner import plan

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")

ASSET_ACCOUNT = {"id": "aaaaaaaa-0000-0000-0000-000000000001", "code": "1500", "name": "Computer Equipment", "account_type": "ASSET"}
ACC_DEP_ACCOUNT = {"id": "aaaaaaaa-0000-0000-0000-000000000002", "code": "1501", "name": "Accumulated Depreciation", "account_type": "ASSET"}
DEP_EXPENSE_ACCOUNT = {"id": "aaaaaaaa-0000-0000-0000-000000000003", "code": "6200", "name": "Depreciation Expense", "account_type": "EXPENSE"}
CASH_ACCOUNT = {"id": "aaaaaaaa-0000-0000-0000-000000000004", "code": "1000", "name": "Cash", "account_type": "ASSET"}
LOSS_ACCOUNT = {"id": "aaaaaaaa-0000-0000-0000-000000000005", "code": "6300", "name": "Loss on Disposal", "account_type": "EXPENSE"}


def _asset(**over):
    row = {
        "id": "bbbbbbbb-0000-0000-0000-000000000001",
        "asset_code": "FA-0001",
        "name": "Office Laptop",
        "purchase_date": "2026-01-01",
        "purchase_cost": 120000.0,
        "salvage_value": 0.0,
        "useful_life_years": 4,
        "depreciation_method": "STRAIGHT_LINE",
        "accumulated_depreciation": 0.0,
        "book_value": 120000.0,
        "status": "ACTIVE",
        "gl_asset_account_id": ASSET_ACCOUNT["id"],
        "gl_depreciation_expense_account_id": DEP_EXPENSE_ACCOUNT["id"],
        "gl_accumulated_depreciation_account_id": ACC_DEP_ACCOUNT["id"],
    }
    row.update(over)
    return row


class _JournalCapture:
    """Captures journal construction; returns a posted-entry shape."""

    def __init__(self):
        self.calls = []

    async def prepare_journal(self, **kwargs):
        self.calls.append(kwargs)
        lines = kwargs["lines"]
        total_debit = sum(float(l.get("debit", 0)) for l in lines)
        total_credit = sum(float(l.get("credit", 0)) for l in lines)
        assert abs(total_debit - total_credit) < 0.01, "journal must balance"
        return {
            "entry": {"id": "cccccccc-0000-0000-0000-000000000009", "status": "DRAFT"},
            "lines": lines,
            "total_debit": total_debit,
            "total_credit": total_credit,
        }

    async def validate_journal(self, *, entry_id):
        return {"id": str(entry_id), "status": "VALIDATED"}

    async def post_journal(self, *, entry_id):
        return {"id": str(entry_id), "status": "POSTED"}


# ===================================================================
# Product catalog
# ===================================================================


class TestProductService:

    @pytest.mark.asyncio
    async def test_duplicate_name_is_reused_not_created(self):
        existing = [{"id": "p1", "name": "A4 Paper", "product_code": "PRD-1"}]
        with patch.object(product_service.repo, "search_products", return_value=existing), \
             patch.object(product_service.repo, "create_product") as create:
            result = await product_service.create(ORG, name="a4 paper")
        assert result["reused"] is True
        assert result["id"] == "p1"
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_product_is_created(self):
        with patch.object(product_service.repo, "search_products", return_value=[]), \
             patch.object(product_service.repo, "create_product", new=AsyncMock(return_value={"id": "p2", "name": "Toner"})) as create:
            result = await product_service.create(ORG, name="Toner", unit_price=8500)
        assert result["name"] == "Toner"
        create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blank_name_is_rejected(self):
        with pytest.raises(ValueError, match="name is required"):
            await product_service.create(ORG, name="   ")

    @pytest.mark.asyncio
    async def test_negative_price_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            await product_service.create(ORG, name="X", unit_price=-5)

    @pytest.mark.asyncio
    async def test_code_generation_falls_back_when_rpc_unavailable(self):
        async def rpc_fail(*a, **kw):
            raise RuntimeError("rpc down")
        with patch("app.repositories.product_repository.call_rpc", side_effect=rpc_fail):
            code = await product_service.repo.generate_product_code(ORG)
        assert code.startswith("PRD-")


# ===================================================================
# Fixed-asset lifecycle
# ===================================================================


class TestRegisterAsset:

    @pytest.mark.asyncio
    async def test_acquisition_capitalises_and_posts(self):
        cap = _JournalCapture()
        created = _asset()
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch("app.repositories.organization_repository.get_bank_accounts", new=AsyncMock(return_value=[])), \
             patch("app.accounting_engine._resolve_default_account", new=AsyncMock(return_value=CASH_ACCOUNT)), \
             patch.object(fas.accounting_service, "prepare_journal", cap.prepare_journal), \
             patch.object(fas.accounting_service, "validate_journal", cap.validate_journal), \
             patch.object(fas.accounting_service, "post_journal", cap.post_journal):
            acct.get_chart_of_accounts = AsyncMock(return_value=[ASSET_ACCOUNT])
            acct.get_account = AsyncMock(return_value=CASH_ACCOUNT)
            repo.create_asset = AsyncMock(return_value=dict(created))
            repo.update_asset_by_id = AsyncMock(return_value=None)
            repo.record_asset_transaction = AsyncMock(return_value={})
            result = await fas.register_asset(
                ORG, name="Office Laptop", purchase_cost=120000,
                payment_method="CASH",
            )
        assert result["journal_entry"]["status"] == "POSTED"
        lines = cap.calls[0]["lines"]
        # Dr asset / Cr cash — capitalised, NEVER expensed
        assert lines[0]["debit"] == 120000 and lines[1]["credit"] == 120000
        assert lines[0]["account_id"] == ASSET_ACCOUNT["id"]
        # source-tied to the asset record
        assert cap.calls[0]["source_id"] == uuid.UUID(created["id"])
        repo.update_asset_by_id.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_asset_account_is_configuration_gap_not_guess(self):
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo:
            acct.get_chart_of_accounts = AsyncMock(return_value=[])
            repo.create_asset = AsyncMock()
            with pytest.raises(ValueError, match="fixed-asset account"):
                await fas.register_asset(ORG, name="X", purchase_cost=100)
        repo.create_asset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_credit_without_supplier_is_refused(self):
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch.object(fas, "supplier_repo") as sup:
            acct.get_chart_of_accounts = AsyncMock(return_value=[ASSET_ACCOUNT])
            sup.get_supplier = AsyncMock(return_value=None)
            sup.search_suppliers = AsyncMock(return_value=[])
            repo.create_asset = AsyncMock()
            with pytest.raises(ValueError, match="not found"):
                await fas.register_asset(
                    ORG, name="X", purchase_cost=100, payment_method="CREDIT",
                    supplier_name="Ghost Supplier",
                )
        repo.create_asset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_journal_failure_surfaces_partial_state_honestly(self):
        async def fail(**kw):
            raise ValueError("no accounts")
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch("app.repositories.organization_repository.get_bank_accounts", new=AsyncMock(return_value=[])), \
             patch("app.accounting_engine._resolve_default_account", new=AsyncMock(return_value=CASH_ACCOUNT)), \
             patch.object(fas.accounting_service, "prepare_journal", fail):
            acct.get_chart_of_accounts = AsyncMock(return_value=[ASSET_ACCOUNT])
            acct.get_account = AsyncMock(return_value=CASH_ACCOUNT)
            repo.create_asset = AsyncMock(return_value=_asset())
            result = await fas.register_asset(
                ORG, name="Office Laptop", purchase_cost=120000, payment_method="CASH",
            )
        assert "journal_warning" in result  # never pretend the journal happened


def _install_account_lookup(acct, *accounts):
    """AsyncMock get_account resolving by id across the given accounts."""
    lookup = {a["id"]: a for a in accounts}

    async def _get(organization_id, *, account_id):
        return lookup.get(str(account_id))

    acct.get_account = AsyncMock(side_effect=_get)


class TestRecordDepreciation:

    @pytest.mark.asyncio
    async def test_straight_line_monthly_amount_and_schedule(self):
        cap = _JournalCapture()
        asset = _asset(useful_life_years=4, purchase_cost=120000, salvage_value=0)
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch.object(fas.accounting_service, "prepare_journal", cap.prepare_journal), \
             patch.object(fas.accounting_service, "validate_journal", cap.validate_journal), \
             patch.object(fas.accounting_service, "post_journal", cap.post_journal):
            acct.get_chart_of_accounts = AsyncMock(
                return_value=[ACC_DEP_ACCOUNT, DEP_EXPENSE_ACCOUNT]
            )
            _install_account_lookup(acct, ACC_DEP_ACCOUNT, DEP_EXPENSE_ACCOUNT)
            repo.get_asset = AsyncMock(return_value=dict(asset))
            repo.search_assets = AsyncMock(return_value=[dict(asset)])
            repo.update_asset_by_id = AsyncMock(return_value=None)
            repo.record_asset_transaction = AsyncMock(return_value={})
            repo.record_depreciation_schedule = AsyncMock(return_value={})
            result = await fas.record_depreciation(ORG, asset_name="Office Laptop")
        # (120000 - 0) / 4 years / 12 months = 2500
        assert result["depreciation_amount"] == 2500.0
        assert result["accumulated_depreciation"] == 2500.0
        assert result["book_value"] == 117500.0
        schedule = repo.record_depreciation_schedule.call_args.kwargs
        assert schedule["opening_book_value"] == 120000.0
        assert schedule["closing_book_value"] == 117500.0
        assert schedule["journal_entry_id"] is not None
        lines = cap.calls[0]["lines"]
        assert lines[0]["account_id"] == DEP_EXPENSE_ACCOUNT["id"]  # Dr expense
        assert lines[1]["account_id"] == ACC_DEP_ACCOUNT["id"]      # Cr accum. dep.

    @pytest.mark.asyncio
    async def test_depreciation_never_exceeds_cost_minus_salvage(self):
        cap = _JournalCapture()
        asset = _asset(purchase_cost=1000, salvage_value=900,
                       accumulated_depreciation=90, book_value=910)
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch.object(fas.accounting_service, "prepare_journal", cap.prepare_journal), \
             patch.object(fas.accounting_service, "validate_journal", cap.validate_journal), \
             patch.object(fas.accounting_service, "post_journal", cap.post_journal):
            acct.get_chart_of_accounts = AsyncMock(
                return_value=[ACC_DEP_ACCOUNT, DEP_EXPENSE_ACCOUNT]
            )
            _install_account_lookup(acct, ACC_DEP_ACCOUNT, DEP_EXPENSE_ACCOUNT)
            repo.get_asset = AsyncMock(return_value=dict(asset))
            repo.search_assets = AsyncMock(return_value=[dict(asset)])
            repo.update_asset_by_id = AsyncMock(return_value=None)
            repo.record_asset_transaction = AsyncMock(return_value={})
            repo.record_depreciation_schedule = AsyncMock(return_value={})
            result = await fas.record_depreciation(
                ORG, asset_name="X", depreciation_amount=500
            )
        # capped at the remaining depreciable amount (1000 − 900 − 90 = 10)
        assert result["depreciation_amount"] == 10.0

    @pytest.mark.asyncio
    async def test_disposed_asset_cannot_be_depreciated(self):
        with patch.object(fas, "repo") as repo:
            repo.search_assets = AsyncMock(
                return_value=[_asset(status="DISPOSED")]
            )
            with pytest.raises(ValueError, match="no longer be depreciated"):
                await fas.record_depreciation(ORG, asset_name="X")

    @pytest.mark.asyncio
    async def test_missing_asset_is_dependency_gap_not_failure(self):
        with patch.object(fas, "repo") as repo:
            repo.search_assets = AsyncMock(return_value=[])
            with pytest.raises(ValueError, match="is registered"):
                await fas.record_depreciation(ORG, asset_name="Ghost Asset")


class TestDisposeAsset:

    @pytest.mark.asyncio
    async def test_loss_disposal_builds_balanced_journal_and_updates_state(self):
        cap = _JournalCapture()
        asset = _asset(purchase_cost=120000, accumulated_depreciation=50000, book_value=70000)
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch("app.repositories.organization_repository.get_bank_accounts", new=AsyncMock(return_value=[])), \
             patch("app.accounting_engine._resolve_default_account", new=AsyncMock(return_value=CASH_ACCOUNT)), \
             patch.object(fas.accounting_service, "prepare_journal", cap.prepare_journal), \
             patch.object(fas.accounting_service, "validate_journal", cap.validate_journal), \
             patch.object(fas.accounting_service, "post_journal", cap.post_journal):
            acct.get_chart_of_accounts = AsyncMock(
                return_value=[ASSET_ACCOUNT, ACC_DEP_ACCOUNT, LOSS_ACCOUNT]
            )
            _install_account_lookup(acct, ASSET_ACCOUNT, ACC_DEP_ACCOUNT, CASH_ACCOUNT)
            repo.get_asset = AsyncMock(return_value=dict(asset))
            repo.search_assets = AsyncMock(return_value=[dict(asset)])
            repo.update_asset_by_id = AsyncMock(return_value=None)
            repo.record_asset_transaction = AsyncMock(return_value={})
            result = await fas.dispose_asset(
                ORG, asset_name="Office Laptop",
                disposal_amount=60000, disposal_type="SALE",
            )
        assert result["loss_on_disposal"] == 10000.0
        lines = cap.calls[0]["lines"]
        debits = sum(float(l["debit"]) for l in lines)
        credits = sum(float(l["credit"]) for l in lines)
        assert debits == credits == 120000.0
        # Dr cash 60000 + Dr acc-dep 50000 + Dr loss 10000 / Cr asset 120000
        assert [float(l["debit"]) for l in lines[:3]] == [60000.0, 50000.0, 10000.0]
        assert lines[-1]["credit"] == 120000.0
        fields = repo.update_asset_by_id.call_args_list[0].kwargs["fields"]
        assert fields["status"] == "SOLD"   # asset_status enum mapping
        assert fields["disposal_amount"] == 60000

    @pytest.mark.asyncio
    async def test_gain_disposal_credits_gain_account(self):
        cap = _JournalCapture()
        asset = _asset(purchase_cost=100000, accumulated_depreciation=80000, book_value=20000)
        gain_account = {"id": "aaaaaaaa-0000-0000-0000-000000000006", "name": "Gain on Disposal", "account_type": "REVENUE"}
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo, \
             patch("app.repositories.organization_repository.get_bank_accounts", new=AsyncMock(return_value=[])), \
             patch("app.accounting_engine._resolve_default_account", new=AsyncMock(return_value=CASH_ACCOUNT)), \
             patch.object(fas.accounting_service, "prepare_journal", cap.prepare_journal), \
             patch.object(fas.accounting_service, "validate_journal", cap.validate_journal), \
             patch.object(fas.accounting_service, "post_journal", cap.post_journal):
            acct.get_chart_of_accounts = AsyncMock(
                return_value=[ASSET_ACCOUNT, ACC_DEP_ACCOUNT, gain_account]
            )
            _install_account_lookup(acct, ASSET_ACCOUNT, ACC_DEP_ACCOUNT, CASH_ACCOUNT)
            repo.get_asset = AsyncMock(return_value=dict(asset))
            repo.search_assets = AsyncMock(return_value=[dict(asset)])
            repo.update_asset_by_id = AsyncMock(return_value=None)
            repo.record_asset_transaction = AsyncMock(return_value={})
            result = await fas.dispose_asset(
                ORG, asset_name="Office Laptop", disposal_amount=35000,
            )
        assert result["gain_on_disposal"] == 15000.0
        assert result["loss_on_disposal"] == 0.0
        lines = cap.calls[0]["lines"]
        assert sum(float(l["debit"]) for l in lines) == sum(float(l["credit"]) for l in lines)

    @pytest.mark.asyncio
    async def test_double_disposal_is_refused(self):
        with patch.object(fas, "repo") as repo:
            repo.search_assets = AsyncMock(
                return_value=[_asset(status="DISPOSED")]
            )
            with pytest.raises(ValueError, match="cannot be disposed of again"):
                await fas.dispose_asset(ORG, asset_name="X")

    @pytest.mark.asyncio
    async def test_missing_disposal_account_is_configuration_gap(self):
        """Asset WITHOUT configured GL accounts + no Accumulated
        Depreciation account in the COA → precise configuration gap."""
        with patch.object(fas, "account_repo") as acct, \
             patch.object(fas, "repo") as repo:
            acct.get_chart_of_accounts = AsyncMock(return_value=[ASSET_ACCOUNT])
            repo.get_asset = AsyncMock(return_value=_asset(
                gl_asset_account_id=None,
                gl_accumulated_depreciation_account_id=None,
            ))
            repo.search_assets = AsyncMock(return_value=[])
            # resolve by id so get_asset is used (no search match needed)
            with pytest.raises(ValueError, match="not configured"):
                await fas.dispose_asset(
                    ORG, asset_id=_asset()["id"],
                )


# ===================================================================
# Reasoning integration
# ===================================================================


class TestAssetReasoning:

    def test_event_mapping(self):
        assert classify_economic_event("register_fixed_asset") is EconomicEvent.ACQUISITION
        assert classify_economic_event("dispose_fixed_asset") is EconomicEvent.DISPOSAL
        assert classify_economic_event("record_asset_depreciation") is EconomicEvent.DEPRECIATION
        assert classify_economic_event("create_product") is EconomicEvent.RECORD_CREATION

    def test_asset_disposal_is_not_inventory_and_not_revenue(self):
        profile = build_event_profile("dispose_fixed_asset")
        assert profile.affected["fixed_asset"] is True
        assert profile.affected["inventory"] is False
        assert profile.affected["revenue"] is False  # proceeds ≠ sales revenue
        assert any(p.action == "inventory_movement" for p in profile.prohibited)

    def test_asset_acquisition_is_capitalised_never_expensed(self):
        profile = build_event_profile("register_fixed_asset")
        assert profile.affected["fixed_asset"] is True
        assert profile.affected["expense"] is False
        assert profile.affected["inventory"] is False
        assert any(p.action == "inventory_movement" for p in profile.prohibited)

    def test_depreciation_is_non_cash_expense(self):
        profile = build_event_profile("record_asset_depreciation")
        assert profile.affected["journal"] is True
        assert profile.affected["expense"] is True
        assert profile.affected["cash_bank"] is False
        assert profile.affected["fixed_asset"] is True


class TestAssetPlanning:

    def test_register_asset_intent_and_tools(self):
        p = plan("Register a new fixed asset generator for Rs.850,000 paid in cash")
        assert p.intent == "register_fixed_asset"
        assert p.economic_event == EconomicEvent.ACQUISITION.value
        assert p.requires_confirmation is True
        assert "register_fixed_asset" in p.potential_tools
        assert "search_fixed_asset" in p.potential_tools  # dependency-first

    def test_asset_acquisition_without_payment_keyword_asks(self):
        p = plan("Register a new fixed asset generator for Rs.850,000")
        assert p.requires_clarification is True
        assert "payment_type" in p.missing_fields

    def test_dispose_intent(self):
        p = plan("Sell the office laptop asset for Rs.40000")
        assert p.intent == "dispose_fixed_asset"
        assert p.economic_event == EconomicEvent.DISPOSAL.value
        assert p.impact_map.get("inventory") is False

    def test_depreciation_intent(self):
        p = plan("Record depreciation for Office Laptop")
        assert p.intent == "record_asset_depreciation"
        # Work Stream A: the asset name resolves, so the ONLY remaining
        # gap is the mandatory transaction date (asked, never defaulted).
        assert p.missing_fields == ["transaction_date"]

    def test_depreciation_without_asset_asks_which_asset(self):
        p = plan("Record depreciation")
        assert p.requires_clarification is True
        assert "asset_name" in p.missing_fields


# ===================================================================
# Control-plane wiring
# ===================================================================


class TestControlPlaneWiring:

    def test_new_tools_are_registered_with_correct_read_only_flags(self):
        tools = set(list_tools())
        assert {"search_product", "create_product", "search_fixed_asset",
                "get_fixed_asset", "register_fixed_asset",
                "dispose_fixed_asset", "record_asset_depreciation"} <= tools
        for slug in ("search_product", "search_fixed_asset", "get_fixed_asset"):
            assert get_handler(slug)["read_only"] is True
        for slug in ("create_product", "register_fixed_asset",
                     "dispose_fixed_asset", "record_asset_depreciation"):
            assert get_handler(slug)["read_only"] is False

    def test_entity_contract_covers_new_tools(self):
        from app.entity_contract import _TOOL_ENTITY_MAP
        assert _TOOL_ENTITY_MAP["register_fixed_asset"] == ("fixed_asset", "created")
        assert _TOOL_ENTITY_MAP["dispose_fixed_asset"] == ("fixed_asset", "disposed")
        assert _TOOL_ENTITY_MAP["create_product"] == ("product", "created")

    def test_read_only_domain_tools_are_always_allowed(self):
        from app.permissions import _ALWAYS_ALLOWED
        assert {"search_product", "search_fixed_asset", "get_fixed_asset"} <= _ALWAYS_ALLOWED





