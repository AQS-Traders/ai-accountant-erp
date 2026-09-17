"""
Offline unit tests for the agent's reasoning-safety guards:

1. Confirmation disclosure — the approval summary must disclose WHAT will
   happen (transaction, amount, party, every entity that will be created).
2. One-off-cash rule — party-creation tools must be refused for cash
   transaction intents at the reasoning/plan layer.
"""

from __future__ import annotations

import pytest

from app.agent import _confirmation_summary, cash_party_creation_blocked
from app.models.schemas import ExecutionPlan


def _plan(intent: str, tools: list, entities: dict | None = None,
          entity_name: str | None = None) -> ExecutionPlan:
    return ExecutionPlan(
        intent=intent,
        entity_name=entity_name,
        potential_tools=tools,
        extracted_entities=entities or {},
    )


class TestConfirmationDisclosure:
    def test_credit_purchase_discloses_supplier_creation(self):
        plan = _plan(
            "record_credit_purchase",
            ["search_supplier", "create_supplier", "create_purchase_bill",
             "prepare_journal", "validate_journal", "post_journal"],
            {"amount": 12000.0},
            entity_name="Nexa Traders",
        )
        s = _confirmation_summary(plan).lower()
        assert "nexa traders" in s
        assert "supplier" in s and "create" in s
        assert "12,000" in s
        assert "payable" in s

    def test_cash_purchase_does_not_claim_party_creation(self):
        plan = _plan(
            "record_cash_purchase",
            ["search_account", "create_expense", "prepare_journal"],
            {"amount": 9000.0},
            entity_name="Star Traders",
        )
        s = _confirmation_summary(plan).lower()
        # No PARTY creation may be disclosed for a cash transaction…
        assert "supplier" not in s
        assert "customer" not in s
        # …but the document that WILL be created must be disclosed.
        assert "expense record" in s
        assert "9,000" in s

    def test_new_account_creation_disclosed(self):
        plan = _plan(
            "record_expense",
            ["search_account", "create_account", "create_expense"],
            {"amount": 15000.0},
        )
        s = _confirmation_summary(plan).lower()
        assert "chart-of-accounts account" in s
        assert "15,000" in s


class TestCashPartyCreationGuard:
    @pytest.mark.parametrize("tool", ["create_supplier", "create_customer"])
    def test_blocked_for_cash_purchase(self, tool):
        assert cash_party_creation_blocked("record_cash_purchase", tool) is True

    @pytest.mark.parametrize("tool", ["create_supplier", "create_customer"])
    def test_blocked_for_cash_sale(self, tool):
        assert cash_party_creation_blocked("record_cash_sale", tool) is True

    @pytest.mark.parametrize("intent", [
        "record_credit_purchase", "record_credit_sale",
        "create_supplier", "report_trial_balance",
    ])
    def test_allowed_for_credit_and_explicit_intents(self, intent):
        assert cash_party_creation_blocked(intent, "create_supplier") is False
        assert cash_party_creation_blocked(intent, "create_customer") is False

    def test_read_only_tools_never_blocked(self):
        assert cash_party_creation_blocked(
            "record_cash_purchase", "search_supplier") is False
