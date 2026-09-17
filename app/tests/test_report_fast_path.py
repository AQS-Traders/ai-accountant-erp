"""Work Stream B - deterministic report fast-path tests."""

import asyncio
import uuid

import app.agent as agent
from app.models.schemas import ExecutionPlan, ExecutionStatus, ToolResult

ORG = uuid.uuid4()
USER = uuid.uuid4()
SESSION = uuid.uuid4()


def _plan(intent, entities=None):
    return ExecutionPlan(
        intent=intent,
        extracted_entities=entities or {},
        requires_clarification=False,
    )


def _run(monkeypatch, plan, route_impl):
    """Run the fast-path with a mocked router + no-op persistence."""
    steps = []

    async def fake_route(call, **kw):
        return await route_impl(call)

    async def fake_log(session_id, phase, data):
        steps.append((phase, data))

    async def fake_status(session_id, status):
        pass

    async def fake_result(**kw):
        return {"id": "er1"}

    monkeypatch.setattr(agent, "route_tool_call", fake_route)
    monkeypatch.setattr(agent, "_log_step", fake_log)
    monkeypatch.setattr(agent, "_update_status", fake_status)
    monkeypatch.setattr(agent, "create_execution_result", fake_result)
    res = asyncio.run(agent._try_report_fast_path(
        execution_plan=plan,
        session_id=SESSION,
        organization_id=ORG,
        user_id=USER,
        auth=None,
    ))
    return res, steps


class TestDirectReportFastPath:
    def test_trial_balance_skips_llm_and_formats_rows(self, monkeypatch):
        rows = [
            {"account_code": "1010", "account_name": "Cash", "debit": 12000.0},
            {"account_code": "4000", "account_name": "Sales", "credit": 12000.0},
        ]

        async def route(call):
            assert call.tool_name == "get_trial_balance"
            return ToolResult(tool_name=call.tool_name, success=True, data=rows)

        res, steps = _run(monkeypatch, _plan("generate_trial_balance"), route)
        assert res is not None
        assert res.status == ExecutionStatus.COMPLETED
        # Honest step data: no LLM was used.
        assert res.data["bypass_llm"] is True
        assert res.data["rows"] == 2
        assert any(
            phase == "EXECUTING_TOOLS" and data.get("BYPASS_LLM") is True
            for phase, data in steps
        )
        assert "1010 Cash" in res.summary
        assert "12,000.00" in res.summary
        assert "deterministic fast-path" in res.summary

    def test_fast_path_data_equals_tool_path(self, monkeypatch):
        """The fast-path returns the SAME rows the tool path would."""
        rows = [{"account_code": "1010", "account_name": "Cash", "debit": 500}]

        async def route(call):
            return ToolResult(tool_name=call.tool_name, success=True, data=rows)

        res, _ = _run(monkeypatch, _plan("generate_trial_balance"), route)
        assert res.data["rows"] == 1

    def test_failed_tool_report_fails(self, monkeypatch):
        async def route(call):
            return ToolResult(
                tool_name=call.tool_name, success=False, error="db down"
            )

        res, _ = _run(monkeypatch, _plan("generate_balance_sheet"), route)
        assert res is not None
        assert res.status == ExecutionStatus.FAILED
        assert "db down" in res.summary

    def test_non_report_intent_returns_none(self, monkeypatch):
        async def route(call):  # pragma: no cover - must not be called
            raise AssertionError("router must not be called")

        res, _ = _run(monkeypatch, _plan("record_expense"), route)
        assert res is None


