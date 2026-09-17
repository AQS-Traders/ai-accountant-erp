"""Work Stream R3 — purpose-first reasoning & universal interactive
clarification tests (R3.1 taxonomy, R3.3 settlement + event date).

An "expense" utterance is the user's DESCRIPTION of an event, not an
accounting classification.  After amount+intent the agent's FIRST
question is the PURPOSE (with a tap-to-answer option list); the purpose
answer derives the accounting nature; the settlement position (paid
cash/bank vs outstanding vs prepaid) and the UNDERLYING EVENT date
(not the payment date) follow — never an invented account.
"""

import pytest

from app.planner import plan
from app.reasoning import (
    EXPENSE_EVENT_DATE_QUESTION,
    SETTLEMENT_POSITION_QUESTION,
    options_for_question,
    purpose_label,
    purpose_question,
    resolve_purpose_answer,
    resolve_settlement_answer,
    strip_markdown,
)

PURPOSE_Q = purpose_question()


class TestPurposeQuestionFiresFirst:
    """R3.1: the purpose question is the FIRST question of the expense
    ladder and carries the full tap-to-answer option list."""

    def test_purpose_question_fires_for_generic_expense(self):
        p = plan("record an expense of 25000")
        assert p.requires_clarification is True
        assert p.missing_fields[0] == "transaction_purpose"
        assert p.clarification_questions[0].startswith(
            "What is this expense for"
        )

    def test_purpose_precedes_settlement_and_date(self):
        p = plan("record an expense of 25000")
        assert p.missing_fields == [
            "transaction_purpose", "settlement_position", "transaction_date",
        ]

    def test_purpose_options_ship_from_the_backend(self):
        opts = options_for_question(PURPOSE_Q)
        assert opts is not None and len(opts) == 15
        labels = [o["label"] for o in opts]
        assert labels[0] == "Rent"
        assert "Repairs & maintenance" in labels
        assert "Equipment / fixed-asset purchase" in labels
        assert "Something else" in labels
        # values are the tappable letters a..o
        assert [o["value"] for o in opts] == list("abcdefghijklmno")

    def test_no_account_code_leaks_into_the_question(self):
        p = plan("record an expense of 25000")
        joined = "\n".join(p.clarification_questions)
        assert "6010" not in joined
        assert "as indicated" not in joined.lower()


class TestPurposeAnswerResolution:
    """The purpose answer is deterministic: letters, wording, or free
    text — never guessed."""

    def test_letter_answers(self):
        assert resolve_purpose_answer(PURPOSE_Q, "a") == "RENT"
        assert resolve_purpose_answer(PURPOSE_Q, "f") == "REPAIRS_MAINTENANCE"
        assert resolve_purpose_answer(PURPOSE_Q, "l") == "EQUIPMENT_PURCHASE"
        assert resolve_purpose_answer(PURPOSE_Q, "j") == "INVENTORY_PURCHASE"

    def test_wording_answers(self):
        assert (
            resolve_purpose_answer(PURPOSE_Q, "repairs and maintenance")
            == "REPAIRS_MAINTENANCE"
        )
        assert resolve_purpose_answer(PURPOSE_Q, "office rent") == "RENT"

    def test_free_text_other_and_durable_detection(self):
        assert resolve_purpose_answer(PURPOSE_Q, "team lunch") == "OTHER"
        # Durable free-text wording upgrades to the capitalization ladder;
        # explicit equipment wording is recognized directly.
        assert (
            resolve_purpose_answer(PURPOSE_Q, "a new packing machine")
            == "EQUIPMENT_PURCHASE"
        )
        assert (
            resolve_purpose_answer(PURPOSE_Q, "renovation of the north wall")
            == "REPAIRS_MAINTENANCE"  # renovation IS repairs & maintenance
        )
        assert (
            resolve_purpose_answer(PURPOSE_Q, "server room fitout")
            == "OTHER_DURABLE"
        )

    def test_unresolvable_is_none(self):
        assert resolve_purpose_answer(PURPOSE_Q, "") is None

    def test_nature_derived_from_purpose(self):
        from app.reasoning import nature_for_purpose

        assert nature_for_purpose("RENT") == "OPERATING_EXPENSE"
        assert nature_for_purpose("EQUIPMENT_PURCHASE") == "FIXED_ASSET"
        assert nature_for_purpose("INVENTORY_PURCHASE") == "INVENTORY"
        assert nature_for_purpose("REPAIRS_MAINTENANCE") == "OPERATING_EXPENSE"
        assert (
            nature_for_purpose("REPAIRS_MAINTENANCE", capitalized=True)
            == "FIXED_ASSET"
        )

    def test_purpose_round2_sets_nature_and_item(self):
        p = plan(
            "record an expense of 25000",
            clarification_history=[{"question": PURPOSE_Q, "answer": "a"}],
        )
        assert p.extracted_entities["transaction_purpose"] == "RENT"
        assert p.extracted_entities["transaction_nature"] == "OPERATING_EXPENSE"
        assert p.extracted_entities["item_description"] == "Rent"
        # R3.5: description auto-filled from the purpose label
        assert p.extracted_entities["description"].startswith("Rent")

    def test_purpose_label_fallback(self):
        assert purpose_label("RENT") == "Rent"


