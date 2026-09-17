"""Work Stream R2 - IFRS credit-purchase treatment + deterministic fast path.

* A CREDIT purchase must NEVER post a paid-expense/asset journal: it stays
  on the purchase-bill path (Dr account / Cr PARTY PAYABLE, settled later
  by Dr party / Cr bank when payment is made).
* The supplier question offers the one-off LOCAL VENDOR option; the answer
  folds into the standing 'Local Vendor' account (created on first use).
* Fully-resolved credit purchases build their tool call DETERMINISTICALLY
  (no LLM round-trip) - instant execution instead of 30-85s of latency.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.planner import plan
from app.reasoning import NATURE_DECISION_QUESTION

DATE_Q = (
    "What is the transaction date? Reply TODAY, or the date as "
    "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
)
CREDIT_Q = "Was this paid in cash or on credit?"
AMOUNT_Q = "What is the transaction amount?"


class TestCreditPurchaseTreatment:
    def test_credit_expense_stays_on_bill_path(self):
        """CREDIT + general expense: the intent must stay on the purchase-
        bill path (payable) - never re-routed to the PAID expense tool."""
        p = plan("I purchased a charger", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "c"},
            {"question": CREDIT_Q, "answer": "credit"},
            {"question": DATE_Q, "answer": "04/09/2026"},
        ])
        assert p.intent == "record_credit_purchase"
        assert p.extracted_entities["transaction_nature"] == "OPERATING_EXPENSE"
        assert p.extracted_entities["payment_method"] == "CREDIT"

    def test_cash_expense_routes_to_expense_tool(self):
        p = plan("I purchased a charger", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "c"},
            {"question": CREDIT_Q, "answer": "cash"},
            {"question": DATE_Q, "answer": "04/09/2026"},
        ])
        assert p.intent == "record_expense"

    def test_unknown_payment_defers_routing(self):
        """Nature answered but treatment still open: NO re-route (the
        payment treatment decides the destination account)."""
        p = plan("I purchased a charger", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "c"},
        ])
        assert p.intent == "record_purchase"
        assert "payment_type" in p.missing_fields

    def test_credit_fixed_asset_stays_on_bill_path(self):
        """Credit purchase of a durable item: Dr Asset / Cr Payable via the
        bill - never a paid-expense or immediate-capitalisation posting."""
        p = plan("I purchased a table", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "a"},
            {"question": CREDIT_Q, "answer": "credit"},
        ])
        assert p.intent == "record_credit_purchase"


class TestLocalVendorOption:
    def test_supplier_question_offers_local_vendor(self):
        p = plan("I purchased a charger", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "c"},
            {"question": CREDIT_Q, "answer": "credit"},
        ])
        q = next(
            q for q in p.clarification_questions if "supplier" in q.lower()
        )
        assert "LOCAL VENDOR" in q

    def test_local_vendor_answer_folds_into_standing_account(self):
        p = plan("I purchased a charger", clarification_history=[
            {"question": "Who is the supplier?", "answer": "Local Vendor"},
        ])
        assert p.extracted_entities["supplier_name"] == "Local Vendor"

    def test_named_supplier_answer_is_kept_verbatim(self):
        p = plan("I purchased a charger", clarification_history=[
            {"question": "Who is the supplier?", "answer": "ABC Traders"},
        ])
        assert p.extracted_entities["supplier_name"] == "ABC Traders"


class TestDeterministicFastPath:
    def _resolved_plan(self):
        return plan("I purchased a charger", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "c"},
            {"question": CREDIT_Q, "answer": "credit"},
            {"question": "Who is the supplier?", "answer": "Local Vendor"},
            {"question": AMOUNT_Q, "answer": "550"},
            {"question": DATE_Q, "answer": "04/09/2026"},
        ])

    @pytest.mark.asyncio
    async def test_fast_path_builds_bill_call_without_llm(self, monkeypatch):
        from app.agent import _deterministic_mutation_calls
        from app.services import supplier_service

        p = self._resolved_plan()
        assert p.intent == "record_credit_purchase"
        assert p.requires_clarification is False

        classification = SimpleNamespace(
            account_hint_id="acc-123", requires_clarification=False,
        )

        async def fake_search(org_id, *, query, limit=25):
            # EXACT party match exists -> reuse (never create).
            return [{"id": "sup-1", "name": query}]

        monkeypatch.setattr(supplier_service, "search", fake_search)

        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=classification,
        )
        assert calls is not None
        assert calls[0].tool_name == "create_purchase_bill"
        args = calls[0].arguments
        assert args["supplier_id"] == "sup-1"
        assert args["bill_date"] == "2026-09-04"
        assert args["subtotal"] == 550.0
        assert args["account_id"] == "acc-123"

    @pytest.mark.asyncio
    async def test_fast_path_creates_only_after_confirmation(
        self, monkeypatch
    ):
        """No exact party match: without the user's confirmation the call
        is NOT built (the Party check question handles it); with the
        confirmation entity the new party ledger is created."""
        from app.agent import _deterministic_mutation_calls
        from app.services import supplier_service

        p = self._resolved_plan()
        classification = SimpleNamespace(
            account_hint_id="acc-1", requires_clarification=False,
        )
        created = []

        async def fake_search(org_id, *, query, limit=25):
            return []  # nothing similar exists

        async def fake_create(org_id, *, name):
            created.append(name)
            return {"id": "sup-new", "name": name}

        monkeypatch.setattr(supplier_service, "search", fake_search)
        monkeypatch.setattr(supplier_service, "create", fake_create)

        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=classification,
        )
        assert calls is None  # unconfirmed -> the Party check asks first
        assert created == []

        p.extracted_entities["supplier_create_confirmed"] = True
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=classification,
        )
        assert calls is not None
        assert created == ["Local Vendor"]
        assert calls[0].arguments["supplier_id"] == "sup-new"

    @pytest.mark.asyncio
    async def test_fast_path_refuses_incomplete_plan(self):
        from app.agent import _deterministic_mutation_calls

        p = plan("I purchased a charger")  # nothing resolved yet
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None

    @pytest.mark.asyncio
    async def test_fast_path_refuses_other_intents(self):
        from app.agent import _deterministic_mutation_calls

        p = plan("show me the trial balance")
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None

    @pytest.mark.asyncio
    async def test_fast_path_refuses_when_nature_gap_open(self):
        from app.agent import _deterministic_mutation_calls

        p = self._resolved_plan()
        classification = SimpleNamespace(
            account_hint_id=None, requires_clarification=True,
        )
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=classification,
        )
        assert calls is None  # configuration gap - ask, never guess




def _resolved_credit_plan():
    """A fully-resolved credit purchase plan (nature, credit, party,
    amount, date all answered) - shared by the fast-path test classes."""
    return plan("I purchased a charger", clarification_history=[
        {"question": NATURE_DECISION_QUESTION, "answer": "c"},
        {"question": CREDIT_Q, "answer": "credit"},
        {"question": "Who is the supplier?", "answer": "Local Vendor"},
        {"question": AMOUNT_Q, "answer": "550"},
        {"question": DATE_Q, "answer": "04/09/2026"},
    ])


class TestInlinePartyAnswer:
    # Reuse the shared fully-resolved plan helper.
    _resolved_plan = staticmethod(_resolved_credit_plan)

    """The UI's conditional party box: choosing CREDIT on the payment
    question reveals a party box + Local Vendor toggle; the composed
    answer carries a TRAILING "N) supplier: X" part that must map back."""

    def test_trailing_supplier_part_maps_to_supplier_field(self):
        from app.planner import _explode_multi_answers

        pairs = _explode_multi_answers([{
            "question": (
                "To record this transaction I need a few things:\n"
                "1. What is the transaction amount?\n"
                "2. What is the transaction date?"
            ),
            "answer": "1) 550\n2) TODAY\n3) supplier: Local Vendor",
        }])
        assert len(pairs) == 3
        assert pairs[2] == {
            "question": "Who is the supplier?",
            "answer": "Local Vendor",
        }

    def test_party_check_pick_stores_verbatim(self):
        from app.planner import _merge_clarification_answers

        merged = _merge_clarification_answers(
            {"supplier_name": "abc"},  # fuzzy earlier value
            [{"question": (
                "Party check: I found similar suppliers for 'abc'. Which "
                "one is the real party?"
             ),
              "answer": "ABC Traders"}],
        )
        assert merged["supplier_name"] == "ABC Traders"

    def test_party_check_creation_confirmation(self):
        from app.planner import _merge_clarification_answers

        merged = _merge_clarification_answers(
            {"supplier_name": "Local Vendor"},
            [{"question": (
                "Party check: no supplier named 'Local Vendor' exists yet. "
                "Create 'Local Vendor' as a new party ledger?"
             ),
              "answer": "Yes - create 'Local Vendor'"}],
        )
        assert merged["supplier_create_confirmed"] is True

    @pytest.mark.asyncio
    async def test_party_resolution_exact_match_proceeds(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import supplier_service

        p = self._resolved_plan()

        async def fake_search(org_id, *, query, limit=25):
            return [{"id": "s1", "name": query}]

        monkeypatch.setattr(supplier_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is None  # exact match -> proceed to the tool call

    @pytest.mark.asyncio
    async def test_party_resolution_similar_asks_choice(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import supplier_service

        p = self._resolved_plan()

        async def fake_search(org_id, *, query, limit=25):
            return [
                {"id": "s1", "name": "Local Vendors Pvt Ltd"},
                {"id": "s2", "name": "Local Vendor Store"},
            ]

        monkeypatch.setattr(supplier_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is not None
        assert result["question"].startswith("Party check:")
        assert "Local Vendors Pvt Ltd" in result["options"]
        assert any(o.startswith("Create new party") for o in result["options"])

    @pytest.mark.asyncio
    async def test_party_resolution_none_asks_creation(self, monkeypatch):
        from app.agent import _party_resolution_question
        from app.services import supplier_service

        p = self._resolved_plan()

        async def fake_search(org_id, *, query, limit=25):
            return []

        monkeypatch.setattr(supplier_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is not None
        assert "no supplier named" in result["question"]
        assert "Yes - create" in result["options"][0]

    @pytest.mark.asyncio
    async def test_party_resolution_confirmed_never_reasks(
        self, monkeypatch
    ):
        from app.agent import _party_resolution_question
        from app.services import supplier_service

        p = self._resolved_plan()
        p.extracted_entities["supplier_create_confirmed"] = True

        async def fake_search(org_id, *, query, limit=25):
            return []

        monkeypatch.setattr(supplier_service, "search", fake_search)
        result = await _party_resolution_question(
            organization_id=uuid.UUID(int=1), execution_plan=p,
        )
        assert result is None  # already confirmed - proceed and create
