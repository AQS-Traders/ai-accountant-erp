"""Work Stream R3.2 — capitalization disambiguation (IAS 16 / IAS 38).

The capitalization question fires ONLY when the purpose answer creates
genuine ambiguity (repairs, software, durable free text) AND the amount
is at or above the org-set capitalization threshold — small amounts
auto-expense without asking.  Once answered, the decision is learned
per purpose so recurring items stop re-asking, and a CAPITALIZE answer
routes through the fixed-asset path.
"""

import uuid

import pytest

from app.planner import plan
from app.reasoning import (
    CAPITALIZATION_DECISION_QUESTION,
    capitalization_question_for,
    capitalization_threshold_from_prefs,
    options_for_question,
    purpose_question,
    purpose_requires_capitalization_question,
)

PURPOSE_Q = purpose_question()
CAP_Q = capitalization_question_for("REPAIRS_MAINTENANCE")
CAP_Q_SOFTWARE = capitalization_question_for("SOFTWARE_SUBSCRIPTION")


class TestThresholdRule:
    """User decision: purpose-driven, gated by the org threshold —
    small amounts auto-expense, the question is never asked below it."""

    def test_repairs_above_threshold_asks(self):
        assert purpose_requires_capitalization_question(
            "REPAIRS_MAINTENANCE", "building overhaul", 80_000.0, 50_000.0
        ) is True

    def test_repairs_below_threshold_auto_expenses(self):
        assert purpose_requires_capitalization_question(
            "REPAIRS_MAINTENANCE", "door hinge fix", 25_000.0, 50_000.0
        ) is False

    def test_unambiguous_purposes_never_ask(self):
        assert purpose_requires_capitalization_question(
            "RENT", "", 500_000.0, 50_000.0
        ) is False
        assert purpose_requires_capitalization_question(
            "SALARIES", "", 500_000.0, 50_000.0
        ) is False
        # Equipment purchases are already unambiguously capital.
        assert purpose_requires_capitalization_question(
            "EQUIPMENT_PURCHASE", "", 500_000.0, 50_000.0
        ) is False

    def test_org_threshold_preference_is_honoured(self):
        assert capitalization_threshold_from_prefs(
            {"capitalization_threshold": "100,000"}
        ) == 100_000.0
        # Below an org threshold of 100k, an 80k repair auto-expenses.
        assert purpose_requires_capitalization_question(
            "REPAIRS_MAINTENANCE", "overhaul", 80_000.0,
            capitalization_threshold_from_prefs(
                {"capitalization_threshold": "100000"}
            ),
        ) is False
        assert capitalization_threshold_from_prefs({}) == 50_000.0
        assert capitalization_threshold_from_prefs({"capitalization_threshold": "abc"}) == 50_000.0

    def test_software_is_ambiguous_with_intangible_option(self):
        assert "Intangible asset" in CAP_Q_SOFTWARE
        assert purpose_requires_capitalization_question(
            "SOFTWARE_SUBSCRIPTION", "", 200_000.0, 50_000.0
        ) is True


