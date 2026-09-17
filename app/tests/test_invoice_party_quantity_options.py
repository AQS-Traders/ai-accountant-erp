"""Regression tests — "create an invoice for selling 2 ovens to lkj pvt limited".

Three defects were reported against that single request and are pinned here:

1. PARTY  — the customer was extracted as "selling" instead of
   "lkj pvt limited".  ``_NAME_STOP``'s ``\\s+\\d`` lookahead ended the name at
   the quantity following the verb, and no rule rejected a verb as a name.

2. QUANTITY — the agent asked for a quantity that was already in the request
   ("2 ovens").  ``item_quantity`` was only ever populated from a
   clarification ANSWER; the count in the original phrasing was discarded.

3. CRASH — the run died with
   "2 validation errors for AgentResponse options.0 ... Input should be a
   valid string [type=string_type, input_value={'value': 'yes', ...}]"
   because the revenue-ledger gate emitted ``{value,label}`` DICTS into
   ``AgentResponse.options``, which is ``List[str]``.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.agent import _revenue_ledger_review_gate
from app.models.schemas import AgentResponse, ExecutionStatus
from app.planner import _extract_item_quantity, plan
from app.services import revenue_ledger_service as rls

ORG = uuid.uuid4()
REQUEST = "create an invoice for selling 2 ovens to lkj pvt limited"


class TestPartyExtraction:
    """Defect 1 — the party must be the party, never the verb."""

    def test_customer_is_the_named_party_not_the_verb(self):
        p = plan(REQUEST)
        assert p.intent == "create_invoice"
        assert p.extracted_entities.get("customer_name") == "lkj pvt limited"

    def test_verb_never_becomes_a_party(self):
        p = plan(REQUEST)
        assert p.extracted_entities.get("customer_name") != "selling"

    @pytest.mark.parametrize(
        "message",
        [
            "create an invoice for selling 2 ovens to lkj pvt limited",
            "create invoice for selling ovens to lkj pvt limited",
            "sold 2 ovens to lkj pvt limited",
        ],
    )
    def test_party_after_to_in_every_shape(self, message):
        p = plan(message)
        assert p.extracted_entities.get("customer_name") == "lkj pvt limited"

    def test_existing_invoice_phrasings_still_work(self):
        """The new "invoice ... to <party>" rule must not break older shapes."""
        assert (
            plan("create invoice for abc tech for 20000")
            .extracted_entities.get("customer_name") == "abc tech"
        )
        assert (
            plan("sold chairs to ABC Traders")
            .extracted_entities.get("customer_name") == "ABC Traders"
        )
        assert (
            plan("customer John Smith")
            .extracted_entities.get("customer_name") == "John Smith"
        )

    def test_supplier_extraction_is_unaffected(self):
        p = plan("bought a laptop from ABC Computers on credit")
        assert p.extracted_entities.get("supplier_name") == "ABC Computers"

    def test_trailing_verb_is_never_a_party(self):
        """"invoice ... to be paid" must not yield the customer "be paid"."""
        p = plan("create an invoice for abc tech to be paid")
        assert p.extracted_entities.get("customer_name") != "be paid"


class TestItemQuantity:
    """Defect 2 — a quantity stated in the request is used, not re-asked."""

    def test_count_is_read_from_the_phrasing(self):
        assert _extract_item_quantity(REQUEST, "ovens") == 2.0

    def test_plan_populates_item_quantity(self):
        ents = plan(REQUEST).extracted_entities
        assert ents.get("item_description") == "ovens"
        assert ents.get("item_quantity") == 2.0

    def test_unrelated_count_is_ignored(self):
        """A number that precedes a DIFFERENT noun is not this line's qty."""
        assert _extract_item_quantity(REQUEST, "chairs") is None

    def test_explicit_unit_token(self):
        assert _extract_item_quantity("sold 5 pcs chairs", "chairs") == 5.0
        assert _extract_item_quantity("sold 5 units chair", "chair") == 5.0


class _FakePlan:
    def __init__(self, intent, entities):
        self.intent = intent
        self.extracted_entities = entities


class TestRevenueLedgerOptionsAreStrings:
    """Defect 3 — the gate must emit List[str], not {value,label} dicts."""

    @pytest.fixture
    def review(self, monkeypatch):
        async def _needs_review(organization_id, item, decision):
            return True

        async def _find_general_revenue(organization_id):
            return None  # falls back to rls.REVENUE_PARENT_NAME

        monkeypatch.setattr(rls, "needs_review", _needs_review)
        monkeypatch.setattr(rls, "find_general_revenue", _find_general_revenue)

    async def _gate(self):
        return await _revenue_ledger_review_gate(
            organization_id=ORG,
            execution_plan=_FakePlan(
                "create_invoice", {"item_description": "ovens"}
            ),
        )

    @pytest.mark.asyncio
    async def test_options_are_plain_strings(self, review):
        gate = await self._gate()
        assert gate is not None, "gate should ask about the new revenue stream"
        assert gate["options"], "the gate must offer an answer chip"
        assert all(isinstance(o, str) for o in gate["options"]), (
            "AgentResponse.options is List[str]; dicts crashed the session"
        )

    @pytest.mark.asyncio
    async def test_agent_response_accepts_the_gate_payload(self, review):
        """The exact construction that raised before the fix."""
        gate = await self._gate()
        response = AgentResponse(  # must not raise ValidationError
            status=ExecutionStatus.AWAITING_CLARIFICATION,
            question=gate["question"],
            options=gate.get("options"),
            required_information=["revenue_ledger_decision"],
            requires_user_input=True,
        )
        assert response.options is not None
        assert len(response.options) == 2

    def test_dict_options_would_still_be_rejected(self):
        """Guards the invariant itself, so a dict can never slip back in."""
        with pytest.raises(ValidationError):
            AgentResponse(
                status=ExecutionStatus.AWAITING_CLARIFICATION,
                options=[{"value": "yes", "label": "Create"}],
            )

    @pytest.mark.asyncio
    async def test_labels_route_to_the_right_decision(self, review):
        """The chip text must still parse into CREATE / USE_EXISTING."""
        gate = await self._gate()
        create_chip = gate["options"][0]
        assert create_chip.lower().startswith(("yes", "create", "ok", "y"))
        use_chip = gate["options"][1]
        assert not use_chip.lower().startswith(("yes", "create", "ok", "y"))