class TestPartyLedgerFastPath:
    def test_unique_exact_match_runs_ledger(self, monkeypatch):
        seen = []

        async def route(call):
            seen.append(call.tool_name)
            if call.tool_name == "search_customer":
                return ToolResult(
                    tool_name="search_customer", success=True,
                    data=[
                        {"id": "c1", "name": "ABC Traders"},
                        {"id": "c2", "name": "ABC Traders Ltd"},  # not exact
                    ],
                )
            assert call.arguments["customer_id"] == "c1"
            return ToolResult(
                tool_name="get_customer_ledger", success=True,
                data=[{"description": "invoice", "debit": 900}],
            )

        res, _ = _run(
            monkeypatch,
            _plan("customer_balance", {"customer_name": "abc traders"}),
            route,
        )
        assert res is not None
        assert res.status == ExecutionStatus.COMPLETED
        assert seen == ["search_customer", "get_customer_ledger"]

    def test_ambiguous_matches_never_first_hit(self, monkeypatch):
        async def route(call):
            if call.tool_name == "search_customer":
                return ToolResult(
                    tool_name="search_customer", success=True,
                    data=[
                        {"id": "c1", "name": "ABC Traders"},
                        {"id": "c2", "name": "abc traders"},  # duplicate name
                    ],
                )
            raise AssertionError("ledger must not run on ambiguity")

        res, _ = _run(
            monkeypatch,
            _plan("customer_balance", {"customer_name": "ABC Traders"}),
            route,
        )
        assert res is None

    def test_no_party_named_returns_none(self, monkeypatch):
        async def route(call):  # pragma: no cover
            raise AssertionError("router must not be called")

        res, _ = _run(monkeypatch, _plan("customer_balance"), route)
        assert res is None

    def test_zero_matches_returns_none(self, monkeypatch):
        async def route(call):
            return ToolResult(
                tool_name="search_customer", success=True, data=[]
            )

        res, _ = _run(
            monkeypatch,
            _plan("customer_balance", {"customer_name": "Nobody"}),
            route,
        )
        assert res is None


class TestLightBudgetRouting:
    def test_light_budget_intents_cover_lookup_set(self):
        # The lookup intents that reach the model with a reduced output
        # budget.  `list_expenses` belongs to this set: it is a pure lookup
        # (see planner's "Expense LOOKUPS must win over record_expense" rule)
        # and returns a bounded row list, exactly like list_bank_accounts.
        expected = {
            "generate_trial_balance", "generate_balance_sheet",
            "generate_profit_loss", "generate_cash_flow",
            "generate_general_ledger", "list_bank_accounts",
            "list_expenses",
            "customer_balance", "supplier_balance",
        }
        assert agent._LIGHT_BUDGET_INTENTS == expected

    def test_light_budget_threaded_to_client(self, monkeypatch):
        """The orchestrator passes the reduced budget to the client."""
        captured = {}

        class FakeClient:
            async def generate_with_tools(self, **kw):
                captured.update(kw)
                return {"text": "ok", "tool_calls": [], "tool_results": []}

        from app.ai_orchestrator import (
            AIOrchestrator,
            _LIGHT_OUTPUT_BUDGET_TOKENS,
        )

        orch = AIOrchestrator()
        monkeypatch.setattr(
            orch, "_candidate_providers",
            lambda **kw: [{"provider": "fake", "model": "m",
                           "capability": "text", "factory": FakeClient}],
        )
        asyncio.run(orch.generate_with_tools(
            user_message="hi",
            context=None,
            light_budget=True,
        ))
        assert captured["max_output_tokens"] == _LIGHT_OUTPUT_BUDGET_TOKENS

    def test_standard_budget_passes_nothing(self, monkeypatch):
        captured = {}

        class FakeClient:
            async def generate_with_tools(self, **kw):
                captured.update(kw)
                return {"text": "ok", "tool_calls": [], "tool_results": []}

        from app.ai_orchestrator import AIOrchestrator

        orch = AIOrchestrator()
        monkeypatch.setattr(
            orch, "_candidate_providers",
            lambda **kw: [{"provider": "fake", "model": "m",
                           "capability": "text", "factory": FakeClient}],
        )
        asyncio.run(orch.generate_with_tools(user_message="hi", context=None))
        assert "max_output_tokens" not in captured
