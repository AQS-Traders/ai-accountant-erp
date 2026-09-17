"""
ERP AI Agent — Requirement State Coverage Tests
=================================================
Tests for the 360° requirement/dependency reasoning layer
(app/reasoning.py) and the consolidated questionnaire flow.

Focus: STATE coverage, not transaction examples —

* empty database results are information, never failures;
* every dependency dimension gets an explicit resolution state;
* only genuinely-open user decisions become questions;
* ALL independent gaps are consolidated into ONE questionnaire;
* ambiguity (multiple party matches) is surfaced, never guessed;
* answered questions are never re-asked (full post-answer re-evaluation).
"""

from __future__ import annotations

import pytest

from app.models.schemas import TransactionClassification
from app.planner import plan
from app.reasoning import (
    DependencyNode,
    FieldResolutionState,
    NATURE_DECISION_QUESTION,
    PartyMatchState,
    analyze_requirements,
    classify_party_match,
    collect_open_questions,
    format_questionnaire,
    interpret_search_rows,
    plan_clarification_text,
)
from app.agent import party_resolved_in_search


# ===================================================================
# Search-result interpretation — EMPTY_RESULT ≠ failure
# ===================================================================


class TestInterpretSearchRows:

    def test_empty_list_is_empty_result_not_failure(self):
        state, rows = interpret_search_rows([])
        assert state is FieldResolutionState.EMPTY_RESULT
        assert rows == []

    def test_single_row_is_found(self):
        state, rows = interpret_search_rows([{"id": "a", "name": "X"}])
        assert state is FieldResolutionState.FOUND
        assert len(rows) == 1

    def test_multiple_rows_are_multiple_matches(self):
        state, rows = interpret_search_rows([{"id": "a"}, {"id": "b"}])
        assert state is FieldResolutionState.MULTIPLE_MATCHES

    def test_none_is_not_queried(self):
        state, _ = interpret_search_rows(None)
        assert state is FieldResolutionState.NOT_QUERIED

    def test_wrapped_result_shape_is_tolerated(self):
        state, rows = interpret_search_rows({"data": [{"id": "a"}]})
        assert state is FieldResolutionState.FOUND
        assert len(rows) == 1


# ===================================================================
# Party match states — exact / partial / ambiguous / none
# ===================================================================


class TestClassifyPartyMatch:

    def test_exact_match_wins_over_partials(self):
        rows = [
            {"name": "ABC Traders (Lahore)"},
            {"name": "ABC Traders"},
            {"name": "Other Co"},
        ]
        state, matches = classify_party_match(rows, "ABC Traders")
        assert state is PartyMatchState.EXACT
        assert len(matches) == 1
        assert matches[0]["name"] == "ABC Traders"

    def test_multiple_partial_matches_are_ambiguous(self):
        rows = [
            {"name": "ABC Corporation Ltd"},
            {"name": "ABC Traders"},
            {"name": "Unrelated"},
        ]
        state, matches = classify_party_match(rows, "ABC")
        assert state is PartyMatchState.AMBIGUOUS
        assert len(matches) == 2

    def test_single_partial_match_is_resolved(self):
        rows = [{"name": "xyz computers (lahore)"}]
        state, _ = classify_party_match(rows, "XYZ Computers")
        assert state is PartyMatchState.RESOLVED_PARTIAL

    def test_empty_database_is_none_not_failure(self):
        state, _ = classify_party_match([], "XYZ Computers")
        assert state is PartyMatchState.NONE

    def test_malformed_payload_is_none_not_exception(self):
        state, _ = classify_party_match("unexpected-shape", "XYZ Computers")
        assert state is PartyMatchState.NONE

    def test_unnamed_entity_is_unnamed(self):
        state, _ = classify_party_match([{"name": "XYZ Computers"}], None)
        assert state is PartyMatchState.UNNAMED

    def test_case_and_punctuation_insensitive(self):
        state, _ = classify_party_match(
            [{"name": "Abc-Traders, Ltd."}], "abc traders ltd"
        )
        assert state is PartyMatchState.EXACT


