"""PR-C — plan completeness + declared-alias reconciliation.

Two production lessons, pinned here
==================================

**1. A plan that performs no financial mutation must never be approved.**
Production 2026-09-22 (session 0076d9a0, user zameerchattha3@gmail.com,
"create an invoice for fds labs pvt for selling 3 Desks on Credit for 60000"):
the model's proposals were rejected by the argument-contract gate three rounds
running, the bounded reasoning budget was exhausted, and the deterministic
fallback snapshotted a *preparatory* plan —

    [create_customer("FDS Labs Pvt"), search_product("Desks")]

— which the user approved.  The customer was written, no invoice could be built,
and the run ended FAILED (honestly, thanks to #9, but after creating state the
user never asked for).  ``_plan_performs_no_financial_mutation`` now refuses to
snapshot such a plan and asks instead.

**2. The gate must not forbid the aliases the materializer resolves.**
The same session showed the deadlock: a model cannot supply ``customer_id`` for
a customer that does not exist yet, and inventing one is forbidden — the only
shape it can express is ``customer_name`` / ``items[].product_name``, which the
contract gate rejected outright while plan materialization existed to convert
exactly those into ids.  Declared reference inputs now pass the gate and are
resolved before execution (``reference_inputs``).

No live provider, no live database.
"""

from __future__ import annotations

from typing import Any, Dict

from app.agent import _plan_performs_no_financial_mutation
from app.models.schemas import ToolCall, ToolResult
from app.plan_materialization import declared_reference_inputs
from app.tool_contract import validate_arguments, validate_calls


def _calls(*specs):
    return [ToolCall(tool_name=name, arguments=args) for name, args in specs]


def _contracts() -> Dict[str, Dict[str, Any]]:
    from app.tools import tool_contracts

    return tool_contracts()


# ---------------------------------------------------------------------------
# 1. Plan completeness — refuse to approve an operation that cannot happen
# ---------------------------------------------------------------------------


class TestPlanCompleteness:
    def test_the_incident_plan_is_refused(self):
        """create_customer + search_product for a create_invoice intent."""
        calls = _calls(
            ("create_customer", {"name": "FDS Labs Pvt"}),
            ("search_product", {"query": "Desks"}),
        )

        assert _plan_performs_no_financial_mutation(
            intent="create_invoice", calls=calls
        ) is True

    def test_a_plan_with_the_primary_operation_passes(self):
        calls = _calls(
            ("create_customer", {"name": "FDS Labs Pvt"}),
            ("create_invoice", {"customer_name": "FDS Labs Pvt", "items": []}),
        )

        assert _plan_performs_no_financial_mutation(
            intent="create_invoice", calls=calls
        ) is False

    def test_a_different_financial_operation_is_a_reinterpretation_not_a_gap(self):
        """The model may legitimately decide the sale is a cash sale."""
        calls = _calls(("record_cash_sale", {"amount": 60000}))

        assert _plan_performs_no_financial_mutation(
            intent="create_invoice", calls=calls
        ) is False

    def test_non_financial_intents_are_never_gated(self):
        calls = _calls(("get_trial_balance", {}))

        assert _plan_performs_no_financial_mutation(
            intent="generate_trial_balance", calls=calls
        ) is False

    def test_empty_intent_is_never_gated(self):
        calls = _calls(("search_customer", {"query": "x"}))

        assert _plan_performs_no_financial_mutation(intent="", calls=calls) is False

    def test_the_gate_runs_before_the_confirmation_snapshot(self):
        """Source guard: the completeness check must precede create_confirmation."""
        import inspect

        import app.agent as agent_mod

        src = inspect.getsource(agent_mod)
        gate = src.index("_plan_performs_no_financial_mutation(")
        snapshot = src.index("confirmation = await create_confirmation(")
        assert gate < snapshot, (
            "the plan-completeness gate must run BEFORE the plan is snapshotted "
            "for approval"
        )


# ---------------------------------------------------------------------------
# 2. Declared aliases pass the gate (and are still resolved before execution)
# ---------------------------------------------------------------------------


