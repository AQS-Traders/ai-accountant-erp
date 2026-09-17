"""Work Stream R - CA-grade nature/purpose reasoning tests.

A chartered accountant establishes the PURPOSE of a transaction FIRST
(fixed asset vs inventory vs consumable vs expense vs settlement),
because the nature changes the ENTIRE accounting treatment.  The nature
question JOINS the FIRST consolidated questionnaire - ONE round, never a
drip-fed extra round - for EVERY intent that creates, moves or
classifies value, in EVERY module.
"""

import pytest

from app.planner import plan
from app.reasoning import (
    NATURE_DECISION_QUESTION,
    analyze_requirements,
    collect_open_questions,
    nature_question_for_intent,
)

DATE_Q = (
    "What is the transaction date? Reply TODAY, or the date as "
    "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
)


class TestNatureInFirstRound:
    """R0/R1: the FIRST consolidated round asks the nature/purpose
    question TOGETHER with the material fields - one round, dependency-
    first (nature -> cash/credit -> party -> amount -> date)."""

    def test_purchased_table_asks_nature_first(self):
        p = plan("I purchased a table")
        assert p.requires_clarification is True
        assert "transaction_nature" in p.missing_fields
        # purpose -> nature is the FIRST question of the round
        assert p.missing_fields[0] == "transaction_nature"
        assert p.clarification_questions[0] == NATURE_DECISION_QUESTION
        joined = "\n".join(p.clarification_questions)
        low = joined.lower()
        # the four-way decision tree, in the SAME round as the rest
        assert "fixed asset" in low and "inventory" in low
        assert "consumable" in low
        assert "What is the transaction amount?" in joined
        assert "paid in cash or on credit" in low
        assert DATE_Q in joined
        # dependency-first ordering, ONE round
        assert p.missing_fields == [
            "transaction_nature", "payment_type", "amount", "transaction_date",
        ]

    def test_sold_table_asks_sale_nature(self):
        p = plan("I sold a table")
        assert "transaction_nature" in p.missing_fields
        q = p.clarification_questions[0].lower()
        assert "goods" in q and "service" in q
        assert "fixed asset disposal" in q and "other income" in q

    def test_expense_asks_purpose_first(self):
        # Work Stream R3.1: an "expense" utterance is a DESCRIPTION, not an
        # accounting classification — the FIRST question is the purpose
        # (what is it actually for?), not the 4-way nature tree.
        p = plan("record expense: office renovation Rs.80,000")
        assert "transaction_purpose" in p.missing_fields
        assert p.missing_fields[0] == "transaction_purpose"
        q = p.clarification_questions[0]
        assert "What is this expense for" in q
        assert "Rent" in q and "Salaries & wages" in q
        assert "Repairs & maintenance" in q
        assert "Equipment / fixed-asset purchase" in q
        # The 4-way nature tree is REPLACED (never asked twice).
        assert all(
            "nature of this expense" not in q2.lower()
            for q2 in p.clarification_questions
        )

    def test_loose_expense_typo_still_reaches_the_ladder(self):
        # The observed R3 failure: "record an explanation of 25000" (typo
        # for "expense") used to fall to the generic model path and skip
        # the purpose question entirely.
        p = plan("record an explanation of 25000")
        assert p.intent == "record_expense"
        assert p.missing_fields[0] == "transaction_purpose"
        assert p.clarification_questions[0].startswith(
            "What is this expense for"
        )

    def test_receipt_asks_settlement_nature(self):
        p = plan("received payment of Rs.20,000 from the customer")
        assert "transaction_nature" in p.missing_fields
        q = p.clarification_questions[0].lower()
        assert "invoice" in q and "advance" in q and "loan" in q

    def test_payment_asks_settlement_nature(self):
        p = plan("paid the supplier Rs.12,000")
        assert "transaction_nature" in p.missing_fields
        q = p.clarification_questions[0].lower()
        assert "invoice" in q and "advance" in q and "loan" in q

    def test_credit_note_asks_return_nature(self):
        p = plan("issue a credit note for the damaged goods")
        assert "transaction_nature" in p.missing_fields
        q = p.clarification_questions[0].lower()
        assert "return of goods" in q and "price adjustment" in q
        assert "service reversal" in q

    def test_purchase_return_asks_return_nature(self):
        p = plan("purchase return to the supplier")
        assert "transaction_nature" in p.missing_fields
        assert "return of goods" in p.clarification_questions[0].lower()

    def test_quotation_asks_goods_or_service(self):
        p = plan("create a quotation for the customer")
        assert "transaction_nature" in p.missing_fields
        q = p.clarification_questions[0].lower()
        assert "goods" in q and "service" in q


class TestNatureSuppression:
    """R1: a learned nature preference suppresses the question (the user
    override always wins); the source of the nature is visible on the
    plan for the audit trail."""

    def test_learned_nature_preference_suppresses_question(self):
        p = plan(
            "I purchased a table",
            org_preferences={"transaction_nature": "INVENTORY"},
        )
        assert "transaction_nature" not in p.missing_fields
        assert p.extracted_entities["transaction_nature"] == "INVENTORY"
        assert p.transaction_nature == "INVENTORY"
        assert p.transaction_nature_source == "PREFERENCE"
        # the nature question really is absent from the round
        assert all(
            "fixed asset" not in q.lower() for q in p.clarification_questions
        )

    def test_user_nature_source_logged_on_plan(self):
        p = plan("I purchased a table", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "b"},
        ])
        assert p.transaction_nature == "INVENTORY"
        assert p.transaction_nature_source == "USER_ANSWER"

    def test_asset_lifecycle_never_redundantly_asks_nature(self):
        p = plan("Register a new fixed asset generator for Rs.850,000")
        assert p.intent == "register_fixed_asset"
        assert "transaction_nature" not in p.missing_fields