class TestPartyResolvedRegression:
    """party_resolved_in_search delegates to classify_party_match — the
    previously verified truth table must not change."""

    def test_resolved_when_named_party_hit(self):
        rows = [{"id": "s1", "name": "XYZ Computers"}, {"id": "s2", "name": "Other"}]
        assert party_resolved_in_search(rows, "XYZ Computers") is True

    def test_resolved_case_and_partial_insensitive(self):
        assert party_resolved_in_search(
            [{"name": "xyz computers (lahore)"}], "XYZ Computers"
        ) is True

    def test_not_resolved_when_absent_or_unnamed(self):
        assert party_resolved_in_search([{"name": "Someone Else"}], "XYZ Computers") is False
        assert party_resolved_in_search([{"name": "XYZ Computers"}], None) is False
        assert party_resolved_in_search([], "XYZ Computers") is False


# ===================================================================
# 360° dependency graph — requirement states per dimension
# ===================================================================


def _classification(**kw) -> TransactionClassification:
    return TransactionClassification(**kw)


class TestAnalyzeRequirements:

    def test_cash_purchase_party_is_missing_but_optional(self):
        """Cash purchase: the party LEDGER is never a required dependency —
        even when the user names a party, the name is informational only.
        Work Stream R: the nature/purpose decision is the ONLY open
        question (asked, never guessed)."""
        nodes = analyze_requirements(
            intent="record_cash_purchase",
            entities={"amount": 150000.0, "supplier_name": "Dell"},
            missing_fields=[],
        )
        counterparty = next(n for n in nodes if n.key == "counterparty")
        assert counterparty.state is FieldResolutionState.MISSING_BUT_OPTIONAL
        assert counterparty.question is None  # MUST NOT ask
        open_qs = collect_open_questions(nodes)
        assert [q["field"] for q in open_qs] == ["transaction_nature"]

    def test_credit_purchase_empty_erp_requires_supplier(self):
        """Credit purchase with an EMPTY supplier table: the dependency is
        genuinely missing AND required → one user question.  Work Stream
        R: the nature question precedes it (dependency-first)."""
        nodes = analyze_requirements(
            intent="record_credit_purchase",
            entities={"amount": 150000.0},
            missing_fields=["supplier_name"],
        )
        counterparty = next(n for n in nodes if n.key == "counterparty")
        assert counterparty.state is FieldResolutionState.MISSING_AND_REQUIRED
        questions = collect_open_questions(nodes)
        assert [q["field"] for q in questions] == [
            "transaction_nature", "counterparty",
        ]
        assert "supplier" in questions[1]["question"].lower()

    def test_amount_known_from_user_is_never_asked(self):
        nodes = analyze_requirements(
            intent="record_cash_sale",
            entities={"amount": 20000.0},
            missing_fields=[],
        )
        amount = next(n for n in nodes if n.key == "amount")
        assert amount.state is FieldResolutionState.KNOWN_FROM_USER
        assert amount.question is None

    def test_generic_intent_payment_treatment_is_required(self):
        """Generic 'bought/sold' without cash/credit wording: the treatment
        changes the accounting entries → genuine user decision."""
        nodes = analyze_requirements(
            intent="record_purchase",
            entities={"amount": 5000.0},
            missing_fields=["payment_type"],
        )
        treatment = next(n for n in nodes if n.key == "payment_treatment")
        assert treatment.state is FieldResolutionState.MISSING_AND_REQUIRED
        assert "cash" in treatment.question.lower()

    def test_classification_with_hint_is_resolvable_from_erp(self):
        cls = _classification(
            transaction_nature="FIXED_ASSET",
            confidence="HIGH",
            source="ITEM_MAPPING",
            account_hint_id="acc-1",
            account_hint_code="1500",
            account_hint_name="Computer Equipment",
        )
        nodes = analyze_requirements(
            intent="record_cash_purchase",
            entities={"amount": 150000.0, "item": "laptop"},
            missing_fields=[],
            classification=cls,
        )
        mapping = next(n for n in nodes if n.key == "account_mapping")
        assert mapping.state is FieldResolutionState.RESOLVABLE_FROM_ERP
        assert mapping.question is None

    def test_classification_config_gap_is_blocked_not_invented(self):
        """Nature decided but no configured account: BLOCKED_BY_CONFIGURATION
        — the agent must ask, never invent an accounting treatment."""
        cls = _classification(
            transaction_nature="OPERATING_EXPENSE",
            confidence="HIGH",
            source="DETERMINISTIC_RULE",
            requires_clarification=True,
            clarification_reason="No 'Marketing & advertising expense' account exists",
        )
        nodes = analyze_requirements(
            intent="record_expense",
            entities={"amount": 30000.0, "item": "facebook advertising"},
            missing_fields=[],
            classification=cls,
        )
        mapping = next(n for n in nodes if n.key == "account_mapping")
        assert mapping.state is FieldResolutionState.BLOCKED_BY_CONFIGURATION
        assert mapping.kind == "CONFIGURATION_GAP"
        questions = collect_open_questions(nodes)
        assert questions and questions[0]["kind"] == "CONFIGURATION_GAP"

    def test_ambiguous_classification_asks_not_guesses(self):
        cls = _classification(
            transaction_nature=None,
            confidence="LOW",
            source="INFERENCE",
            requires_clarification=True,
            clarification_reason="Materially ambiguous fixed-asset-versus-expense treatment",
        )
        nodes = analyze_requirements(
            intent="record_cash_purchase",
            entities={"amount": 150000.0, "item": "laptop"},
            missing_fields=[],
            classification=cls,
        )
        mapping = next(n for n in nodes if n.key == "account_mapping")
        assert mapping.state is FieldResolutionState.AMBIGUOUS
        assert collect_open_questions(nodes)

    def test_empty_bank_config_is_blocked_by_configuration(self):
        nodes = analyze_requirements(
            intent="record_bank_transfer",
            entities={},
            missing_fields=[],
            relevant_bank_accounts=[],
        )
        banks = next(n for n in nodes if n.key == "bank_accounts")
        assert banks.state is FieldResolutionState.BLOCKED_BY_CONFIGURATION
        assert banks.kind == "CONFIGURATION_GAP"

    def test_populated_bank_config_resolves_from_erp(self):
        nodes = analyze_requirements(
            intent="record_bank_transfer",
            entities={},
            missing_fields=[],
            relevant_bank_accounts=[{"id": "b1", "name": "HBL"}],
        )
        banks = next(n for n in nodes if n.key == "bank_accounts")
        assert banks.state is FieldResolutionState.RESOLVABLE_FROM_ERP
        assert banks.question is None

    def test_dependency_graph_is_transaction_specific(self):
        """A plain expense carries no bank dimension, and its counterparty
        dimension is explicitly OPTIONAL — the graph is event-specific,
        never a universal questionnaire."""
        nodes = analyze_requirements(
            intent="record_expense",
            entities={"amount": 5000.0},
            missing_fields=[],
        )
        assert all(n.key != "bank_accounts" for n in nodes)
        counterparty = next(
            (n for n in nodes if n.key == "counterparty"), None
        )
        if counterparty is not None:
            assert counterparty.state is FieldResolutionState.MISSING_BUT_OPTIONAL