class TestDeclaredAliasInputs:
    def test_declared_inputs_are_exposed_per_tool(self):
        declared = declared_reference_inputs()

        assert declared["create_invoice"]["customer_id"] == (
            "customer_name", "party_name",
        )
        assert declared["create_invoice"]["items"] == ("line_items",)
        assert declared["create_purchase_bill"]["supplier_id"] == (
            "supplier_name", "party_name",
        )
        assert declared["create_expense"]["supplier_id"] == (
            "supplier_name", "payee_name",
        )

    def test_an_alias_satisfies_unknown_and_required_checks(self):
        contract = _contracts()["create_invoice"]
        reference_inputs = declared_reference_inputs()["create_invoice"]

        violations = validate_arguments(
            "create_invoice",
            {
                "customer_name": "FDS Labs Pvt",
                "items": [
                    {"description": "Desks", "quantity": 3, "unit_price": 20000}
                ],
            },
            contract,
            reference_inputs,
        )

        assert violations == [], violations

    def test_without_reference_inputs_the_alias_is_still_rejected(self):
        """Pins the pre-fix behaviour this PR changes deliberately."""
        contract = _contracts()["create_invoice"]

        violations = validate_arguments(
            "create_invoice",
            {"customer_name": "FDS Labs Pvt", "items": []},
            contract,
        )

        assert any("'customer_name'" in v for v in violations)
        assert any("'customer_id' is missing" in v for v in violations)

    def test_a_truly_invented_key_is_still_rejected(self):
        contract = _contracts()["create_invoice"]
        reference_inputs = declared_reference_inputs()["create_invoice"]

        violations = validate_arguments(
            "create_invoice",
            {"customer_id": "c1", "amount": 60000, "items": []},
            contract,
            reference_inputs,
        )

        assert any("'amount'" in v for v in violations)

    def test_validate_calls_threads_reference_inputs_per_tool(self):
        calls = [
            {"tool_name": "create_customer", "arguments": {"name": "FDS Labs Pvt"}},
            {
                "tool_name": "create_invoice",
                "arguments": {"customer_name": "FDS Labs Pvt", "line_items": []},
            },
        ]

        violations = validate_calls(calls, _contracts(), declared_reference_inputs())

        assert violations == [], violations

    def test_the_reasoning_gate_accepts_the_natural_proposal(self):
        """The proposal the production gate rejected three times now passes."""
        from app.accounting_reasoning import _outcome_from_parsed, validate_outcome
        from app.tools import tool_contracts

        outcome = _outcome_from_parsed(
            {
                "understanding": {"economic_event": "credit sale of desks"},
                "proposal": {
                    "interpretation": "Credit sale: Dr Receivable / Cr Desks Sales.",
                    "affected_records": ["fds labs pvt", "invoice"],
                    "accounting_impact": [
                        {"account": "Accounts Receivable", "debit": 60000},
                        {"account": "Desks Sales", "credit": 60000},
                    ],
                    "not_affected": ["cash/bank"],
                    "unresolved_uncertainty": [],
                    "confirmation": "Create the customer and issue the invoice?",
                    "tools": [
                        {
                            "tool_name": "create_customer",
                            "arguments": {"name": "FDS Labs Pvt"},
                        },
                        {
                            "tool_name": "create_invoice",
                            "arguments": {
                                "customer_name": "FDS Labs Pvt",
                                "line_items": [
                                    {
                                        "description": "Desks",
                                        "quantity": 3,
                                        "unit_price": 20000,
                                    }
                                ],
                            },
                        },
                    ],
                },
            },
            rounds=1,
        )

        violations = validate_outcome(
            outcome,
            offered_tools=("create_customer", "create_invoice"),
            tool_contracts=tool_contracts(),
        )

        assert violations == [], violations

    def test_the_reasoning_gate_still_rejects_invented_keys(self):
        from app.accounting_reasoning import _outcome_from_parsed, validate_outcome
        from app.tools import tool_contracts

        outcome = _outcome_from_parsed(
            {
                "understanding": {"economic_event": "credit sale of desks"},
                "proposal": {
                    "interpretation": "Credit sale of desks.",
                    "affected_records": ["invoice"],
                    "accounting_impact": [{"account": "Receivable", "debit": 60000}],
                    "not_affected": [],
                    "unresolved_uncertainty": [],
                    "confirmation": "Issue the invoice?",
                    "tools": [
                        {
                            "tool_name": "create_invoice",
                            "arguments": {
                                "amount": 60000,
                                "item_description": "Desks",
                            },
                        }
                    ],
                },
            },
            rounds=1,
        )

        violations = validate_outcome(
            outcome,
            offered_tools=("create_invoice",),
            tool_contracts=tool_contracts(),
        )

        message = " ".join(violations)
        assert "'amount'" in message
        assert "'item_description'" in message


# ---------------------------------------------------------------------------
# 3. Intent vocabulary vs tool vocabulary (never conflated)
# ---------------------------------------------------------------------------


