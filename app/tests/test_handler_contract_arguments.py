"""The argument contract must describe the HANDLER, not only the service.

Production 2026-09-22 (org "Zameer Labs PVT Ltd"): *"I purchased two Tables for
49000 today"* was refused with

    create_purchase_bill: 'account_id' is not a parameter of this tool — the
    call would fail before anything ran. Valid parameters: bill_date, …

and the run parked in AWAITING_CLARIFICATION; every later request was then
refused with *"Your last request is still waiting for your answer"*.

The call would NOT have failed.  ``_create_purchase_bill`` pops ``account_id``
itself and hands it to the accounting engine as a JOURNAL account hint, and the
deterministic purchase fast path SETS that very argument
(app/agent.py: ``params["account_id"] = str(account_hint)``).  The contract was
derived from ``purchase_service.create_purchase_bill`` — the SERVICE the handler
forwards to — so an argument the handler legitimately consumes was reported as
unknown and the agent could not execute its own plan.

The rule pinned here: a handler-level argument is DECLARED per tool (never
inferred from ``**kwargs``), and the deterministic plans bind.

No live provider, no live database.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.plan_materialization import declared_reference_inputs
from app.tool_contract import contract_from_callable, validate_calls


def _contracts() -> Dict[str, Dict[str, Any]]:
    from app.tools import tool_contracts

    return tool_contracts()


def _reference_inputs() -> Dict[str, Dict[str, Any]]:
    return declared_reference_inputs()


# ---------------------------------------------------------------------------
# The declared per-tool handler arguments
# ---------------------------------------------------------------------------


class TestHandlerLevelArguments:
    def test_the_purchase_bill_handler_argument_is_declared(self):
        contract = _contracts()["create_purchase_bill"]

        assert "account_id" in contract["accepted"]

    def test_the_expense_handler_argument_is_declared(self):
        contract = _contracts()["create_expense"]

        assert "account_id" in contract["accepted"]

    def test_the_journal_hint_is_optional_never_required(self):
        for slug in ("create_purchase_bill", "create_expense"):
            assert "account_id" not in _contracts()[slug]["required"], slug

    def test_an_undeclared_extra_argument_is_still_rejected(self):
        """Declaration is per tool — create_invoice has no such handler input."""
        violations = validate_calls(
            [{
                "tool_name": "create_invoice",
                "arguments": {"customer_id": "c1", "items": [], "account_id": "a1"},
            }],
            _contracts(),
            _reference_inputs(),
        )

        assert any("'account_id'" in v for v in violations)

    def test_a_master_data_tool_still_rejects_it(self):
        violations = validate_calls(
            [
                {
                    "tool_name": "create_customer",
                    "arguments": {"name": "X", "account_id": "a1"},
                }
            ],
            _contracts(),
            _reference_inputs(),
        )

        assert any("'account_id'" in v for v in violations)

    def test_declaring_a_name_does_not_weaken_the_unknown_key_check(self):
        contract = _contracts()["create_purchase_bill"]

        assert "amount" not in contract["accepted"]
        assert "payee_name" not in contract["accepted"]

    def test_the_derivation_helper_is_explicit(self):
        async def service(*, organization_id, bill_date, items=None):
            ...

        contract = contract_from_callable(service, extra_arguments=("account_id",))

        assert set(contract["accepted"]) == {"bill_date", "items", "account_id"}
        assert contract["required"] == ("bill_date",)


# ---------------------------------------------------------------------------
# The plans the deterministic paths build must BIND
# ---------------------------------------------------------------------------


class TestDeterministicPlansBind:
    @pytest.mark.asyncio
    async def test_the_purchase_fast_path_plan_binds(self, monkeypatch):
        """The exact plan app/agent.py builds for a confirmed credit purchase.

        Drives the REAL deterministic builder (``_deterministic_mutation_calls``
        -> the ``record_credit_purchase`` branch that sets
        ``params["account_id"]``) and then applies the same contract check the
        plan-materialization guard runs before execution.
        """
        import app.agent as agent_mod
        from app.services import supplier_service

        async def fake_search(organization_id, query=None, limit=5):
            return []

        async def fake_create(organization_id, **kw):
            return {"id": "sup-1", "name": kw.get("name")}

        monkeypatch.setattr(supplier_service, "search", fake_search)
        monkeypatch.setattr(supplier_service, "create", fake_create)

        plan = agent_mod.ExecutionPlan(
            intent="record_credit_purchase",
            entity_type="supplier",
            entity_name="FurnishMart",
            transaction_type="CREDIT_PURCHASE",
            potential_tools=["search_supplier", "create_purchase_bill"],
            requires_validation=True,
            requires_accounting_engine=True,
            requires_confirmation=True,
            requires_clarification=False,
            missing_fields=[],
            clarification_questions=[],
            expected_outcome="Purchase bill recorded",
            extracted_entities={
                "supplier_name": "FurnishMart",
                "supplier_create_confirmed": True,
                "amount": 49000,
                "transaction_date": "2026-09-22",
                "line_items": [
                    {"description": "Tables", "quantity": 2, "unit_price": 24500}
                ],
            },
            economic_event="CREDIT_PURCHASE",
            impact_map={},
            prohibited_actions=[],
            transaction_nature="OPERATING_EXPENSE",
            transaction_nature_source="DETERMINISTIC_RULE",
            batch_items=None,
        )
        classification = SimpleNamespace(
            account_hint_id="acct-hint-1", requires_clarification=False
        )

        calls = await agent_mod._deterministic_mutation_calls(
            organization_id="org-1",
            execution_plan=plan,
            classification=classification,
        )

        assert calls and calls[0].tool_name == "create_purchase_bill"
        arguments = dict(calls[0].arguments)
        assert arguments["account_id"] == "acct-hint-1", (
            "the deterministic purchase path sets the journal account hint"
        )

        violations = validate_calls(
            [{"tool_name": "create_purchase_bill", "arguments": arguments}],
            _contracts(),
            _reference_inputs(),
        )

        assert violations == [], violations

    def test_the_expense_fast_path_arguments_bind(self):
        """The argument set app/agent.py builds for a fast-pathed expense."""
        arguments = {
            "expense_date": "2026-09-22",
            "payee_name": "Internet Provider",
            "description": "internet",
            "subtotal": 5000,
            "total": 5000,
            "payment_mode": "CASH",
            "account_id": "acct-hint-1",  # set when the classifier resolved one
        }

        assert validate_calls(
            [{"tool_name": "create_expense", "arguments": arguments}],
            _contracts(),
            _reference_inputs(),
        ) == []




# ---------------------------------------------------------------------------
# Drift guard: a handler argument can never be consumed without being declared
# ---------------------------------------------------------------------------


def _handler_popped_arguments() -> Dict[str, List[str]]:
    """handler function name -> the argument names it pops from **kw."""
    import app.tools as tools_mod

    source = inspect.getsource(tools_mod)
    current = None
    popped: Dict[str, List[str]] = {}
    for line in source.splitlines():
        match = re.match(r"\s*(?:async )?def (\w+)\(", line)
        if match:
            current = match.group(1)
            popped.setdefault(current, [])
            continue
        if current is not None:
            for name in re.findall(r"kw\.pop\(\s*[\"']([^\"']+)[\"']", line):
                if name not in popped[current]:
                    popped[current].append(name)
    return popped


def test_every_handler_consumed_argument_is_declared():
    """A future ``kw.pop("x")`` without a declared contract fails here."""
    import app.tools as tools_mod

    popped = _handler_popped_arguments()
    undeclared = []

    for slug, entry in tools_mod._TOOL_REGISTRY.items():
        handler = entry.get("handler")
        contract = entry.get("contract")
        if handler is None or not contract:
            continue
        for name in popped.get(handler.__name__, ()):
            if name not in contract["accepted"]:
                undeclared.append((slug, name))

    assert undeclared == [], (
        "these handler arguments are consumed but not declared in the tool "
        "contract, so a valid call would be rejected: %s" % undeclared
    )