class TestCollectOpenQuestionsOrdering:

    def test_independent_questions_precede_dependent_ones(self):
        nodes = [
            DependencyNode(
                key="counterparty", dimension="Counterparty",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                question="Who is the supplier?", requires=("party_resolution",),
            ),
            DependencyNode(
                key="amount", dimension="Transaction amount",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                question="What is the transaction amount?",
            ),
        ]
        ordered = collect_open_questions(nodes)
        assert [q["field"] for q in ordered] == ["amount", "counterparty"]

    def test_optional_absence_never_yields_questions(self):
        nodes = [
            DependencyNode(
                key="counterparty", dimension="Counterparty ledger",
                state=FieldResolutionState.MISSING_BUT_OPTIONAL,
            ),
        ]
        assert collect_open_questions(nodes) == []


# ===================================================================
# Consolidated questionnaire (planner → single round)
# ===================================================================


class TestConsolidatedQuestionnaire:

    def test_all_missing_fields_in_one_round(self):
        """Credit purchase missing BOTH amount and supplier: the planner
        must surface every independent gap — not just the first one."""
        p = plan("I bought a laptop on credit.")
        assert p.requires_clarification is True
        assert set(p.missing_fields) >= {"amount", "supplier_name"}
        assert len(p.clarification_questions) >= 2

    def test_plan_clarification_text_consolidates(self):
        text = plan_clarification_text(
            ["What is the transaction amount?", "Who is the supplier?"],
            ["amount", "supplier_name"],
        )
        assert text is not None
        assert "amount" in text.lower()
        assert "supplier" in text.lower()
        # numbered consolidated list, not a single question
        assert "1." in text and "2." in text

    def test_single_question_stays_plain(self):
        text = plan_clarification_text(
            ["What is the transaction amount?"], ["amount"]
        )
        assert text == "What is the transaction amount?"

    def test_no_questions_means_no_clarification(self):
        assert plan_clarification_text([], []) is None

    def test_format_questionnaire_empty_is_none(self):
        assert format_questionnaire([]) is None
        assert format_questionnaire(None) is None

    def test_cash_purchase_never_asks_for_supplier(self):
        """Explicit cash intent + complete entities: the supplier is NEVER
        asked.  Work Stream R: the ONE remaining gap is the nature/purpose
        decision (asset vs inventory vs consumable vs service) - asked,
        never guessed; the date resolved silently from "yesterday"."""
        from app.reasoning import nature_question_for_intent

        p = plan("I bought a Dell laptop for Rs.150,000 in cash yesterday.")
        assert "supplier_name" not in p.missing_fields
        assert p.missing_fields == ["transaction_nature"]
        assert p.clarification_questions == [
            nature_question_for_intent("record_cash_purchase")
        ]