class TestIntentToolVocabulary:
    """`record_expense` is an INTENT; the TOOL that records it is `create_expense`.

    Comparing the intent string with a tool-result name matched nothing, so
    PHASE 7 (`required_failures`) synthesized "primary operation did not
    execute" and PHASE 8 (`verified`) returned False for a RECORDED expense:
    the expense and its journal were in the books while the run was closed
    FAILED.  These tests pin the corrected vocabulary.
    """

    def test_the_intent_resolves_to_its_tool(self):
        from app.agent import _intent_tool_names

        assert "create_expense" in _intent_tool_names("record_expense")
        assert "create_invoice" in _intent_tool_names("record_sale")
        assert "create_invoice" in _intent_tool_names("record_credit_sale")
        assert "create_invoice" in _intent_tool_names("create_invoice")

    def test_an_intent_name_is_never_mistaken_for_a_tool(self):
        from app.agent import _intent_tool_names

        for intent in ("record_expense", "record_sale", "record_credit_sale"):
            assert intent not in _intent_tool_names(intent)

    def test_a_non_financial_intent_has_no_financial_tools(self):
        from app.agent import _intent_tool_names

        assert _intent_tool_names("generate_trial_balance") == frozenset()
        assert _intent_tool_names("") == frozenset()

    def test_a_recorded_expense_is_not_a_failure(self):
        from app.agent import _primary_mutation_failed

        results = [
            ToolResult(tool_name="search_account", success=True, data={}),
            ToolResult(tool_name="create_expense", success=True, data={"id": "exp-1"}),
        ]

        assert _primary_mutation_failed(
            results, intent="record_expense"
        ) is False, "a RECORDED expense must never be reported as a failed run"


    def test_an_expense_that_never_ran_is_a_failure(self):
        from app.agent import _primary_mutation_failed

        results = [ToolResult(tool_name="search_account", success=True, data={})]

        assert _primary_mutation_failed(results, intent="record_expense") is True

    def test_a_successful_auxiliary_never_satisfies_a_sale_intent(self):
        from app.agent import _primary_mutation_failed

        results = [
            ToolResult(tool_name="create_customer", success=True, data={"id": "c1"})
        ]

        assert _primary_mutation_failed(results, intent="record_sale") is True
        assert _primary_mutation_failed(results, intent="create_invoice") is True

    def test_an_expense_plan_performs_a_financial_mutation(self):
        from app.agent import _plan_performs_no_financial_mutation

        calls = _calls(("create_expense", {"subtotal": 5000, "description": "internet"}))

        assert _plan_performs_no_financial_mutation(
            intent="record_expense", calls=calls
        ) is False, "an expense plan DOES perform the user's operation"

    def test_an_expense_plan_without_the_mutation_is_refused(self):
        from app.agent import _plan_performs_no_financial_mutation

        calls = _calls(("search_account", {"query": "internet"}))

        assert _plan_performs_no_financial_mutation(
            intent="record_expense", calls=calls
        ) is True

    def test_a_failed_expense_call_is_operation_level_not_recoverable(self):
        from app.agent import _financial_mutation_failures

        results = [
            ToolResult(tool_name="create_expense", success=False, error="boom"),
            ToolResult(tool_name="search_account", success=True, data={}),
        ]

        failures = _financial_mutation_failures(results)

        assert [tr.tool_name for tr in failures] == ["create_expense"]

    def test_the_write_vocabulary_is_tool_slugs_only(self):
        from app.idempotency import (
            FINANCIAL_MUTATION_TOOLS,
            FINANCIAL_WRITE_TOOLS,
            INTENT_ONLY_NAMES,
        )

        assert "create_expense" in FINANCIAL_WRITE_TOOLS
        assert not (FINANCIAL_WRITE_TOOLS & INTENT_ONLY_NAMES)
        # The claim set is unchanged: widening it is a separate concern.
        assert FINANCIAL_MUTATION_TOOLS - INTENT_ONLY_NAMES <= FINANCIAL_WRITE_TOOLS

    def test_no_predicate_compares_an_intent_string_with_a_tool_name(self):
        """Source guard: the intent is never matched against a result name."""
        import inspect

        import app.agent as agent_mod

        src = inspect.getsource(agent_mod)

        assert "tool_name == execution_plan.intent" not in src
        assert "_intent_tool_names(" in src

    def test_a_failed_expense_is_a_failure(self):
        from app.agent import _primary_mutation_failed

        results = [ToolResult(tool_name="create_expense", success=False, error="boom")]

        assert _primary_mutation_failed(results, intent="record_expense") is True

