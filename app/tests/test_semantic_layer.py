"""Work Stream S2 — AI-native semantic understanding layer.

Proves the architectural contract (docs/SEMANTIC_ARCHITECTURE.md):

* semantically DIFFERENT phrasings of the same business act produce
  MATERIALLY EQUIVALENT normalized intent (the exact screenshots failure:
  "record a sale of two chairs on credit for 23000 to hjk pvt limited"
  used to ask who the customer / what item / how many units);
* completely unfamiliar wording (no transaction keywords at all) is
  understood from meaning, not vocabulary;
* grounding: hallucinated values are dropped, traceable ones kept;
* degradation: LLM down/timeout/garbage -> regex-only behaviour unchanged;
* deterministic safeguards: the planner whitelist is the only path a
  semantic intent may take into execution.
"""

from datetime import date

import pytest

from app.semantic_layer import (
    _INTENT_MAP,
    extract_semantic_facts,
    ground_facts,
    normalize_to_erp,
)
from app.planner import plan

TODAY = date(2026, 9, 19)

# The SAME business act, expressed four different ways (the fourth is
# deliberately free of any planner keyword).
_EQUIVALENT_PHRASINGS = [
    ("record a sale of two chairs on credit for 23000 to hjk pvt limited",
     {"activity": "sale", "party": {"name": "hjk pvt limited", "role": "customer"},
      "item": {"description": "chairs"}, "quantity": 2, "amount": 23000,
      "payment_terms": "CREDIT", "transaction_nature": "GOODS",
      "status": {"quantity": "SAFELY_INFERRED"},
      "evidence": {"quantity": "written 'two'"}}),
    ("hjk pvt limited bought two chairs from us for 23,000 on credit yesterday",
     {"activity": "sale", "party": {"name": "hjk pvt limited", "role": "customer"},
      "item": {"description": "chairs"}, "quantity": 2, "amount": 23000,
      "payment_terms": "CREDIT", "transaction_date": "2026-09-18",
      "transaction_nature": "GOODS",
      "status": {"transaction_date": "SAFELY_INFERRED",
                 "transaction_nature": "SAFELY_INFERRED"},
      "evidence": {"transaction_date": "relative 'yesterday'"}}),
    ("We supplied 2 chairs to HJK for 23k and they'll pay later.",
     {"activity": "sale", "party": {"name": "HJK", "role": "customer"},
      "item": {"description": "chairs"}, "quantity": 2, "amount": 23000,
      "payment_terms": "CREDIT", "transaction_nature": "GOODS",
      "status": {"payment_terms": "SAFELY_INFERRED", "amount": "SAFELY_INFERRED"},
      "evidence": {"payment_terms": "'they'll pay later'",
                   "amount": "'23k' = 23 x 1000"}}),
    ("hjk pvt limited took delivery of a pair of chairs valued at 23000; "
     "settlement is deferred.",
     {"activity": "sale", "party": {"name": "hjk pvt limited", "role": "customer"},
      "item": {"description": "chairs"}, "quantity": 2, "amount": 23000,
      "payment_terms": "CREDIT", "transaction_nature": "GOODS",
      "status": {"activity": "SAFELY_INFERRED", "quantity": "SAFELY_INFERRED",
                 "payment_terms": "SAFELY_INFERRED"},
      "evidence": {"quantity": "'a pair of' = 2",
                   "payment_terms": "'settlement is deferred'"}}),
]

_EXPECTED_INTENT = "record_credit_sale"


class _FakeOrchestrator:
    def __init__(self, reply: str = "", delay: float = 0.0, fail: bool = False):
        self.reply = reply
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def generate_text(self, *, prompt: str, context=None) -> str:
        self.calls += 1
        if self.delay:
            import asyncio
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


# ---------------------------------------------------------------------------
# Equivalence: different wording -> same business intent
# ---------------------------------------------------------------------------

class TestSemanticEquivalence:
    def test_all_phrasings_normalize_to_the_same_intent(self):
        for msg, llm_json in _EQUIVALENT_PHRASINGS:
            prefill = normalize_to_erp(ground_facts(llm_json, msg, TODAY))
            assert prefill["semantic_intent"] == _EXPECTED_INTENT, msg
            assert prefill["customer_name"], msg
            assert prefill["item_description"] == "chairs", msg
            assert prefill["item_quantity"] == 2, msg
            assert prefill["amount"] == 23000, msg
            assert prefill["payment_method"] == "CREDIT", msg

    def test_questionnaires_collapse_to_genuine_gaps_only(self):
        # THE screenshot failure: five questions for a fully-stated request.
        for msg, llm_json in _EQUIVALENT_PHRASINGS:
            prefill = normalize_to_erp(ground_facts(llm_json, msg, TODAY))
            p = plan(msg, prefill_entities=prefill)
            forbidden = [
                q for q in p.clarification_questions
                if "customer" in q.lower() or "item or service" in q.lower()
                or "units are you invoicing" in q.lower()
                or "nature of this sale" in q.lower()
            ]
            assert not forbidden, (msg, forbidden)
            if "yesterday" in msg:
                assert not p.clarification_questions, msg
            else:
                assert len(p.clarification_questions) == 1, msg
                assert "date" in p.clarification_questions[0].lower()

    def test_inference_status_and_evidence_are_preserved(self):
        msg, llm_json = _EQUIVALENT_PHRASINGS[2]  # "23k", "they'll pay later"
        facts = ground_facts(llm_json, msg, TODAY)
        assert facts["payment_terms"]["status"] == "SAFELY_INFERRED"
        assert "pay later" in facts["payment_terms"]["evidence"]
        assert facts["amount"]["status"] == "SAFELY_INFERRED"

    def test_written_numbers_ground_to_digits(self):
        msg, llm_json = _EQUIVALENT_PHRASINGS[0]  # "two chairs"
        facts = ground_facts(llm_json, msg, TODAY)
        assert facts["quantity"]["value"] == 2
        assert facts["quantity"]["evidence"] == "written 'two'"

    def test_relative_date_resolves_with_today(self):
        msg, llm_json = _EQUIVALENT_PHRASINGS[1]  # "yesterday"
        facts = ground_facts(llm_json, msg, TODAY)
        assert facts["transaction_date"]["value"] == "2026-09-18"
        assert facts["transaction_date"]["status"] == "SAFELY_INFERRED"
        p = plan(msg, prefill_entities=normalize_to_erp(facts))
        assert not p.clarification_questions

    def test_pair_means_two(self):
        msg, llm_json = _EQUIVALENT_PHRASINGS[3]  # "a pair of chairs"
        facts = ground_facts(llm_json, msg, TODAY)
        assert facts["quantity"]["value"] == 2


