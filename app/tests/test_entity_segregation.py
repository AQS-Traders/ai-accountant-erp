"""Tests for the grounded LLM entity-segregation stage (Work Stream S1).

Contract under test:
* grounded values fill regex gaps; regex captures are never overwritten;
* any value not present verbatim in the user's text is rejected;
* numbers must parse positive; generic nouns are rejected as items;
* provider failure / timeout / garbage JSON degrade to exactly {} â€”
  the stage can never break a request;
* disabled flag short-circuits without touching the provider.
"""

import asyncio

import pytest

from app import entity_segregation as es
from app.planner import plan


class _FakeOrchestrator:
    """Minimal stand-in exposing only generate_text(prompt=...)."""

    def __init__(self, reply: str = "", delay: float = 0.0, fail: bool = False):
        self.reply = reply
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def generate_text(self, *, prompt: str, context=None) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider down")
        return self.reply


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "entity_llm_fallback", True, raising=False
    )


def _json(**fields) -> str:
    import json

    return json.dumps(fields)


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

class TestGrounding:
    @pytest.mark.asyncio
    async def test_grounded_party_fills_gap(self):
        orch = _FakeOrchestrator(
            _json(customer_name="Bilal Electronics",
                  item_description="voltage stabilizers")
        )
        out = await es.segregate_entities(
            "make an invoice out for Bilal Electronics covering 4 voltage "
            "stabilizers, they'll pay later",
            orchestrator=orch,
        )
        assert out["customer_name"] == "Bilal Electronics"
        assert out["item_description"] == "voltage stabilizers"

    @pytest.mark.asyncio
    async def test_hallucinated_party_is_dropped(self):
        orch = _FakeOrchestrator(_json(customer_name="Karachi Traders Ltd"))
        out = await es.segregate_entities(
            "make an invoice out for Bilal Electronics covering 4 voltage "
            "stabilizers",
            orchestrator=orch,
        )
        assert "customer_name" not in out

    @pytest.mark.asyncio
    async def test_generic_item_noun_rejected(self):
        orch = _FakeOrchestrator(_json(item_description="stuff"))
        out = await es.segregate_entities(
            "sold some stuff to Bilal", orchestrator=orch
        )
        assert "item_description" not in out

    @pytest.mark.asyncio
    async def test_wrapped_and_prefix_cleaned_but_still_grounded(self):
        orch = _FakeOrchestrator(_json(customer_name="'Bilal Electronics'"))
        out = await es.segregate_entities(
            "invoice to Bilal Electronics for 2 fans", orchestrator=orch
        )
        assert out["customer_name"] == "Bilal Electronics"

    @pytest.mark.asyncio
    async def test_numbers_must_be_positive(self):
        orch = _FakeOrchestrator(_json(amount="0", item_quantity="-3"))
        out = await es.segregate_entities(
            "sold 4 stabilizers to Bilal", orchestrator=orch
        )
        assert out == {}

    @pytest.mark.asyncio
    async def test_unknown_fields_discarded(self):
        orch = _FakeOrchestrator(_json(transaction_date="2030-01-01", ok="yes"))
        out = await es.segregate_entities(
            "sold 4 stabilizers to Bilal", orchestrator=orch
        )
        assert out == {}


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

class TestDegradation:
    @pytest.mark.asyncio
    async def test_provider_failure_returns_empty(self):
        orch = _FakeOrchestrator(fail=True)
        out = await es.segregate_entities("sold goods to Bilal", orchestrator=orch)
        assert out == {}

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "entity_llm_timeout_seconds", 0.05, raising=False
        )
        orch = _FakeOrchestrator(reply="{}", delay=5.0)
        out = await es.segregate_entities("sold goods to Bilal", orchestrator=orch)
        assert out == {}

    @pytest.mark.asyncio
    async def test_garbage_json_returns_empty(self):
        orch = _FakeOrchestrator(reply="I cannot help with that.")
        out = await es.segregate_entities("sold goods to Bilal", orchestrator=orch)
        assert out == {}

    @pytest.mark.asyncio
    async def test_no_orchestrator_returns_empty_without_call(self):
        out = await es.segregate_entities("sold goods to Bilal", orchestrator=None)
        assert out == {}

    @pytest.mark.asyncio
    async def test_disabled_flag_skips_provider(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "entity_llm_fallback", False, raising=False
        )
        orch = _FakeOrchestrator(_json(customer_name="Bilal Electronics"))
        out = await es.segregate_entities(
            "invoice to Bilal Electronics", orchestrator=orch
        )
        assert out == {}
        assert orch.calls == 0

    @pytest.mark.asyncio
    async def test_empty_message_returns_empty(self):
        orch = _FakeOrchestrator(_json(customer_name="Bilal"))
        out = await es.segregate_entities("", orchestrator=orch)
        assert out == {}
        assert orch.calls == 0


# ---------------------------------------------------------------------------
# Planner integration (prefill semantics)
# ---------------------------------------------------------------------------

class TestPlannerPrefill:
    def test_prefill_fills_gap_without_overwriting_regex(self):
        # Regex DOES capture the customer here ("invoice ... to X").
        base = plan("create an invoice for 2 ovens to lkj pvt limited for 40000")
        assert base.extracted_entities.get("customer_name") == "lkj pvt limited"

        # A prefill must NOT win over a regex capture.
        refined = plan(
            "create an invoice for 2 ovens to lkj pvt limited for 40000",
            prefill_entities={"customer_name": "WRONG", "item_description": "ovens"},
        )
        assert refined.extracted_entities["customer_name"] == "lkj pvt limited"
        assert refined.extracted_entities["item_description"] == "ovens"

    def test_prefill_quantity_fills_both_aliases(self):
        p = plan(
            "sold chairs to Bilal",
            prefill_entities={"item_quantity": 5.0, "item_description": "chairs"},
        )
        assert p.extracted_entities["item_quantity"] == 5.0
        assert p.extracted_entities["quantity"] == 5.0

    def test_clarification_answer_beats_prefill(self):
        # Merge-order proof: _merge_clarification_answers runs BEFORE the
        # prefill block, so an ANSWERED value is never clobbered by the
        # grounded LLM stage. "credit" resolves payment_method=CREDIT; the
        # prefill's CASH must lose.
        p = plan(
            "sold goods to Bilal",
            clarification_history=[
                {"question": "Cash or credit?", "answer": "credit"}
            ],
            prefill_entities={"payment_method": "CASH"},
        )
        assert p.extracted_entities["payment_method"] == "CREDIT"

    def test_prefill_cannot_overwrite_regex_customer(self):
        # "sold goods to Bilal" -> regex captures customer_name=Bilal.
        # The prefill's invented customer must lose.
        p = plan(
            "sold goods to Bilal",
            prefill_entities={"customer_name": "Wrong Co"},
        )
        assert p.extracted_entities["customer_name"] == "Bilal"
