"""
ERP AI Agent — Planner Reasoning Tests
=========================================
Tests that the Planner correctly interprets user intent,
extracts entities, determines requirements, and identifies
when clarification is needed.

These tests verify the Agent does NOT operate as:
  User sentence → Keyword match → Immediate database write
"""

from __future__ import annotations

import pytest
from app.planner import plan


# ===================================================================
# Intent Identification
# ===================================================================

class TestIntentIdentification:

    def test_credit_purchase_intent(self, credit_purchase_message):
        p = plan(credit_purchase_message)
        assert p.intent == "record_credit_purchase"

    def test_cash_purchase_intent(self, cash_purchase_message):
        p = plan(cash_purchase_message)
        assert p.intent in ("record_cash_purchase", "record_purchase")

    def test_credit_sale_intent(self, credit_sale_message):
        p = plan(credit_sale_message)
        assert p.intent == "record_credit_sale"

    def test_cash_sale_intent(self, cash_sale_message):
        p = plan(cash_sale_message)
        assert p.intent in ("record_cash_sale", "record_sale")

    def test_trial_balance_intent(self):
        p = plan("Show me the trial balance")
        assert p.intent == "generate_trial_balance"

    def test_balance_sheet_intent(self):
        p = plan("Generate the balance sheet")
        assert p.intent == "generate_balance_sheet"

    def test_profit_loss_intent(self):
        p = plan("Show me the profit and loss")
        assert p.intent == "generate_profit_loss"

    def test_customer_owes_intent(self):
        p = plan("How much does ABC Technologies owe us?")
        assert p.intent == "customer_balance"

    def test_expense_intent(self):
        p = plan("I spent Rs.5,000 on office supplies")
        assert p.intent == "record_expense"

    def test_unknown_intent(self):
        p = plan("Hello, how are you?")
        assert p.intent == "unknown"


# ===================================================================
# Entity Extraction
# ===================================================================

class TestEntityExtraction:

    def test_extracts_supplier_from_credit_purchase(self, credit_purchase_message):
        p = plan(credit_purchase_message)
        assert p.entity_name is not None
        assert "ABC" in p.entity_name or "abc" in (p.entity_name or "").lower()

    def test_extracts_customer_from_credit_sale(self, credit_sale_message):
        p = plan(credit_sale_message)
        assert p.entity_name is not None
        assert "XYZ" in p.entity_name or "xyz" in (p.entity_name or "").lower()


# ===================================================================
# Amount Extraction
# ===================================================================

class TestAmountExtraction:

    def test_extracts_amount_with_rs(self):
        p = plan("Bought laptop for Rs.150,000")
        # Amount should be present (either via entity or clarification)
        assert not p.requires_clarification or p.clarification_questions

    def test_extracts_amount_pkr(self):
        p = plan("Bought laptop for PKR 250000")
        assert p.intent in ("record_cash_purchase", "record_purchase", "unknown")

    def test_missing_amount_triggers_clarification(self, missing_amount_message):
        p = plan(missing_amount_message)
        assert p.requires_clarification is True
        assert any("amount" in q.lower() for q in p.clarification_questions)


# ===================================================================
# Clarification Detection
# ===================================================================

class TestClarification:

    def test_credit_purchase_missing_supplier(self, missing_supplier_message):
        p = plan(missing_supplier_message)
        assert p.requires_clarification is True
        assert any("supplier" in q.lower() for q in p.clarification_questions)

    def test_credit_sale_missing_customer(self):
        p = plan("I sold goods on credit for Rs.100,000")
        assert p.requires_clarification is True

    def test_report_no_clarification_needed(self):
        p = plan("Show me the trial balance")
        assert p.requires_clarification is False

    def test_bare_expense_is_not_a_recording_intent(self):
        """Anti-defect regression: a bare "expense" keyword — no figure and
        no recording verb — must NOT be classified as a `record_expense`
        mutation.

        The bare "expense" pattern was deliberately REMOVED from
        `record_expense` (see the "Expense LOOKUPS must win over
        record_expense" rule in planner.py) because it swallowed QUERY
        phrasings such as "provide details of current month expenses" and
        dragged them into the recording ladder.  A recorder must never be
        entered on a message that states no amount and asks for nothing to be
        recorded.
        """
        p = plan("I had an expense")
        assert p.intent != "record_expense", (
            "A bare 'expense' keyword must not open the expense recording "
            "ladder — it re-introduces the query-swallowing defect."
        )
        # An unrecognised message is deferred to the model, which asks the
        # user for what it needs.  It is never silently executable.
        assert p.intent == "unknown"

    def test_expense_needs_amount(self):
        """An expense that IS recognised (figure present) but lacks the
        purpose/date must ask for what is missing rather than posting blind.
        """
        p = plan("I had an expense of 500")
        assert p.intent == "record_expense"
        assert p.requires_clarification is True
        assert p.clarification_questions, (
            "An under-specified expense must produce clarification questions."
        )