class TestSettlementPosition:
    """R3.3: paid now (cash/bank) vs outstanding (payable) vs prepaid —
    asked explicitly, never assumed."""

    def test_settlement_question_joins_the_first_round(self):
        p = plan("record an expense of 25000")
        assert "settlement_position" in p.missing_fields
        assert SETTLEMENT_POSITION_QUESTION in p.clarification_questions

    def test_settlement_options_ship_from_the_backend(self):
        opts = options_for_question(SETTLEMENT_POSITION_QUESTION)
        assert [o["value"] for o in opts] == ["a", "b", "c", "d", "e"]
        assert [o["label"] for o in opts] == [
            "Paid now — cash (Dr expense / Cr cash)",
            "Paid now — bank (Dr expense / Cr bank)",
            "Payable — pay later (Cr trade payables)",
            "Settle an earlier payable (no new expense)",
            "Prepaid — paid ahead (prepayment)",
        ]

    def test_answer_letters_map(self):
        q = SETTLEMENT_POSITION_QUESTION
        assert resolve_settlement_answer(q, "a") == "CASH"
        assert resolve_settlement_answer(q, "b") == "BANK_TRANSFER"
        assert resolve_settlement_answer(q, "c") == "CREDIT"
        assert resolve_settlement_answer(q, "d") == "SETTLE_EXISTING_PAYABLE"
        assert resolve_settlement_answer(q, "e") == "ACCRUAL_PREPAID"

    def test_outstanding_answer_routes_to_the_bill_path(self):
        p = plan(
            "record an expense of 25000",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "a"},
                {"question": SETTLEMENT_POSITION_QUESTION, "answer": "c"},
            ],
        )
        # CREDIT treatment: Dr expense, Cr party payable — the purchase-
        # bill path (the party question follows in its own round).
        assert p.intent == "record_credit_purchase"
        assert p.extracted_entities["payment_method"] == "CREDIT"

    def test_paid_cash_answer_keeps_the_expense_path(self):
        p = plan(
            "record an expense of 25000",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "a"},
                {"question": SETTLEMENT_POSITION_QUESTION, "answer": "a"},
            ],
        )
        assert p.intent == "record_expense"
        assert p.extracted_entities["payment_method"] == "CASH"

    def test_prepaid_answer_sets_prepaid_nature(self):
        p = plan(
            "record an expense of 25000",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "a"},
                {"question": SETTLEMENT_POSITION_QUESTION, "answer": "e"},
            ],
        )
        assert p.extracted_entities["transaction_nature"] == "PREPAID_EXPENSE"


class TestUnderlyingEventDate:
    """R3.3: the expense date question asks about the UNDERLYING EVENT
    (when the goods/service were received or the obligation incurred) —
    NOT the payment date — keeping the Work Stream A wire format."""

    def test_event_date_question_wording(self):
        p = plan("record an expense of 25000")
        date_q = next(
            q for q in p.clarification_questions if "date" in q.lower()
        )
        assert date_q == EXPENSE_EVENT_DATE_QUESTION
        assert "NOT the payment date" in date_q
        assert "receive the goods/service" in date_q
        # Date protocol preserved: both formats + example.
        assert "YYYY-MM-DD" in date_q and "DD/MM/YYYY" in date_q
        assert "2026-09-04" in date_q and "04/09/2026" in date_q

    def test_date_options_ship_today_and_yesterday(self):
        opts = options_for_question(EXPENSE_EVENT_DATE_QUESTION)
        assert [o["value"] for o in opts] == ["TODAY", "YESTERDAY"]

    def test_event_date_answer_resolves(self):
        p = plan(
            "record an expense of 25000",
            clarification_history=[
                {"question": PURPOSE_Q, "answer": "a"},
                {"question": SETTLEMENT_POSITION_QUESTION, "answer": "a"},
                {"question": EXPENSE_EVENT_DATE_QUESTION, "answer": "2026-08-31"},
            ],
        )
        assert p.extracted_entities["transaction_date"] == "2026-08-31"


class TestMarkdownSanitize:
    """R3.4a: model markdown never reaches the question card."""

    def test_bold_and_backticks_stripped(self):
        assert strip_markdown("**Salaries** account") == "Salaries account"
        assert strip_markdown("use the `Rent` account") == "use the Rent account"
        assert strip_markdown("## Heading\ntext") == "Heading\ntext"

    def test_plain_text_untouched(self):
        assert strip_markdown("plain question?") == "plain question?"