class TestPostAnswerReEvaluation:
    """After clarification answers, the FULL plan is re-derived: answers
    are merged and no answered question is ever re-asked."""

    CREDIT = "I bought a laptop on credit."
    # Work Stream A: the standardized transaction-date question.  The
    # mandatory date protocol means a mutation without a resolved date
    # asks for it in the SAME consolidated questionnaire.
    DATE_Q = (
        "What is the transaction date? Reply TODAY, or the date as "
        "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
    )
    # Work Stream R: the nature/purpose decision tree joins the round
    # (asset vs inventory vs consumable vs service - here: resale stock).
    NATURE_Q = NATURE_DECISION_QUESTION
    ANSWERS = [
        {"question": NATURE_Q, "answer": "b"},
        {"question": "What is the transaction amount?", "answer": "150000"},
        {"question": "Who is the supplier?", "answer": "ABC Traders"},
        {"question": DATE_Q, "answer": "today"},
    ]

    def test_answers_are_merged_and_not_reasked(self):
        merged = plan(self.CREDIT, clarification_history=self.ANSWERS)
        assert merged.requires_clarification is False
        assert merged.missing_fields == []
        assert merged.extracted_entities.get("amount") == 150000.0
        assert merged.extracted_entities.get("supplier_name") == "ABC Traders"
        assert merged.extracted_entities.get("transaction_date")

    def test_partial_answer_keeps_only_real_gaps(self):
        partially = plan(
            self.CREDIT,
            clarification_history=[self.ANSWERS[1]],  # only amount answered
        )
        assert partially.requires_clarification is True
        assert partially.missing_fields == [
            "transaction_nature", "supplier_name", "transaction_date",
        ]
        assert partially.extracted_entities.get("amount") == 150000.0