# ---------------------------------------------------------------------------
# Grounding: hallucination is dropped, never repaired
# ---------------------------------------------------------------------------

class TestGrounding:
    def test_hallucinated_party_is_dropped(self):
        facts = ground_facts(
            {"activity": "sale", "party": {"name": "Karachi Traders Ltd",
                                           "role": "customer"}},
            "sold chairs to Bilal", TODAY,
        )
        assert "party" not in facts

    def test_untraceable_number_is_dropped(self):
        facts = ground_facts(
            {"activity": "sale", "amount": 99999},
            "sold chairs to Bilal for 500", TODAY,
        )
        assert "amount" not in facts

    def test_untraceable_date_is_dropped(self):
        facts = ground_facts(
            {"activity": "sale", "transaction_date": "2030-01-01"},
            "sold chairs to Bilal", TODAY,
        )
        assert "transaction_date" not in facts

    def test_inconsistent_party_role_is_dropped(self):
        # role=customer for a purchase — money-out vs role contradiction:
        # treated as ambiguous, never guessed.
        facts = ground_facts(
            {"activity": "purchase",
             "party": {"name": "Acme Corp", "role": "customer"}},
            "bought chairs from Acme Corp", TODAY,
        )
        assert "party" not in facts

    def test_nature_must_be_canonical(self):
        facts = ground_facts(
            {"activity": "sale", "transaction_nature": "MAGIC"},
            "sold chairs to Bilal", TODAY,
        )
        assert "transaction_nature" not in facts

    def test_activity_must_be_known(self):
        facts = ground_facts(
            {"activity": "teleportation"}, "sold chairs to Bilal", TODAY,
        )
        assert "activity" not in facts

    def test_unknown_payment_terms_dropped(self):
        facts = ground_facts(
            {"activity": "sale", "payment_terms": "BARter"},
            "sold chairs to Bilal", TODAY,
        )
        assert "payment_terms" not in facts


# ---------------------------------------------------------------------------
# Deterministic intent whitelist (LLM cannot invent an intent)
# ---------------------------------------------------------------------------

class TestIntentSafeguard:
    def test_semantic_intent_outside_whitelist_is_ignored(self):
        # An LLM value outside the planner's whitelist must not reach
        # execution: the keyword fallback decides instead.
        p = plan(
            "sold chairs to Bilal",
            prefill_entities={"semantic_intent": "delete_all_ledgers"},
        )
        assert p.intent != "delete_all_ledgers"

    def test_unknown_intent_names_fall_back_to_regex(self):
        p = plan(
            "sold chairs to Bilal",
            prefill_entities={"semantic_intent": "not_a_real_intent"},
        )
        assert p.intent == "record_sale"

    def test_intent_map_covers_the_core_acts(self):
        assert _INTENT_MAP[("purchase", "CREDIT")] == "record_credit_purchase"
        assert _INTENT_MAP[("expense", None)] == "record_expense"
        assert _INTENT_MAP[("receipt", None)] == "record_receipt"
        assert _INTENT_MAP[("payment", None)] == "record_payment"


# ---------------------------------------------------------------------------
# Degradation: provider failure = exactly the old regex behaviour
# ---------------------------------------------------------------------------

class TestDegradation:
    @pytest.mark.asyncio
    async def test_provider_failure_returns_empty(self):
        orch = _FakeOrchestrator(fail=True)
        assert await extract_semantic_facts("sold chairs", orchestrator=orch) == {}

    @pytest.mark.asyncio
    async def test_garbage_json_returns_empty(self):
        orch = _FakeOrchestrator(reply="sorry, I cannot")
        assert await extract_semantic_facts("sold chairs", orchestrator=orch) == {}

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "entity_llm_timeout_seconds", 0.05, raising=False
        )
        orch = _FakeOrchestrator(reply="{}", delay=5.0)
        assert await extract_semantic_facts("sold chairs", orchestrator=orch) == {}

    @pytest.mark.asyncio
    async def test_no_orchestrator_returns_empty(self):
        assert await extract_semantic_facts("sold chairs", orchestrator=None) == {}

    @pytest.mark.asyncio
    async def test_disabled_flag_skips_provider(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "entity_llm_fallback", False, raising=False
        )
        orch = _FakeOrchestrator(reply="{}")
        assert await extract_semantic_facts("sold chairs", orchestrator=orch) == {}
        assert orch.calls == 0
