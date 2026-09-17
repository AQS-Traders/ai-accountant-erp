"""
ERP AI Agent — Reasoning-fix regression tests (final verification pass).

Covers the five confirmed defects fixed in this pass:
  P1  trusted record_cash_sale tool (planner + registry wiring)
  P2  account-code resolver (no silent unique-constraint failure)
  P3  party-tool narrowing once a search resolves the party
  P4  advertising/rent category-gap detection (no silent Utilities default)
  P5  (bank-transfer guard is agent-flow level; covered by live tests)
"""

from __future__ import annotations

import asyncio

import pytest

import app.repositories.account_repository as account_repo
import app.classifier as classifier
from app.agent import cash_party_creation_blocked, party_resolved_in_search
from app.classifier import _account_matches_expense_label, rule_based_nature
from app.planner import _tools_for_intent
from app.tools import get_handler


# ---------------------------------------------------------------------------
# P1 — trusted record_cash_sale tool wiring
# ---------------------------------------------------------------------------


def test_record_cash_sale_tool_registered_as_mutation():
    entry = get_handler("record_cash_sale")
    assert entry is not None
    assert entry["read_only"] is False


def test_cash_sale_planner_uses_trusted_tool_not_manual_journal():
    tools = _tools_for_intent("record_cash_sale")
    assert "record_cash_sale" in tools
    # The model must never assemble this journal manually any more.
    assert "prepare_journal" not in tools
    assert "post_journal" not in tools
    assert "validate_journal" not in tools


def test_cash_sale_intent_still_blocks_party_creation():
    assert cash_party_creation_blocked("record_cash_sale", "create_customer")
    assert cash_party_creation_blocked("record_cash_sale", "create_supplier")
    assert not cash_party_creation_blocked("record_cash_sale", "record_cash_sale")


# ---------------------------------------------------------------------------
# P2 — account code resolver
# ---------------------------------------------------------------------------


def _patch_code_lookup(monkeypatch, taken):
    async def fake_get_by_code(organization_id, *, code):
        return {"code": code} if code in taken else None

    monkeypatch.setattr(account_repo, "get_account_by_code", fake_get_by_code)


def test_next_available_code_free_code_returned_verbatim(monkeypatch):
    _patch_code_lookup(monkeypatch, taken=set())

    async def run():
        return await account_repo.next_available_code(
            "00000000-0000-0000-0000-000000000001", "6151"
        )

    assert asyncio.run(run()) == "6151"


def test_next_available_code_bumps_to_next_free_in_series(monkeypatch):
    _patch_code_lookup(monkeypatch, taken={"6150", "6151"})

    async def run():
        return await account_repo.next_available_code(
            "00000000-0000-0000-0000-000000000001", "6150"
        )

    assert asyncio.run(run()) == "6152"


def test_next_available_code_skips_run_of_taken_codes(monkeypatch):
    taken = {"6150", "6151", "6152", "6153"}

    async def fake(organization_id, *, code):
        return {"code": code} if code in taken else None

    monkeypatch.setattr(account_repo, "get_account_by_code", fake)

    async def run():
        return await account_repo.next_available_code(
            "00000000-0000-0000-0000-000000000001", "6150"
        )

    assert asyncio.run(run()) == "6154"


def test_next_available_code_non_numeric_suffix(monkeypatch):
    _patch_code_lookup(monkeypatch, taken={"ACC"})

    async def run():
        return await account_repo.next_available_code(
            "00000000-0000-0000-0000-000000000001", "ACC"
        )

    assert asyncio.run(run()) == "ACC1"


# ---------------------------------------------------------------------------
# P3 — party-tool narrowing once a search resolves the party
# ---------------------------------------------------------------------------


def test_party_resolved_when_search_hits_named_party():
    rows = [{"id": "s1", "name": "XYZ Computers"}, {"id": "s2", "name": "Other"}]
    assert party_resolved_in_search(rows, "XYZ Computers") is True


def test_party_resolved_case_and_partial_insensitive():
    rows = [{"name": "xyz computers (lahore)"}]
    assert party_resolved_in_search(rows, "XYZ Computers") is True


def test_party_not_resolved_when_absent_or_unnamed():
    assert party_resolved_in_search([{"name": "Someone Else"}], "XYZ Computers") is False
    assert party_resolved_in_search([{"name": "XYZ Computers"}], None) is False
    assert party_resolved_in_search([], "XYZ Computers") is False
    assert party_resolved_in_search("unexpected-shape", "XYZ Computers") is False


# ---------------------------------------------------------------------------
# P4 — advertising / category-gap detection (no silent Utilities default)
# ---------------------------------------------------------------------------


def test_advertising_message_classified_as_marketing_expense():
    nature, confidence = rule_based_nature(
        "facebook advertising campaign", {}
    )
    assert nature == "OPERATING_EXPENSE"
    assert confidence in ("HIGH", "MEDIUM")