# ===================================================================
# ECONOMIC EVENT CLASSIFICATION (first-class stage, pre-tool-selection)
# ===================================================================

from app.reasoning import (  # noqa: E402
    EconomicEvent,
    build_event_profile,
    classify_economic_event,
    prohibited_tool_names,
    resolve_existence_matrix,
)


class TestEconomicEventClassification:

    @pytest.mark.parametrize("intent,expected", [
        ("record_cash_purchase", EconomicEvent.ACQUISITION),
        ("record_credit_purchase", EconomicEvent.ACQUISITION),
        ("record_purchase", EconomicEvent.ACQUISITION),
        ("record_cash_sale", EconomicEvent.DISPOSAL),
        ("record_credit_sale", EconomicEvent.DISPOSAL),
        ("record_expense", EconomicEvent.EXPENDITURE),
        ("record_receipt", EconomicEvent.SETTLEMENT_IN),
        ("record_payment", EconomicEvent.SETTLEMENT_OUT),
        ("record_expense_payment", EconomicEvent.SETTLEMENT_OUT),
        ("record_bank_transfer", EconomicEvent.INTERNAL_TRANSFER),
        ("create_credit_note", EconomicEvent.RETURN_OUT),
        ("create_purchase_return", EconomicEvent.RETURN_IN),
        ("create_quotation", EconomicEvent.DOCUMENT),
        ("create_customer", EconomicEvent.RECORD_CREATION),
        ("create_supplier", EconomicEvent.RECORD_CREATION),
        ("create_bank_account", EconomicEvent.RECORD_CREATION),
        ("generate_trial_balance", EconomicEvent.REPORTING),
        ("customer_balance", EconomicEvent.REPORTING),
    ])
    def test_intent_maps_to_economic_event(self, intent, expected):
        assert classify_economic_event(intent) is expected

    def test_unknown_intent_is_unknown_not_guessed(self):
        assert classify_economic_event("totally_new_module") is EconomicEvent.UNKNOWN


class TestEventProfiles:

    def test_cash_purchase_prohibits_party_ledger(self):
        profile = build_event_profile("record_cash_purchase")
        assert profile.event is EconomicEvent.ACQUISITION
        assert profile.affected["party_ledger"] is False
        assert profile.affected["ar_ap"] is False
        assert any(
            p.action == "party_ledger_creation" for p in profile.prohibited
        )
        assert prohibited_tool_names(profile) == {
            "create_supplier", "create_customer",
        }

    def test_credit_purchase_requires_party_and_ar_ap(self):
        profile = build_event_profile("record_credit_purchase")
        assert profile.affected["party_ledger"] is True
        assert profile.affected["ar_ap"] is True
        assert not any(
            p.action == "party_ledger_creation" for p in profile.prohibited
        )

    def test_service_nature_prohibits_inventory(self):
        """A service sale has a fundamentally different inventory impact:
        stock movements are explicitly forbidden."""
        cls = TransactionClassification(
            transaction_nature="SERVICE",
            confidence="HIGH",
            source="DETERMINISTIC_RULE",
        )
        profile = build_event_profile("record_cash_sale", classification=cls)
        assert profile.affected["inventory"] is False
        assert any(p.action == "inventory_movement" for p in profile.prohibited)

    def test_expense_nature_prohibits_capitalisation(self):
        """Normal stationery/consumables are expenses — never fixed assets,
        never manufactured inventory records."""
        cls = TransactionClassification(
            transaction_nature="OPERATING_EXPENSE",
            confidence="HIGH",
            source="DETERMINISTIC_RULE",
        )
        profile = build_event_profile("record_expense", classification=cls)
        assert profile.affected["inventory"] is False
        assert profile.affected["fixed_asset"] is False
        assert profile.affected["expense"] is True
        assert any(
            p.action == "fixed_asset_capitalization"
            for p in profile.prohibited
        )

    def test_inventory_nature_enables_stock_and_disables_expense(self):
        """Inventory resale purchase: stock balance yes, expense at
        acquisition no — the balance-sheet purpose differs from expense."""
        cls = TransactionClassification(
            transaction_nature="INVENTORY",
            confidence="HIGH",
            source="USER_ANSWER",
        )
        profile = build_event_profile("record_cash_purchase", classification=cls)
        assert profile.affected["inventory"] is True
        assert profile.affected["expense"] is False

    def test_fixed_asset_nature_is_not_inventory(self):
        """'Expensive' never implies fixed asset, and a fixed asset is
        never inventory — both directions are prohibited."""
        cls = TransactionClassification(
            transaction_nature="FIXED_ASSET",
            confidence="HIGH",
            source="ITEM_MAPPING",
        )
        profile = build_event_profile("record_credit_purchase", classification=cls)
        assert profile.affected["fixed_asset"] is True
        assert profile.affected["inventory"] is False
        assert profile.affected["expense"] is False
        assert any(p.action == "inventory_movement" for p in profile.prohibited)

    def test_reporting_event_prohibits_all_mutations(self):
        profile = build_event_profile("generate_trial_balance")
        blocked = prohibited_tool_names(profile)
        assert "record_cash_sale" in blocked
        assert "create_supplier" in blocked
        assert "post_journal" in blocked
        assert "get_trial_balance" not in blocked  # read-only stays allowed

    def test_cash_sale_party_is_informational_not_required(self):
        profile = build_event_profile("record_cash_sale")
        assert profile.affected["party_ledger"] is False
        assert profile.affected["revenue"] is True
        assert profile.affected["journal"] is True