class TestCapitalizationQuestionAppearance:
    """The question appears in the ladder ONLY for the ambiguous set,
    never for rent-like purposes, and only above the threshold."""

    def test_repairs_purpose_triggers_the_question(self):
        p = plan(
            "record an expense of 80000 for building repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question":
                 "Has this expense been paid, or is it outstanding? Reply "
                 "with a, b, c or d: (a) Paid now — cash; (b) Paid now — "
                 "bank / online; (c) Outstanding — invoice or bill "
                 "received, payable to the party; (d) Prepaid / advance — "
                 "paid ahead of the expense.",
                 "answer": "a"},
            ],
        )
        assert p.intent == "record_expense"
        assert "capitalization_decision" in p.missing_fields
        joined = "\n".join(p.clarification_questions).lower()
        assert "ordinary expense or capitalized" in joined
        assert "repairs & maintenance" in joined
        # The question is tap-to-answer (options payload exists).
        opts = options_for_question(CAP_Q)
        assert [o["value"] for o in opts] == ["A", "B"]
        assert [o["label"] for o in opts] == [
            "Ordinary expense", "Capitalize to fixed asset",
        ]

    def test_rent_purpose_never_asks_capitalization(self):
        p = plan(
            "record an expense of 80000",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "a"},
                {"question":
                 "Has this expense been paid, or is it outstanding? Reply "
                 "with a, b, c or d: (a) Paid now — cash; (b) Paid now — "
                 "bank / online; (c) Outstanding — invoice or bill "
                 "received, payable to the party; (d) Prepaid / advance — "
                 "paid ahead of the expense.",
                 "answer": "a"},
            ],
        )
        assert "capitalization_decision" not in p.missing_fields
        assert all(
            "ordinary expense or capitalized" not in q.lower()
            for q in p.clarification_questions
        )

    def test_small_repairs_auto_expense_without_asking(self):
        p = plan(
            "record an expense of 25000 for repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question":
                 "Has this expense been paid, or is it outstanding? Reply "
                 "with a, b, c or d: (a) Paid now — cash; (b) Paid now — "
                 "bank / online; (c) Outstanding — invoice or bill "
                 "received, payable to the party; (d) Prepaid / advance — "
                 "paid ahead of the expense.",
                 "answer": "a"},
            ],
        )
        assert "capitalization_decision" not in p.missing_fields
        assert p.intent == "record_expense"


class TestCapitalizedRouting:
    """A CAPITALIZE answer routes through the fixed-asset path; an
    EXPENSE answer stays on the expense path."""

    SETTLE_Q = (
        "Has this expense been paid, or is it outstanding? Reply with "
        "a, b, c or d: (a) Paid now — cash; (b) Paid now — bank / "
        "online; (c) Outstanding — invoice or bill received, payable "
        "to the party; (d) Prepaid / advance — paid ahead of the expense."
    )

    def test_capitalized_answer_routes_to_asset_registration(self):
        p = plan(
            "record an expense of 80000 for building repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question": self.SETTLE_Q, "answer": "a"},
                {"question": CAP_Q, "answer": "b"},
            ],
        )
        assert p.intent == "register_fixed_asset"
        assert p.extracted_entities["transaction_nature"] == "FIXED_ASSET"
        assert p.extracted_entities["asset_name"] == "Repairs & maintenance"

    def test_expense_answer_keeps_the_expense_path(self):
        p = plan(
            "record an expense of 80000 for building repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question": self.SETTLE_Q, "answer": "a"},
                {"question": CAP_Q, "answer": "a"},
            ],
        )
        assert p.intent == "record_expense"
        assert p.extracted_entities["transaction_nature"] == "OPERATING_EXPENSE"

    def test_capitalized_on_credit_stays_on_the_bill_path(self):
        p = plan(
            "record an expense of 80000 for building repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question": self.SETTLE_Q, "answer": "c"},
                {"question": CAP_Q, "answer": "b"},
            ],
        )
        # Credit + capitalized: Dr PPE, Cr party payable — the bill path.
        assert p.intent == "record_credit_purchase"
        assert p.extracted_entities["transaction_nature"] == "FIXED_ASSET"


