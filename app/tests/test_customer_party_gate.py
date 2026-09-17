"""
R4.8 - Customer party gate for sale-side intents (create_invoice,
record_credit_sale, create_quotation).

Before R4.8 the party-resolution gate only knew about SUPPLIERS, so an
answered invoice naming a customer that does not exist yet fell off the
deterministic fast path into the ~70-80s LLM planning round.  Now the
gate asks the standard "Party check" question for the CUSTOMER, the
user's confirmation sets ``customer_create_confirmed`` (the planner
merge already supported it), and the fast path creates the ledger
deterministically - no LLM round-trip.
"""

from __future__ import annotations

import uuid

import pytest

from app.planner import plan

# The consolidated first-round invoice question (markers the planner's
# merge gates on) — same shape the live flow produces.
INVOICE_SALE_Q = (
    "To record this transaction I need a few things:\n"
    "1. What is the nature of this sale: (a) GOODS (a stock item you "
    "sell), (b) SERVICE, (c) OTHER_INCOME, (d) FIXED_ASSET_DISPOSAL?\n"
    "2. Which customer is this invoice for?\n"
    "3. What is the invoice date?"
)


def _invoice_plan(customer: str = "Acme Corp"):
    return plan("create invoice of 450000", clarification_history=[
        {"question": INVOICE_SALE_Q,
         "answer": f"1) a 2) {customer} 3) today"},
    ])


class TestCustomerPartyGate:
    @pytest.mark.asyncio
    async def test_invoice_missing_customer_asks_creation(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import customer_service

        p = _invoice_plan("Acme Corp")
        assert p.extracted_entities.get("customer_name") == "Acme Corp"

        async def fake_search(org_id, *, query, limit=25):
            return []  # the org genuinely has no customers

        monkeypatch.setattr(customer_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is not None
        assert result["question"].startswith("Party check:")
        assert "no customer named" in result["question"]
        assert "Acme Corp" in result["question"]
        assert result["options"][0].startswith("Yes - create")
        assert result["required_fields"] == ["customer_name"]

    @pytest.mark.asyncio
    async def test_invoice_similar_customers_offer_choice(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import customer_service

        p = _invoice_plan("Acme")

        async def fake_search(org_id, *, query, limit=25):
            return [
                {"id": "c1", "name": "Acme Corp Pvt Ltd"},
                {"id": "c2", "name": "Acme Traders"},
            ]

        monkeypatch.setattr(customer_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is not None
        assert "similar customers" in result["question"]
        assert "Acme Corp Pvt Ltd" in result["options"]
        assert any(o.startswith("Create new party") for o in result["options"])
        assert result["required_fields"] == ["customer_name"]

    @pytest.mark.asyncio
    async def test_invoice_exact_customer_proceeds(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import customer_service

        p = _invoice_plan("Acme Corp")

        async def fake_search(org_id, *, query, limit=25):
            return [{"id": "c1", "name": query}]

        monkeypatch.setattr(customer_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is None  # exact match -> straight to the fast path

    @pytest.mark.asyncio
    async def test_invoice_confirmed_creation_never_reasks(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import customer_service

        p = _invoice_plan("Acme Corp")
        p.extracted_entities["customer_create_confirmed"] = True

        async def fake_search(org_id, *, query, limit=25):
            return []

        monkeypatch.setattr(customer_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is None  # confirmed - the fast path creates it

    @pytest.mark.asyncio
    async def test_confirmed_customer_is_created_not_invented(self, monkeypatch):
        """The party gate CREATES the ledger only on explicit confirmation."""
        from app.agent import _settlement_party
        from app.services import customer_service

        created = {}

        async def fake_search(org_id, *, query, limit=25):
            return []  # nothing similar - creation is the only route

        async def fake_create(org_id, *, name):
            created["name"] = name
            return {"id": "c-new", "name": name}

        monkeypatch.setattr(customer_service, "search", fake_search)
        monkeypatch.setattr(customer_service, "create", fake_create)

        party = await _settlement_party(
            uuid.UUID(int=1), kind="customer", name="Acme Corp",
            confirmed=True,
        )
        assert party == {"id": "c-new", "name": "Acme Corp"}
        assert created["name"] == "Acme Corp"

        # Without confirmation the gate refuses - never silently invented.
        refused = await _settlement_party(
            uuid.UUID(int=1), kind="customer", name="Other Corp",
            confirmed=False,
        )
        assert refused is None
        assert created["name"] == "Acme Corp"  # no second create happened


class TestCustomerPartyCheckMerge:
    """The planner merge must map the customer party-check answer onto
    ``customer_create_confirmed`` (R4.5 made the gate party-kind aware)."""

    def test_creation_confirmation_sets_customer_flag(self):
        from app.planner import _merge_clarification_answers

        merged = _merge_clarification_answers(
            {"customer_name": "Acme Corp", "transaction_nature": "GOODS"},
            [{"question": (
                "Party check: no customer named 'Acme Corp' exists yet. "
                "Create 'Acme Corp' as a new customer ledger?"
             ),
              "answer": "Yes - create 'Acme Corp'"}],
        )
        assert merged["customer_create_confirmed"] is True
        assert merged["customer_name"] == "Acme Corp"

    def test_pick_existing_stores_verbatim(self):
        from app.planner import _merge_clarification_answers

        merged = _merge_clarification_answers(
            {"customer_name": "acme"},
            [{"question": (
                "Party check: I found similar customers for 'acme'. Which "
                "one is the real party for this transaction?"
             ),
              "answer": "Acme Corp Pvt Ltd"}],
        )
        assert merged["customer_name"] == "Acme Corp Pvt Ltd"
        assert "customer_create_confirmed" not in merged