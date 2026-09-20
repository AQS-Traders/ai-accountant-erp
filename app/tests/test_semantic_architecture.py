"""Work Stream S2 — the AI-native semantic contract + pipeline tests.

These tests prove the ARCHITECTURAL contract documented in
``docs/SEMANTIC_ARCHITECTURE.md``:

* four explicit information states (EXPLICIT / SAFELY_INFERRED / AMBIGUOUS /
  MISSING) with the required behaviour for each;
* the rulebook (the agent's reasoning brain) and the bounded,
  organization-scoped context package actually reach the LLM;
* ambiguity is grounded against REAL records and drives a choice question;
* a resolved fact can never be reported missing or re-asked;
* clarification answers merge into the established facts without loss;
* the exact original failure is a permanent regression test;
* the deterministic accounting/security path is never bypassed.

The LLM is simulated with a fake orchestrator that returns the JSON the
prompt asks for — that is deliberate: these tests pin the CONTRACT (grounding,
states, questions, precedence). Live-model behaviour is measured separately
in ``app/semantic_eval.py`` and ``test_semantic_live_eval.py``.
"""

from datetime import date

import pytest

from app.semantic_contract import (
    AMBIGUOUS,
    EXPLICIT,
    MISSING,
    SAFELY_INFERRED,
    SemanticIntent,
    canonical_field,
    information_state_summary,
    normalize_state,
)
from app.semantic_context import (
    SemanticContext,
    distill_rulebook,
    extract_candidate_terms,
)
from app.semantic_layer import (
    extract_semantic_intent,
    ground_semantic_intent,
    intent_for_activity,
    normalize_semantic_intent,
    semantic_field_to_planner_field,
    semantic_questions,
)

TODAY = date(2026, 9, 19)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_schema(
    *,
    activities=("sale",),
    party=None,
    role="customer",
    item="chairs",
    quantity=2,
    amount=23000,
    terms="CREDIT",
    txn_date="2026-09-18",
    nature="GOODS",
) -> dict:
    """The S2 ``facts[]`` payload the prompt asks the model to produce."""
    facts = []
    if party:
        facts.append(
            {"name": "party_name", "value": party, "role": role, "state": "EXPLICIT"}
        )
    if item:
        facts.append({"name": "item_description", "value": item, "state": "EXPLICIT"})
    if quantity is not None:
        facts.append({"name": "quantity", "value": quantity, "state": "EXPLICIT"})
    if amount is not None:
        facts.append({"name": "amount", "value": amount, "state": "EXPLICIT"})
    if terms:
        facts.append(
            {
                "name": "payment_terms",
                "value": terms,
                "state": "SAFELY_INFERRED",
                "evidence": "\"they'll pay later\"",
            }
        )
    if txn_date:
        facts.append(
            {
                "name": "transaction_date",
                "value": txn_date,
                "state": "SAFELY_INFERRED",
                "evidence": "\"yesterday\"",
            }
        )
    if nature:
        facts.append(
            {
                "name": "transaction_nature",
                "value": nature,
                "state": "SAFELY_INFERRED",
                "evidence": "chairs are stock goods",
            }
        )
    return {"activities": list(activities), "facts": facts}


class _FakeOrchestrator:
    def __init__(self, reply: str = "", delay: float = 0.0, fail: bool = False):
        self.reply = reply
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.prompts: list = []

    async def generate_text(self, *, prompt: str, context=None) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider down")
        return self.reply

    def prompt_text(self) -> str:
        return "\n".join(self.prompts)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "semantic_llm_enabled", True, raising=False)
    monkeypatch.setattr(settings, "entity_llm_fallback", None, raising=False)


def _ctx(**kwargs) -> SemanticContext:
    kwargs.setdefault("today", TODAY.isoformat())
    return SemanticContext(**kwargs)


# ---------------------------------------------------------------------------
# A. The rulebook reaches the LLM (how to think, not just what to extract)
# ---------------------------------------------------------------------------


class TestRulebook:
    def test_rulebook_carries_the_reasoning_laws(self):
        rule = distill_rulebook("")
        for law in (
            "UNDERSTAND MEANING, NOT WORDS",
            "GROUND EVERY COPIED VALUE",
            "MONEY DIRECTION DECIDES THE PARTY ROLE",
            "FOUR INFORMATION STATES",
            "AMBIGUOUS IS NOT MISSING",
            "NEVER ASK WHAT IS ALREADY KNOWN",
            "QUESTIONS ARE GENERATED, NOT TEMPLATED",
            "NEVER INVENT IDS",
        ):
            assert law in rule, law

    def test_constitution_governance_is_included_and_bounded(self):
        constitution = (
            "## 3. Deterministic vs AI Responsibilities\nAI interprets; "
            "backend validates.\n\n## 99. Irrelevant\nskip me\n"
        )
        rule = distill_rulebook(constitution)
        assert "GOVERNANCE EXTRACT" in rule
        assert "AI interprets; backend validates." in rule
        assert "skip me" not in rule

    @pytest.mark.asyncio
    async def test_rulebook_is_sent_to_the_provider(self):
        orch = _FakeOrchestrator(reply='{"activities": ["sale"], "facts": []}')
        await extract_semantic_intent(
            "sold two chairs to HJK for 23k",
            orchestrator=orch,
            context=_ctx(rulebook=distill_rulebook("")),
            today=TODAY,
        )
        sent = orch.prompt_text()
        assert "DOMAIN RULEBOOK" in sent
        assert "UNDERSTAND MEANING, NOT WORDS" in sent
        assert "RESPONSE FORMAT" in sent


# ---------------------------------------------------------------------------
# B. The context package is bounded, org-scoped and traceable
# ---------------------------------------------------------------------------