class TestCapitalizationPreferenceLearning:
    """R3.2: the decision is recorded via preference_service per purpose
    so recurring purposes stop re-asking."""

    def test_capitalization_answer_is_preference_shaped(self):
        from app.services.preference_service import (
            CAPITALIZATION_KEY_PREFIX,
            capture_preference_from_answer,
        )

        shaped = capture_preference_from_answer(CAP_Q, "a")
        assert shaped is not None
        assert shaped["key"] == f"{CAPITALIZATION_KEY_PREFIX}REPAIRS_MAINTENANCE"
        assert shaped["value"] == "EXPENSE"

        shaped_b = capture_preference_from_answer(CAP_Q, "b")
        assert shaped_b["value"] == "CAPITALIZE"

    def test_learned_decision_suppresses_the_question(self):
        p = plan(
            "record an expense of 80000 for building repairs",
            org_preferences={"capitalization:REPAIRS_MAINTENANCE": "EXPENSE"},
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
            ],
        )
        assert "capitalization_decision" not in p.missing_fields
        assert p.extracted_entities["capitalization_decision"] == "EXPENSE"

    def test_learned_decision_capitalize_routes_to_asset(self):
        p = plan(
            "record an expense of 80000 for building repairs",
            org_preferences={"capitalization:REPAIRS_MAINTENANCE": "CAPITALIZE"},
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
            ],
        )
        assert p.intent == "register_fixed_asset"


class TestGenericExpenseFastPath:
    """R3.5: when the purpose is unambiguous (from the option list) and
    the settlement is confirmed, the expense executes deterministically
    (no LLM) — same pattern as the credit-purchase fast path."""

    SETTLE_Q = (
        "Has this expense been paid, or is it outstanding? Reply with "
        "a, b, c or d: (a) Paid now — cash; (b) Paid now — bank / "
        "online; (c) Outstanding — invoice or bill received, payable "
        "to the party; (d) Prepaid / advance — paid ahead of the expense."
    )
    DATE_Q = (
        "What date did you receive the goods/service or incur the "
        "obligation? This is the expense date — NOT the payment date. "
        "Reply TODAY, or the date as YYYY-MM-DD or DD/MM/YYYY (for "
        "example 2026-09-04 or 04/09/2026)."
    )

    def _resolved_plan(self):
        return plan(
            "record an expense of 25000",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "a"},
                {"question": self.SETTLE_Q, "answer": "a"},
                {"question": self.DATE_Q, "answer": "2026-08-31"},
            ],
        )

    @pytest.mark.asyncio
    async def test_fast_path_builds_expense_call_without_llm(self):
        from app.agent import _deterministic_mutation_calls

        p = self._resolved_plan()
        assert p.intent == "record_expense"
        assert p.requires_clarification is False
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is not None and len(calls) == 1
        tc = calls[0]
        assert tc.tool_name == "create_expense"
        assert tc.arguments["payee_name"] == "Local Vendor"
        assert tc.arguments["subtotal"] == 25000.0
        assert tc.arguments["expense_date"] == "2026-08-31"
        assert tc.arguments["payment_mode"] == "CASH"
        assert tc.arguments["description"].startswith("Rent")

    @pytest.mark.asyncio
    async def test_fast_path_refuses_ambiguous_purposes(self):
        from app.agent import _deterministic_mutation_calls

        # Repairs resolved as EXPENSE — still never fast-pathed (the
        # capitalization ladder case always completes its own path).
        p = plan(
            "record an expense of 80000 for building repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question": self.SETTLE_Q, "answer": "a"},
                {"question": CAP_Q, "answer": "a"},
                {"question": self.DATE_Q, "answer": "2026-08-31"},
            ],
        )
        assert p.intent == "record_expense"
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None

    @pytest.mark.asyncio
    async def test_fast_path_refuses_unanswered_ladder(self):
        from app.agent import _deterministic_mutation_calls

        # No purpose/settlement/date yet — the ladder asks, never guesses.
        p = plan("record an expense of 25000")
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None

    @pytest.mark.asyncio
    async def test_fast_path_refuses_capitalized_outcome(self):
        from app.agent import _deterministic_mutation_calls

        p = plan(
            "record an expense of 80000 for building repairs",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "f"},
                {"question": self.SETTLE_Q, "answer": "a"},
                {"question": CAP_Q, "answer": "b"},
                {"question": self.DATE_Q, "answer": "2026-08-31"},
            ],
        )
        assert p.intent == "register_fixed_asset"
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None