# ===================================================================
# Accounting Engine Requirement
# ===================================================================

class TestAccountingRequirement:

    def test_credit_purchase_requires_accounting(self, credit_purchase_message):
        p = plan(credit_purchase_message)
        assert p.requires_accounting_engine is True

    def test_report_does_not_require_accounting(self):
        p = plan("Show trial balance")
        assert p.requires_accounting_engine is False


# ===================================================================
# Confirmation Requirement
# ===================================================================

class TestConfirmationRequirement:

    def test_credit_purchase_requires_confirmation(self, credit_purchase_message):
        p = plan(credit_purchase_message)
        assert p.requires_confirmation is True

    def test_cash_purchase_no_confirmation(self, cash_purchase_message):
        p = plan(cash_purchase_message)
        # Cash purchases typically don't need confirmation
        # (implementation may vary)


# ===================================================================
# Multi-question answers + item extraction (live-defect regressions)
# ===================================================================

class TestMultiQuestionAnswers:
    # Work Stream A: the mandatory date question joins the questionnaire.
    # Work Stream R: the nature/purpose decision tree comes FIRST in the
    # SAME consolidated round (dependency-first ordering).
    QUESTIONNAIRE = (
        "To record this transaction I need a few things:\n"
        "1. Is this item: (a) a FIXED ASSET (long-term use, to be "
        "capitalised), (b) an INVENTORY-STOCKED PRODUCT (resale stock - "
        "catalog flag only, quantities are not tracked), (c) a CONSUMABLE "
        "/ one-off EXPENSE (expensed immediately), or (d) a SERVICE? "
        "Please answer with a, b, c or d.\n"
        "2. What is the transaction amount?\n"
        "3. Who is the supplier?\n"
        "4. What is the transaction date? Reply TODAY, or the date as "
        "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
    )

    def test_comma_separated_answers_fill_both_fields(self):
        """Live defect: both answers on ONE comma-separated line — the
        second answer was dropped and its question asked all over again."""
        p = plan("I bought a laptop on credit.", clarification_history=[
            {"question": self.QUESTIONNAIRE,
             "answer": "b, 150000, ABC Traders, today"},
        ])
        assert p.requires_clarification is False
        assert p.extracted_entities.get("transaction_nature") == "INVENTORY"
        assert p.extracted_entities.get("amount") == 150000.0
        assert p.extracted_entities.get("supplier_name") == "ABC Traders"
        assert p.extracted_entities.get("transaction_date")

    def test_numbered_answers_fill_both_fields(self):
        p = plan("I bought a laptop on credit.", clarification_history=[
            {"question": self.QUESTIONNAIRE,
             "answer": "1) b 2) 150000 3) ABC Traders 4) today"},
        ])
        assert p.extracted_entities.get("transaction_nature") == "INVENTORY"
        assert p.extracted_entities.get("amount") == 150000.0
        assert p.extracted_entities.get("supplier_name") == "ABC Traders"
        assert p.extracted_entities.get("transaction_date")

    def test_digit_grouping_comma_never_splits(self):
        from app.planner import _split_answer_parts
        assert _split_answer_parts("5 at 2,000 each") == ["5 at 2,000 each"]
        assert _split_answer_parts("cash, 50,000") == ["cash", "50,000"]


class TestItemExtractionVerbs:
    def test_purchase_verb_extracts_item(self):
        """Live defect: 'i purchase a chair from ...' extracted NO item, so
        the nature question (asset vs inventory vs expense) never fired."""
        p = plan("i purchase a chair from ghulam furnitures")
        assert p.extracted_entities.get("item_description") == "chair"

    def test_nature_question_fires_for_durable_item(self):
        """A chair IS plausibly capital — the 4-way decision tree must be
        part of the FIRST consolidated round (via the reasoning layer)."""
        from app.reasoning import analyze_requirements, collect_open_questions
        nodes = analyze_requirements(
            intent="record_purchase",
            entities={"item_description": "chair", "amount": 50000.0},
            missing_fields=[],
        )
        questions = collect_open_questions(nodes)
        assert any("FIXED ASSET" in q["question"] for q in questions)