class TestContextPackage:
    def test_candidate_terms_come_from_the_users_own_words(self):
        terms = extract_candidate_terms(
            "hjk pvt limited bought two chairs from us for 23,000 on credit"
        )
        joined = " | ".join(terms).lower()
        assert "hjk" in joined
        assert "chairs" in joined
        assert not any(t.lower() in ("from", "for", "on") for t in terms)

    def test_terms_are_bounded(self):
        terms = extract_candidate_terms(
            " ".join(f"Vendor{i} Traders" for i in range(40))
        )
        assert len(terms) <= 8

    def test_prompt_block_includes_context_sections(self):
        ctx = _ctx(
            organization={"name": "Acme Traders", "base_currency": "PKR"},
            candidates={"customers": [{"name": "HJK Pvt Limited", "id": "abc"}]},
            capabilities=["record_credit_sale", "create_invoice"],
            prior_answers=[{"question": "Which customer?", "answer": "HJK"}],
            prior_facts={"amount": 23000},
            session_state={
                "original_request": "sold chairs",
                "financial_year": "FY2026",
                "open_period": "Sep-2026",
            },
            rulebook="DOMAIN RULEBOOK",
        )
        block = ctx.as_prompt_block()
        assert "Organization: name=Acme Traders; currency=PKR" in block
        assert "HJK Pvt Limited" in block
        assert "record_credit_sale" in block
        assert "PREVIOUS USER ANSWERS" in block
        assert "ALREADY ESTABLISHED FACTS" in block
        assert "Original request" in block
        assert "DOMAIN RULEBOOK" in block

    def test_no_database_ids_are_offered_to_the_model(self):
        ctx = _ctx(
            candidates={"customers": [{"name": "HJK Pvt Limited", "id": "uuid-123"}]}
        )
        # The rendered prompt surfaces NAMES (and codes), never raw ids — the
        # model cannot echo an id it was never shown.
        assert "uuid-123" not in ctx.as_prompt_block()


# ---------------------------------------------------------------------------
# C. Grounding into the contract + information states
# ---------------------------------------------------------------------------


class TestGroundingToContract:
    def test_explicit_and_inferred_states_are_recorded(self):
        parsed = _new_schema(party="hjk pvt limited")
        msg = (
            "hjk pvt limited bought two chairs from us for 23,000 on credit yesterday"
        )
        intent = ground_semantic_intent(parsed, msg, TODAY)
        assert intent.fact("item_description").state == EXPLICIT
        assert intent.fact("quantity").state == EXPLICIT
        assert intent.fact("transaction_date").state == SAFELY_INFERRED
        assert intent.fact("transaction_date").evidence
        assert intent.value("transaction_date") == "2026-09-18"

    def test_party_verbatim_and_role_from_money_direction(self):
        parsed = _new_schema(party="hjk pvt limited", role=None)
        msg = "hjk pvt limited bought two chairs from us for 23,000 on credit"
        intent = ground_semantic_intent(parsed, msg, TODAY)
        assert intent.value("party_name") == "hjk pvt limited"
        assert intent.value("party_role") == "customer"

    def test_purchase_names_a_supplier(self):
        parsed = _new_schema(
            activities=("purchase",), party="Acme Corp", role=None, terms="CREDIT"
        )
        intent = ground_semantic_intent(parsed, "bought chairs from Acme Corp", TODAY)
        assert intent.value("party_role") == "supplier"

    def test_hallucinated_party_is_dropped(self):
        parsed = _new_schema(party="Karachi Traders Ltd")
        intent = ground_semantic_intent(parsed, "sold chairs to Bilal", TODAY)
        assert intent.value("party_name") is None

    def test_untraceable_number_is_dropped(self):
        parsed = _new_schema(amount=99999)
        intent = ground_semantic_intent(parsed, "sold two chairs for 23000", TODAY)
        assert intent.value("amount") is None

    def test_untraceable_date_is_dropped(self):
        parsed = _new_schema(txn_date="2030-01-01")
        intent = ground_semantic_intent(parsed, "sold chairs to Bilal", TODAY)
        assert intent.value("transaction_date") is None

    def test_written_numbers_are_traceable(self):
        parsed = _new_schema(quantity=2, amount=23000)
        intent = ground_semantic_intent(
            parsed, "sold two chairs for twenty-three thousand", TODAY
        )
        assert intent.value("quantity") == 2
        assert intent.value("amount") == 23000

    def test_state_normalization_accepts_models_own_words(self):
        assert normalize_state("stated") == EXPLICIT
        assert normalize_state("derived") == SAFELY_INFERRED
        assert normalize_state("unclear") == AMBIGUOUS
        assert normalize_state("not stated") == MISSING

    def test_alias_names_land_on_one_canonical_fact(self):
        for alias in ("customer_name", "supplier", "party", "vendor"):
            assert canonical_field(alias) == "party_name"
        assert canonical_field("item_quantity") == "quantity"
        assert canonical_field("payment_method") == "payment_terms"

    def test_activity_maps_to_planner_intent(self):
        assert intent_for_activity("sale", "CREDIT") == "record_credit_sale"
        assert intent_for_activity("purchase", "CASH") == "record_cash_purchase"
        assert intent_for_activity("expense", None) == "record_expense"

    def test_state_summary_lists_every_state(self):
        intent = ground_semantic_intent(
            _new_schema(party="hjk pvt limited"),
            "hjk pvt limited bought two chairs for 23,000 on credit",
            TODAY,
        )
        summary = information_state_summary(intent)
        assert set(summary) == {EXPLICIT, SAFELY_INFERRED, AMBIGUOUS, MISSING}
        assert "quantity" in summary[EXPLICIT]
