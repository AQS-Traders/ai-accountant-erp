"""Regression tests — reasoning round exhaustion.

Production incident a59b889c-dc26-4ba6-a90a-e0cb6085dacf
=========================================================

The LLM accounting reasoning loop spent its whole round budget asking for
``bank_accounts`` evidence, the books kept answering EMPTY, the loop reached
its intended terminal round-exhaustion state — but ``run_reasoning_loop()``
fell off the end of the function and implicitly returned ``None``.  The
caller then executed ``_reasoning.as_dict()`` on that ``None``:

    AttributeError: 'NoneType' object has no attribute 'as_dict'

and the generic top-level handler surfaced it as "An unexpected error
occurred. Please try again."

These tests pin the fix:

* every reachable terminal path of ``run_reasoning_loop()`` returns a
  populated outcome — round exhaustion included (Tests A and B);
* an unexpectedly-``None`` reasoning result is converted into a CONTROLLED
  failure by the caller — no planning, no confirmation snapshot, no tool
  execution, no accounting mutation (Test C);
* an unhandled exception writes a DURABLE ``FAILED`` execution step, so the
  execution timeline never silently ends at the previous successful step
  (Test D).

The provider is simulated with a scripted double — no live model, no live
database.  The architecture is unchanged: the LLM still decides, Python still
enforces; the fix is in terminal outcome propagation only.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.accounting_reasoning as ar
import app.agent as agent_mod
import app.books_evidence as be
from app.models.schemas import ExecutionStatus

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
PAID_ABC = "paid 50,000 to ABC"
INVOICE_REQUEST = (
    "create an invoice for abc furnitures for selling them 2 chairs "
    "amounting to 25 000"
)


class _ReasoningSettings:
    """Settings stub: generous budgets, no dedicated model chain."""

    accounting_reasoning_timeout = 5.0
    accounting_reasoning_total_timeout = 10.0
    accounting_reasoning_model_chain = ""


class _AgentSettings(_ReasoningSettings):
    """Settings stub with the reasoning stage ENABLED (as in production)."""

    accounting_reasoning_enabled = True


class _RepeatingReasoning:
    """Provider double: answers EVERY round with the same scripted reply.

    Round exhaustion needs a model that keeps requesting the same evidence —
    exactly the production behaviour: the model asked for ``bank_accounts``
    and the books kept answering empty, round after round.
    """

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def generate_text(self, *, prompt: str = ""):
        self.calls += 1
        return json.dumps(self.reply)


def _fake_kind(loader, *, kind, permission_slug):
    """A stand-in evidence kind registered under its production name."""
    return be.EvidenceKind(
        kind=kind,
        title=f"{kind} title",
        description=f"{kind} description",
        args={},
        permission_slug=permission_slug,
        loader=loader,
    )


# ---------------------------------------------------------------------------
# Tests A and B — run_reasoning_loop() round exhaustion returns an outcome
# ---------------------------------------------------------------------------


class TestRoundExhaustionReturnsOutcome:
    def _facts(self, message=PAID_ABC):
        return ar.ReasoningFacts(
            user_request=message,
            preliminary=ar.preliminary_extraction(message),
            today="2026-09-19",
        )

    @pytest.mark.asyncio
    async def test_a_round_budget_exhaustion_returns_populated_outcome(
        self, monkeypatch
    ):
        """Every permitted round answers NEEDS_EVIDENCE for the same kind;
        when ``max_rounds`` is exhausted the loop must still return a
        populated terminal outcome — never ``None``."""

        async def _empty_loader(organization_id, **kw):
            return []

        monkeypatch.setitem(
            be.EVIDENCE_KINDS,
            "chart_of_accounts",
            _fake_kind(
                _empty_loader,
                kind="chart_of_accounts",
                permission_slug="get_chart_of_accounts",
            ),
        )
        monkeypatch.setattr(
            "app.config.get_settings", lambda: _ReasoningSettings()
        )
        orch = _RepeatingReasoning(
            {
                "evidence_requests": [
                    {
                        "kind": "chart_of_accounts",
                        "why": "map the request to real accounts",
                    }
                ]
            }
        )

        outcome = await ar.run_reasoning_loop(
            self._facts(),
            organization_id=ORG,
            orchestrator=orch,
            max_rounds=3,
        )

        # The contract: a populated outcome on every reachable terminal path.
        assert outcome is not None
        assert orch.calls == 3, "every permitted round must have been used"
        assert outcome.rounds == 3
        assert outcome.status == ar.UNSUPPORTED
        # The provider was healthy — exhaustion is not a provider failure.
        assert outcome.provider_failed is False
        assert outcome.usable is True
        # Reasoning/evidence metadata stays intact.
        assert len(outcome.evidence_results) == 3
        assert all(r.empty for r in outcome.evidence_results)
        assert outcome.violations == []
        # The exact production crash site must work.
        serialized = outcome.as_dict()
        assert serialized["status"] == ar.UNSUPPORTED
        assert serialized["rounds"] == 3

    @pytest.mark.asyncio
    async def test_b_production_shape_bank_accounts_exhaustion_returns_outcome(
        self, monkeypatch
    ):
        """The EXACT production shape:

            round 1 -> NEEDS_EVIDENCE(bank_accounts) -> empty evidence
            round 2 -> NEEDS_EVIDENCE(bank_accounts) -> empty evidence
            round 3 -> NEEDS_EVIDENCE(bank_accounts) -> empty evidence
            max_rounds = 3

        ``run_reasoning_loop()`` used to fall off the end and return ``None``;
        the caller then crashed on ``.as_dict()``.  This test is the
        production incident pinned forever."""

        async def _empty_bank_loader(organization_id, **kw):
            return []

        monkeypatch.setitem(
            be.EVIDENCE_KINDS,
            "bank_accounts",
            _fake_kind(
                _empty_bank_loader,
                kind="bank_accounts",
                permission_slug="list_bank_accounts",
            ),
        )
        monkeypatch.setattr(
            "app.config.get_settings", lambda: _ReasoningSettings()
        )
        orch = _RepeatingReasoning(
            {
                "evidence_requests": [
                    {
                        "kind": "bank_accounts",
                        "why": "settlement needs an existing bank account",
                    }
                ]
            }
        )

        outcome = await ar.run_reasoning_loop(
            self._facts(INVOICE_REQUEST),
            organization_id=ORG,
            orchestrator=orch,
            max_rounds=3,
        )

        assert outcome is not None, (
            "round exhaustion must return a terminal outcome, never None"
        )
        assert outcome.status == ar.UNSUPPORTED
        assert outcome.rounds == 3
        assert outcome.provider_failed is False
        assert len(outcome.evidence_results) == 3
        assert all(r.kind == "bank_accounts" for r in outcome.evidence_results)
        # The exact production crash site.
        assert outcome.as_dict()["status"] == ar.UNSUPPORTED



# ---------------------------------------------------------------------------
# Agent-level wiring — controlled failure, durable timeline
# ---------------------------------------------------------------------------


def _stub_context():
    """Attribute bag mirroring AgentContext for the deterministic path."""
    return SimpleNamespace(
        relevant_customers=[],
        relevant_suppliers=[],
        relevant_accounts=[],
        relevant_bank_accounts=[],
        extracted_entities={},
        classification=None,
        economic_event=None,
        impact_map={},
        prohibited_actions=[],
        preliminary_extraction={},
        live_evidence=[],
        accounting_reasoning=None,
    )


def _stub_boundaries(monkeypatch, *, steps=None):
    """Stub ONLY the process boundaries (database + provider).

    With ``steps`` given, the real ``_log_step`` durability routing is kept
    and ``create_execution_step`` records every write, so a test can prove
    the TIMELINE content.  Without it, ``_log_step`` is captured at the call
    boundary instead.
    """
    session = {
        "id": str(uuid.uuid4()),
        "organization_id": str(ORG),
        "user_id": str(USER),
        "user_request": "",
        "conversation_id": "conv-exhaustion",
        "status": "PENDING",
        "current_phase": "RECEIVED",
    }
    recorded = {
        "session": session,
        "clarifications": [],
        "confirmations": [],
        "tool_runs": [],
        "results": [],
        "log_steps": [],
    }
    # Timeline mode is when the CALLER passed a steps list — decide BEFORE
    # defaulting, or ``[] is not None`` would make the flag always True.
    timeline_mode = steps is not None
    if steps is None:
        steps = []
    recorded["steps"] = steps

    async def fake_create_session(**kw):
        session["user_request"] = kw.get("user_message", "")
        return session

    async def fake_create_clarification(**kw):
        recorded["clarifications"].append(kw)
        return {"id": f"clar-{len(recorded['clarifications'])}", **kw}

    async def fake_create_confirmation(**kw):
        recorded["confirmations"].append(kw)
        return {"id": f"conf-{len(recorded['confirmations'])}", **kw}

    async def fake_execute_planned(tool_calls, executor):
        for tc in tool_calls:
            recorded["tool_runs"].append(
                (tc.tool_name, dict(tc.arguments or {}))
            )
        return [{"success": True, "data": {}} for _ in tool_calls]

    async def fake_create_execution_result(**kw):
        recorded["results"].append(kw)
        return {"id": f"res-{len(recorded['results'])}"}

    async def fake_create_execution_step(*, session_id, step_type, step_data):
        steps.append({"step_type": step_type, "step_data": step_data})
        return {"id": f"step-{len(steps)}"}

    async def fake_log_step(session_id, step_type, data):
        recorded["log_steps"].append((session_id, step_type, data))

    monkeypatch.setattr(
        agent_mod, "create_execution_session", fake_create_session
    )
    monkeypatch.setattr(agent_mod, "_close_superseded_sessions", AsyncMock())
    monkeypatch.setattr(agent_mod, "seed_clarification_history", AsyncMock())
    monkeypatch.setattr(
        agent_mod, "_load_org_preferences", AsyncMock(return_value={})
    )
    monkeypatch.setattr(agent_mod, "_update_status", AsyncMock())
    monkeypatch.setattr(
        agent_mod, "build_context", AsyncMock(return_value=_stub_context())
    )
    monkeypatch.setattr(
        agent_mod, "create_clarification", fake_create_clarification
    )
    monkeypatch.setattr(
        agent_mod, "create_confirmation", fake_create_confirmation
    )
    monkeypatch.setattr(
        agent_mod, "execute_planned_tool_calls", fake_execute_planned
    )
    monkeypatch.setattr(
        agent_mod, "verify_journal", AsyncMock(return_value={"verified": True})
    )
    monkeypatch.setattr(
        agent_mod, "create_execution_result", fake_create_execution_result
    )
    monkeypatch.setattr(
        agent_mod, "create_execution_step", fake_create_execution_step
    )
    if timeline_mode:
        pass
    else:
        monkeypatch.setattr(agent_mod, "_log_step", fake_log_step)
        monkeypatch.setattr(agent_mod, "flush_step_logs", AsyncMock())
    return recorded



class TestCallerGuardsUnexpectedNone:
    @pytest.mark.asyncio
    async def test_c_none_reasoning_result_becomes_controlled_failure(
        self, monkeypatch
    ):
        """If the reasoning layer ever returns None (an internal defect, not a
        model answer), the caller must fail CONTROLLED: no planning, no
        confirmation snapshot, no tool execution, no accounting mutation, and
        no ``.as_dict()`` AttributeError escaping into the generic handler."""

        recorded = _stub_boundaries(monkeypatch)
        monkeypatch.setattr("app.config.get_settings", lambda: _AgentSettings())
        monkeypatch.setattr(
            "app.accounting_reasoning.run_reasoning_loop",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(agent_mod, "get_client", lambda: object())

        response = await agent_mod.execute(
            user_message=PAID_ABC, user_id=USER, organization_id=ORG
        )

        # Controlled failure — never an AttributeError.
        assert response.status == ExecutionStatus.FAILED
        # Nothing downstream of reasoning was allowed to start.
        assert recorded["tool_runs"] == [], "no tool may execute"
        assert recorded["confirmations"] == [], (
            "no confirmation snapshot may be created"
        )
        assert recorded["clarifications"] == []
        assert recorded["results"] == [], (
            "the guard is not the generic error path — no error result row"
        )
        # The run is closed honestly, with a controlled FAILED timeline step.
        # (The run legitimately passed through INTERPRETING first.)
        status_calls = [
            call.args[1] for call in agent_mod._update_status.await_args_list
        ]
        assert ExecutionStatus.FAILED in status_calls
        assert status_calls[-1] == ExecutionStatus.FAILED
        # The FAILED step is captured either at the _log_step boundary or (if
        # durability routing is under test) through create_execution_step —
        # assert on both so the check is about CONTENT, not plumbing.
        failed = [
            *[(t, d) for _, t, d in recorded["log_steps"] if t == "FAILED"],
            *[
                (s["step_type"], s["step_data"])
                for s in recorded["steps"]
                if s["step_type"] == "FAILED"
            ],
        ]
        assert failed, "the timeline must end in an explicit FAILED step"
        assert failed[-1][1]["stage"] == "reasoning"
        assert failed[-1][1]["controlled"] is True
        assert failed[-1][1]["reason"] == "reasoning_result_missing"


class TestTopLevelExceptionWritesDurableFailedStep:
    @pytest.mark.asyncio
    async def test_d_unhandled_exception_creates_durable_failed_step(
        self, monkeypatch
    ):
        """An unexpected exception escaping the agent path must (a) still
        write the execution_results error row as before, and (b) create a
        durable FAILED execution step identifying stage, reason, error type
        and that it was unhandled — the timeline must not silently end at the
        previous successful step."""

        steps = []
        recorded = _stub_boundaries(monkeypatch, steps=steps)
        monkeypatch.setattr("app.config.get_settings", lambda: _AgentSettings())

        async def _boom(session_id, prior_qa):
            raise RuntimeError("context exploded")

        monkeypatch.setattr(agent_mod, "seed_clarification_history", _boom)

        response = await agent_mod.execute(
            user_message=PAID_ABC, user_id=USER, organization_id=ORG
        )

        assert response.status == ExecutionStatus.FAILED
        # (a) The result row is still written exactly as before the fix.
        assert recorded["results"], "the error result row must still be written"
        assert recorded["results"][-1]["status"] == "FAILED"
        assert "context exploded" in str(
            recorded["results"][-1].get("result_data", {}).get("error", "")
        )
        # (b) A durable FAILED step now closes the timeline.
        failed = [s for s in steps if s["step_type"] == "FAILED"]
        assert failed, (
            "the execution timeline must end in an explicit FAILED step, "
            "not silently at the previous successful step"
        )
        data = failed[-1]["step_data"]
        assert data["stage"] == "agent_execute"
        assert data["error_type"] == "RuntimeError"
        assert "context exploded" in data["reason"]
        assert data["controlled"] is False