def test_utilities_account_is_NOT_a_marketing_match():
    assert _account_matches_expense_label(
        {"name": "6140 Utilities Expense"},
        "Marketing & advertising expense",
    ) is False


def test_advertising_account_IS_a_marketing_match():
    assert _account_matches_expense_label(
        {"name": "Advertising & Marketing"},
        "Marketing & advertising expense",
    ) is True


def test_rent_label_matches_rent_account_only():
    assert _account_matches_expense_label(
        {"name": "Rent Expense"}, "Rent expense"
    ) is True
    assert _account_matches_expense_label(
        {"name": "Utilities Expense"}, "Rent expense"
    ) is False


@pytest.mark.asyncio
async def test_classify_transaction_flags_advertising_gap(monkeypatch):
    """Advertising spend with NO advertising account and ONLY a Utilities
    expense account in the COA must surface the configuration gap — never
    silently hint the default Utilities account (live defect S8)."""

    async def fake_search(organization_id, *, query, limit=50):
        return []  # no advertising/marketing account exists

    async def fake_coa(organization_id, account_type=None, limit=5):
        if account_type == "EXPENSE":
            return [{"id": "a-6140", "code": "6140", "name": "Utilities Expense",
                     "account_type": "EXPENSE", "is_active": True}]
        return []

    monkeypatch.setattr(account_repo, "search_accounts", fake_search)
    monkeypatch.setattr(account_repo, "get_chart_of_accounts", fake_coa)

    result = await classifier.classify_transaction(
        organization_id="00000000-0000-0000-0000-000000000001",
        intent="record_expense",
        entities={"amount": 15000.0},
        message="I spent 15,000 PKR cash on a Facebook advertising campaign",
    )
    assert result.transaction_nature == "OPERATING_EXPENSE"
    assert result.requires_clarification is True
    assert "account" in (result.clarification_reason or "").lower()


@pytest.mark.asyncio
async def test_classify_transaction_proposes_account_never_silent_create(monkeypatch):
    """A KNOWN expense category whose account is missing is PROPOSED (name +
    free code) and asked — the classifier never auto-creates it (the
    account-creation confirmation policy) and never hints a generic default."""

    async def fake_search(organization_id, *, query, limit=50):
        return []  # no utilities account exists

    async def fake_coa(organization_id, account_type=None, limit=5):
        if account_type == "EXPENSE":
            return [{"id": "a-gen", "code": "6999", "name": "General Operating Expense",
                     "account_type": "EXPENSE", "is_active": True}]
        return []

    async def fake_next_code(organization_id, base_code):
        return base_code  # a free code in the series

    async def forbid_create(*args, **kwargs):
        raise AssertionError("create_account must never run inside the classifier")

    monkeypatch.setattr(account_repo, "search_accounts", fake_search)
    monkeypatch.setattr(account_repo, "get_chart_of_accounts", fake_coa)
    monkeypatch.setattr(account_repo, "next_available_code", fake_next_code)
    monkeypatch.setattr(account_repo, "create_account", forbid_create)

    result = await classifier.classify_transaction(
        organization_id="00000000-0000-0000-0000-000000000001",
        intent="record_expense",
        entities={"amount": 20000.0},
        message="record electricity bill of 20,000",
    )
    assert result.transaction_nature == "OPERATING_EXPENSE"
    assert result.requires_clarification is True
    assert result.proposed_account_name == "Utilities Expense"
    assert result.proposed_account_code == "6130"
    assert "create 'utilities expense'" in (result.clarification_reason or "").lower()


@pytest.mark.asyncio
async def test_classify_transaction_hints_existing_advertising_account(monkeypatch):
    """When an Advertising account EXISTS, the classifier hints it directly
    (no unnecessary clarification)."""

    async def fake_search(organization_id, *, query, limit=50):
        if "advertis" in query.lower():
            return [{"id": "a-6151", "code": "6151",
                     "name": "Advertising & Marketing",
                     "account_type": "EXPENSE", "is_active": True}]
        return []

    async def fake_coa(organization_id, account_type=None, limit=5):
        return [{"id": "a-6140", "code": "6140", "name": "Utilities Expense",
                 "account_type": "EXPENSE", "is_active": True}]

    monkeypatch.setattr(account_repo, "search_accounts", fake_search)
    monkeypatch.setattr(account_repo, "get_chart_of_accounts", fake_coa)

    result = await classifier.classify_transaction(
        organization_id="00000000-0000-0000-0000-000000000001",
        intent="record_expense",
        entities={"amount": 15000.0},
        message="I spent 15,000 PKR cash on a Facebook advertising campaign",
    )
    assert result.requires_clarification is False
    assert result.account_hint_code == "6151"
    assert result.account_hint_name == "Advertising & Marketing"