class TestNatureRouting:

    def test_sale_nature_disposal_routes_to_dispose_fixed_asset(self):
        p = plan("I sold a table for Rs.15,000", clarification_history=[
            {"question": nature_question_for_intent("record_sale"), "answer": "c"},
        ])
        assert p.intent == "dispose_fixed_asset"
        assert p.extracted_entities["transaction_nature"] == "ASSET_DISPOSAL"
        assert p.extracted_entities["asset_name"] == "table"

    def test_expense_nature_capitalisable_routes_to_register_fixed_asset(self):
        p = plan("record expense: office renovation Rs.80,000", clarification_history=[
            {"question": nature_question_for_intent("record_expense"), "answer": "c"},
        ])
        assert p.intent == "register_fixed_asset"
        assert p.extracted_entities["transaction_nature"] == "FIXED_ASSET"

    def test_receipt_nature_advance_recorded(self):
        p = plan("received payment of Rs.20,000 from the customer", clarification_history=[
            {"question": nature_question_for_intent("record_receipt"), "answer": "b"},
        ])
        assert p.intent == "record_receipt"
        assert p.extracted_entities["transaction_nature"] == "ADVANCE"

    def test_credit_note_nature_service_reversal_recorded(self):
        p = plan("issue a credit note for the damaged goods", clarification_history=[
            {"question": nature_question_for_intent("create_credit_note"), "answer": "c"},
        ])
        assert p.intent == "create_credit_note"
        assert p.extracted_entities["transaction_nature"] == "SERVICE_REVERSAL"


class TestNatureOrderingAndMapping:
    """R2: the consolidated questionnaire is answered in ONE message and
    every part maps back to its own question - including the nature
    answer."""

    def test_four_question_round_answered_in_one_line(self):
        p = plan("I purchased a table")
        assert len(p.clarification_questions) == 4
        questionnaire = (
            "To record this transaction I need a few things:\n"
            + "\n".join(
                f"{i}. {q}"
                for i, q in enumerate(p.clarification_questions, 1)
            )
        )
        p2 = plan("I purchased a table", clarification_history=[
            {"question": questionnaire,
             "answer": "1) a 2) cash 3) 25,000 4) 04/09/2026"},
        ])
        assert p2.intent == "register_fixed_asset"
        assert p2.requires_clarification is False
        assert p2.extracted_entities["transaction_nature"] == "FIXED_ASSET"
        assert p2.extracted_entities["payment_method"] == "CASH"
        assert p2.extracted_entities["amount"] == 25000.0
        assert p2.extracted_entities["transaction_date"] == "2026-09-04"

    def test_ambiguous_nature_answer_is_reasked_not_guessed(self):
        p = plan("I purchased a table", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "kind of an asset"},
        ])
        assert "transaction_nature" in p.missing_fields


class TestNatureDependencyGraph:
    """R1: the 360-degree dependency graph asks the nature node for EVERY
    value intent - a missing amount never suppresses it."""

    def test_nature_node_fires_for_receipt_without_item(self):
        nodes = analyze_requirements(
            intent="record_receipt",
            entities={"amount": 5000.0},
            missing_fields=["transaction_nature"],
        )
        open_qs = collect_open_questions(nodes)
        assert any(q["field"] == "transaction_nature" for q in open_qs)
        assert any("invoice" in q["question"].lower() for q in open_qs)

    def test_missing_amount_never_suppresses_nature_node(self):
        nodes = analyze_requirements(
            intent="record_payment",
            entities={"amount": None},
            missing_fields=["amount", "transaction_nature"],
        )
        open_qs = collect_open_questions(nodes)
        fields = [q["field"] for q in open_qs]
        assert "transaction_nature" in fields
        assert "amount" in fields
        # dependency-first: nature first
        assert fields[0] == "transaction_nature"

    def test_known_nature_suppresses_graph_question(self):
        nodes = analyze_requirements(
            intent="record_payment",
            entities={"amount": 5000.0, "transaction_nature": "ADVANCE"},
            missing_fields=[],
        )
        assert not any(
            n.key == "transaction_nature" and n.question for n in nodes
        )

    """R3: the nature answer deterministically re-routes the intent (via
    the planner's existing routing, feeding the classifier as
    USER_ANSWER)."""

    def test_purchase_nature_fixed_asset_routes_to_register_fixed_asset(self):
        p = plan("I purchased a table for Rs.25,000", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "a"},
            {"question": "Was this paid in cash or on credit?", "answer": "cash"},
            {"question": DATE_Q, "answer": "04/09/2026"},
        ])
        assert p.intent == "register_fixed_asset"
        assert p.requires_clarification is False
        assert p.extracted_entities["transaction_nature"] == "FIXED_ASSET"
        assert p.extracted_entities["payment_method"] == "CASH"
        assert p.extracted_entities["amount"] == 25000.0
        assert p.extracted_entities["transaction_date"] == "2026-09-04"
        assert p.extracted_entities["asset_name"] == "table"

    def test_purchase_nature_consumable_routes_to_expense(self):
        p = plan("I purchased cleaning supplies for Rs.3,000", clarification_history=[
            {"question": NATURE_DECISION_QUESTION, "answer": "c"},
            {"question": "Was this paid in cash or on credit?", "answer": "cash"},
        ])
        assert p.intent == "record_expense"
        assert p.extracted_entities["transaction_nature"] == "OPERATING_EXPENSE"