class TestExistenceMatrix:
    """The universal EXISTS × REQUIRED resolution matrix."""

    def test_required_and_existing_is_reuse(self):
        assert resolve_existence_matrix(
            exists=True, required=True
        ) is FieldResolutionState.RESOLVABLE_FROM_ERP

    def test_required_and_missing_is_create_or_ask(self):
        assert resolve_existence_matrix(
            exists=False, required=True
        ) is FieldResolutionState.MISSING_AND_REQUIRED

    def test_not_required_is_do_nothing(self):
        assert resolve_existence_matrix(
            exists=True, required=False
        ) is FieldResolutionState.MISSING_BUT_OPTIONAL
        assert resolve_existence_matrix(
            exists=False, required=False
        ) is FieldResolutionState.MISSING_BUT_OPTIONAL

    def test_ambiguous_existing_is_resolve_not_guess(self):
        assert resolve_existence_matrix(
            exists=True, required=True, ambiguous=True
        ) is FieldResolutionState.AMBIGUOUS

    def test_invalid_existing_is_fix_or_ask(self):
        assert resolve_existence_matrix(
            exists=True, required=True, invalid=True
        ) is FieldResolutionState.INVALID

    def test_missing_configuration_is_gap(self):
        assert resolve_existence_matrix(
            exists=False, required=True, invalid=True
        ) is FieldResolutionState.BLOCKED_BY_CONFIGURATION


class TestPlanCarriesEconomicEvent:

    def test_planner_classifies_event_before_tools(self):
        """The plan must carry the economic event + impact map + prohibited
        actions — classification happens BEFORE tool selection."""
        p = plan("I bought a Dell laptop for Rs.150,000 in cash.")
        assert p.intent == "record_cash_purchase"
        assert p.economic_event == EconomicEvent.ACQUISITION.value
        assert p.impact_map.get("party_ledger") is False
        assert p.impact_map.get("journal") is True
        assert any(
            a["action"] == "party_ledger_creation" for a in p.prohibited_actions
        )

    def test_credit_sale_plan_requires_party_dimension(self):
        p = plan("I sold services to XYZ Corp for Rs.200,000 on credit.")
        assert p.economic_event == EconomicEvent.DISPOSAL.value
        assert p.impact_map.get("party_ledger") is True
        assert p.impact_map.get("ar_ap") is True




        assert party_resolved_in_search("unexpected-shape", "XYZ Computers") is False
