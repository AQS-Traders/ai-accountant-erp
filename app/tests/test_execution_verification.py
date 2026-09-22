"""Regression tests â€” operation-level execution verification.

Production incident (session 6a48a432-d9d9-480c-8e47-e6799305bc6f)
===================================================================

A multi-step plan (``create_customer`` -> ``create_invoice``) executed with:

    create_customer -> SUCCESS
    create_invoice  -> FAILED
    overall         -> COMPLETED / VERIFIED

The old batch-level check ``verified = bool(successful_results)`` let the
successful AUXILIARY step launder the whole execution into VERIFIED, while
the primary requested operation (the invoice) never recorded anything.
The persisted summary even claimed "Sales invoice created with journal
entry" (LLM/plan expected_outcome) plus a factually wrong "some steps needed
retries" note.

These tests pin the corrected invariants:

* every required (financial/primary) mutation must succeed before the
  execution can be COMPLETED / VERIFIED â€” fail-closed (Tests B, D);
* a successful auxiliary mutation stays honestly represented (Test B);
* dependency semantics are preserved: a legitimately optional auxiliary
  failure does not fail a successful primary operation (Test C);
* the persisted failure summary is derived from ACTUAL tool results, never
  from the LLM/plan expected_outcome (Test E);
* a failed primary mutation with no journal cannot be marked VERIFIED by a
  journal produced by another operation (Test F).

No live provider, no live database â€” boundaries are stubbed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.agent as agent_mod
from app.agent import resume_with_confirmation
from app.models.schemas import ExecutionStatus, ToolCall, ToolResult

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
MSG = "create an invoice for abc furnitures for selling them 2 chairs amounting to 25 000"
# The plan/LLM narrative that production persisted for session
# 6a48a432-d9d9-480c-8e47-e6799305bc6f — Test E proves it cannot manufacture a
# success claim once the primary mutation failed.
EXPECTED_OUTCOME = "Sales invoice created with journal entry"


# ---------------------------------------------------------------------------
# Unit tests â€” the pure verification helpers
# ---------------------------------------------------------------------------


class TestVerificationHelpers:
    def test_financial_mutation_failure_detected(self):
        trs = [
            ToolResult(tool_name="create_customer", success=True),
            ToolResult(
                tool_name="create_invoice", success=False,
                error="An unexpected error occurred while executing this operation.",
            ),
        ]
        failed = agent_mod._financial_mutation_failures(trs)
        assert [tr.tool_name for tr in failed] == ["create_invoice"]

    def test_non_financial_failure_is_not_required(self):
        trs = [
            ToolResult(tool_name="get_catalog", success=False, error="x"),
        ]
        assert agent_mod._financial_mutation_failures(trs) == []

    def test_primary_failed_flag_on_failure(self):
        trs = [
            ToolResult(tool_name="create_customer", success=True),
            ToolResult(tool_name="create_invoice", success=False, error="boom"),
        ]
        assert agent_mod._primary_mutation_failed(
            trs, intent="create_invoice"
        ) is True

    def test_primary_failed_flag_false_when_primary_succeeded(self):
        trs = [
            ToolResult(tool_name="create_customer", success=True),
            ToolResult(
                tool_name="create_invoice", success=True,
                data={"entry": {"id": "j1"}},
            ),
        ]
        assert agent_mod._primary_mutation_failed(
            trs, intent="create_invoice"
        ) is False

    def test_primary_failed_flag_true_when_primary_never_ran(self):
        trs = [ToolResult(tool_name="create_customer", success=True)]
        assert agent_mod._primary_mutation_failed(
            trs, intent="create_invoice"
        ) is True

    def test_non_financial_intent_is_never_primary_flagged(self):
        """An informational intent (read-only / report) cannot fail merely
        because no financial mutation matched it."""
        trs = [ToolResult(tool_name="get_trial_balance", success=True)]
        assert agent_mod._primary_mutation_failed(
            trs, intent="generate_trial_balance"
        ) is False


# ---------------------------------------------------------------------------
# End-to-end execute() tests â€” boundaries stubbed


from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock as _AM  # noqa: E402


def _stub_context():
    return SimpleNamespace(
        relevant_customers=[], relevant_suppliers=[],
        relevant_accounts=[], relevant_bank_accounts=[],
        extracted_entities={}, classification=None, economic_event=None,
        impact_map={}, prohibited_actions=[], preliminary_extraction={},
        live_evidence=[], accounting_reasoning=None,
    )


def _stub_confirmed_world(
    monkeypatch, *, outcomes, session=None, extra_plan_calls=None
):
    """Stub ONLY process boundaries for a CONFIRMED multi-tool execution.

    ``outcomes`` maps tool_name -> {"success": bool, "data": {...}, "error": ...}
    and is replayed by the execute_planned_tool_calls double in plan order.

    ``extra_plan_calls`` appends additional operations to the APPROVED plan
    snapshot (the plan the user confirmed).  Tool calls and their results are
    paired positionally, so an extra operation must be added to the plan — not
    injected into the results — to stay faithful.

    The REAL agent code runs: resume_with_confirmation -> execute
    (confirmation_granted=True, approved_tool_calls) -> deterministic path
    -> Phase 7 operation-level verification (the code under test).
    """
    session = session or {
        "id": str(uuid.uuid4()),
        "organization_id": str(ORG),
        "user_id": str(USER),
        "user_request": MSG,
        "conversation_id": "conv-verify",
        "status": "WAITING_FOR_USER",
        "current_phase": "AWAITING_CONFIRMATION",
    }
    recorded = {
        "session": session,
        "results": [],
        "steps": [],
        "tool_runs": [],
        "journals": [],
        "tool_calls": [],
    }

    async def fake_fetch_one(table, filters=None, **kw):
        # first read: the pending session; later reads (replay check etc.): none
        if fake_fetch_one.calls == 0:
            fake_fetch_one.calls += 1
            return session
        fake_fetch_one.calls += 1
        return None
    fake_fetch_one.calls = 0

    async def fake_fetch_many(table, filters=None, **kw):
        if table == "ai_confirmations":
            # The APPROVED plan carries CANONICAL ids: before execution the
            # materialization stage resolves human-level references
            # (`customer_name`, `items[].product_name`) against the live,
            # tenant-scoped books (see app/plan_materialization.py), and these
            # tests stub nothing about the party/catalog lookups.  Their subject
            # is the VERIFICATION semantics — which outcome may be called
            # VERIFIED — so the fixture supplies the materialized shape it would
            # receive in production.  The name-based shape is covered by
            # app/tests/test_plan_materialization.py.
            plan = [
                {
                    "tool_name": "create_customer",
                    "arguments": {"name": "ABC Furnitures"},
                },
                {
                    "tool_name": "create_invoice",
                    "arguments": {
                        "customer_id": "cust-abc-furnitures",
                        "invoice_date": "2026-09-20",
                        "due_date": "2026-10-20",
                        "items": [
                            {"description": "chairs", "quantity": 2,
                             "unit_price": 16666.67}
                        ],
                    },
                },
            ]
            plan.extend(extra_plan_calls or [])
            return [{
                "id": "conf-1",
                "execution_session_id": session["id"],
                "user_confirmed": None,
                "plan": plan,
            }]
        return []

    async def fake_create_session(**kw):
        # execute() still creates a session row; reuse the pending one so the
        # durable writes target the session the user confirmed.
        return session

    async def fake_execute_planned(tool_calls, executor):
        out = []
        for tc in tool_calls:
            recorded["tool_runs"].append((tc.tool_name, dict(tc.arguments or {})))
            spec = outcomes.get(
                tc.tool_name, {"success": True, "data": {}}
            )
            out.append({
                "success": bool(spec.get("success", True)),
                "tool_name": tc.tool_name,
                "data": spec.get("data", {}),
                **({"error": spec["error"]} if spec.get("error") else {}),
            })
        return out

    async def fake_create_result(**kw):
        recorded["results"].append(kw)
        return {"id": f"res-{len(recorded['results'])}"}

    async def fake_log_step(session_id, step_type, data):
        recorded["steps"].append((session_id, step_type, data))

    async def fake_log_tool_call(**kw):
        # Hermetic: the real writer would open a DB client per tool call.
        # Recording here keeps the run offline and still proves the audit
        # path was invoked (observability of ai_tool_calls is a separate PR).
        recorded["tool_calls"].append(kw)
        return {"id": f"tc-{len(recorded['tool_calls'])}"}

    async def fake_verify_journal(**kw):
        recorded["journals"].append(kw)
        return {"verified": True}

    class _LegacyProvider:
        async def generate_text(self, *, prompt: str = "", **kw):
            # Parses as a decision that helps nobody -> the pipeline falls
            # through to the deterministic path exactly as before.
            return '{"understanding": {"basis": "verification-test double"}}'

    monkeypatch.setattr(agent_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_mod, "fetch_many", fake_fetch_many)
    monkeypatch.setattr(
        agent_mod, "create_execution_session", fake_create_session
    )
    monkeypatch.setattr(
        agent_mod, "_close_superseded_sessions", _AM(return_value=0)
    )
    monkeypatch.setattr(agent_mod, "seed_clarification_history", _AM())
    monkeypatch.setattr(
        agent_mod, "_load_org_preferences", _AM(return_value={})
    )
    monkeypatch.setattr(
        agent_mod, "resolve_confirmation", _AM(return_value={"id": "conf-1"})
    )
    monkeypatch.setattr(
        agent_mod, "get_clarification_history", _AM(return_value=[])
    )
    monkeypatch.setattr(agent_mod, "_update_status", _AM())
    monkeypatch.setattr(agent_mod, "_log_step", fake_log_step)
    monkeypatch.setattr(agent_mod, "log_tool_call", fake_log_tool_call)
    monkeypatch.setattr(agent_mod, "flush_step_logs", _AM())
    monkeypatch.setattr(
        agent_mod, "get_client", lambda: _LegacyProvider()
    )

    async def fake_build_context(**kw):
        ctx = _stub_context()
        # The real build_context merges the planner's entities (including the
        # answers merged from the confirmation flow) into the context.
        ctx.extracted_entities = dict(kw.get("entity_hints") or {})
        return ctx

    monkeypatch.setattr(agent_mod, "build_context", fake_build_context)

    # The accounting-reasoning layer is exercised end-to-end in
    # test_llm_primary_reasoning.py / test_reasoning_round_exhaustion.py.  Here
    # it returns its terminal non-blocking outcome so the confirmed-execution
    # path (the code under test) runs offline: a real loop reads books evidence
    # over the network.  UNSUPPORTED never short-circuits execution in agent.py
    # (only REFUSAL / COMPLETE / NEEDS_INPUT / PROPOSAL do).
    from app.accounting_reasoning import ReasoningOutcome, UNSUPPORTED

    async def fake_reasoning_loop(*args, **kw):
        return ReasoningOutcome(status=UNSUPPORTED, rounds=1)

    monkeypatch.setattr(
        "app.accounting_reasoning.run_reasoning_loop", fake_reasoning_loop
    )
    monkeypatch.setattr(
        agent_mod, "_party_resolution_question", _AM(return_value=None)
    )
    monkeypatch.setattr(
        agent_mod, "_settlement_check_question", _AM(return_value=None)
    )
    monkeypatch.setattr(
        agent_mod, "_catalog_check_question", _AM(return_value=None)
    )
    monkeypatch.setattr(
        agent_mod, "_revenue_ledger_review_gate", _AM(return_value=None)
    )

    # A real classification object: the pipeline reads its fields
    # unconditionally (e.g. ``classification.transaction_nature`` for the
    # audit trail), so a None stub would fail for the wrong reason.
    from app.models.schemas import TransactionClassification

    async def fake_classify(**kw):
        return TransactionClassification(
            transaction_nature="CREDIT_SALE",
            confidence="HIGH",
            source="DETERMINISTIC_RULE",
            requires_clarification=False,
        )

    monkeypatch.setattr(
        "app.classifier.classify_transaction", fake_classify
    )
    monkeypatch.setattr(
        "app.services.preference_service.record_answer_preference", _AM()
    )
    monkeypatch.setattr(
        agent_mod, "execute_planned_tool_calls", fake_execute_planned
    )
    monkeypatch.setattr(agent_mod, "verify_journal", fake_verify_journal)
    monkeypatch.setattr(
        agent_mod, "create_execution_result", fake_create_result
    )

    # The GL-label resolver reads the accounts table.  Stub the DB touch point
    # (the real resolver's only job is a label lookup; the impact builder itself
    # stays real and is covered by entity-contract tests).
    async def _no_label(account_id):
        return None

    monkeypatch.setattr(
        agent_mod, "_make_account_label_resolver", lambda org: _no_label
    )
    # Planning is fully specified here: these tests target OUTCOME semantics
    # (Phase 7 verification), not planning.  A deterministic plan with no
    # missing fields routes the confirmed run straight to execution, and it
    # carries the PRIMARY user intent (create_invoice) the verification layer
    # must key on.  MSG/EXPECTED mirror the production request.
    def fake_planner(*args, **kwargs):
        return agent_mod.ExecutionPlan(
            intent="create_invoice",
            entity_type="customer",
            entity_name="ABC Furnitures",
            transaction_type="CREDIT_SALE",
            potential_tools=["create_customer", "create_invoice"],
            requires_validation=True,
            requires_accounting_engine=True,
            requires_confirmation=True,
            requires_clarification=False,
            missing_fields=[],
            clarification_questions=[],
            expected_outcome=EXPECTED_OUTCOME,
            extracted_entities={
                "customer_name": "ABC Furnitures",
                "amount": 25000,
                "transaction_nature": "CREDIT_SALE",
            },
            economic_event="CREDIT_SALE",
            impact_map={},
            prohibited_actions=[],
            transaction_nature="CREDIT_SALE",
            transaction_nature_source="DETERMINISTIC_RULE",
            batch_items=None,
        )

    monkeypatch.setattr(agent_mod, "run_planner", fake_planner)
    return recorded




class TestExecuteOutcomeSemantics:
    @staticmethod
    def _run(
        monkeypatch, *, invoice_success: bool,
        extra_outcomes=None, extra_plan_calls=None,
    ):
        outcomes = {
            "create_customer": {
                "success": True, "data": {"id": "cus-1"},
            },
            "create_invoice": (
                {
                    "success": True,
                    "data": {"id": "inv-1", "entry": {"id": "j-1"}},
                }
                if invoice_success
                else {
                    "success": False, "data": {},
                    "error": (
                        "An unexpected error occurred while executing "
                        "this operation."
                    ),
                }
            ),
        }
        outcomes.update(extra_outcomes or {})
        rec = _stub_confirmed_world(
            monkeypatch, outcomes=outcomes,
            extra_plan_calls=extra_plan_calls,
        )
        return rec

    @pytest.mark.asyncio
    async def test_a_primary_and_auxiliary_both_succeed_verified(
        self, monkeypatch
    ):
        """Test A — both mutations succeed → COMPLETED / VERIFIED."""
        rec = self._run(monkeypatch, invoice_success=True)

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.COMPLETED
        assert response.verification_status == "VERIFIED"
        assert rec["tool_runs"] == [
            ("create_customer", {"name": "ABC Furnitures"}),
            ("create_invoice", dict(rec["tool_runs"][1][1])),
        ]
        assert rec["results"][-1]["status"] == "COMPLETED"
        assert rec["results"][-1]["verification_status"] == "VERIFIED"

    @pytest.mark.asyncio
    async def test_b_primary_fails_auxiliary_succeeds_failed(
        self, monkeypatch
    ):
        """Test B — THE production incident shape.

        create_customer SUCCESS + create_invoice FAILURE must produce
        FAILED / NOT VERIFIED, never claim the invoice exists, and still
        honestly report that the customer WAS created."""
        rec = self._run(monkeypatch, invoice_success=False)

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.FAILED
        assert response.verification_status == "FAILED"
        # The customer success stays represented — never hidden.
        assert "create_customer" in response.summary
        # The primary failure is named, with no false success narrative.
        assert "create_invoice" in response.summary
        assert "Invoice created successfully" not in response.summary
        assert "created with journal entry" not in response.summary
        last = rec["results"][-1]
        assert last["status"] == "FAILED"
        assert last["verification_status"] == "FAILED"
        # ``create_execution_result(result_data=...)`` maps to the DB column
        # ``result_payload``; the fake intercepts the FUNCTION, so the recorded
        # kwarg is ``result_data``.
        payload = last.get("result_data", {})
        assert payload.get("succeeded_operations") == ["create_customer"]
        failed_ops = [
            f["tool"] for f in payload.get("failed_operations", [])
        ]
        assert failed_ops == ["create_invoice"]
        # Durable timeline: explicit FAILED step with the split outcome.
        failed_steps = [s for s in rec["steps"] if s[1] == "FAILED"]
        assert failed_steps, "timeline must end in an explicit FAILED step"
        data = failed_steps[-1][2]
        assert data["failed_operations"] == ["create_invoice"]
        assert data["succeeded_operations"] == ["create_customer"]
        assert data["controlled"] is True


    @pytest.mark.asyncio
    async def test_c_auxiliary_fails_primary_succeeds_still_verified(
        self, monkeypatch
    ):
        """Test C — dependency semantics (deliberately NOT Test B).

        ``search_customer`` is an auxiliary READ-ONLY lookup (not a financial
        mutation) that failed.  The primary invoice mutation succeeded on its
        own, so the execution legitimately stays COMPLETED / VERIFIED — with
        the auxiliary failure disclosed honestly and without retry wording.
        A read-only lookup must never be treated as a required mutation, and
        an auxiliary failure must never be silent."""
        rec = self._run(
            monkeypatch,
            invoice_success=True,
            extra_plan_calls=[{
                "tool_name": "search_customer",
                "arguments": {"name": "ABC Furnitures"},
            }],
            extra_outcomes={
                "search_customer": {
                    "success": False, "error": "lookup failed",
                },
            },
        )

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.COMPLETED
        assert response.verification_status == "VERIFIED"
        # The auxiliary failure is disclosed with honest (non-retry) wording.
        assert "not retried" in response.summary
        assert "search_customer" in response.summary
        assert rec["results"][-1]["status"] == "COMPLETED"
        assert rec["results"][-1]["verification_status"] == "VERIFIED"

    @pytest.mark.asyncio
    async def test_d_multiple_mutations_one_required_failure_failed(
        self, monkeypatch
    ):
        """Test D — a failed REQUIRED (financial) mutation fails the whole
        execution even when other mutations succeeded.  No partial success may
        be promoted to VERIFIED, and the successful mutations stay reported."""
        rec = self._run(
            monkeypatch,
            invoice_success=True,
            extra_plan_calls=[{
                "tool_name": "record_customer_receipt",
                # canonical reference: a receipt needs the customer's id, and
                # the contract gate now refuses an un-bindable approved plan
                # before anything executes.
                "arguments": {"customer_id": "cust-abc-furnitures", "amount": 25000},
            }],
            extra_outcomes={
                "record_customer_receipt": {
                    "success": False, "error": "settlement account missing",
                },
            },
        )

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.FAILED
        assert response.verification_status == "FAILED"
        failed_steps = [s for s in rec["steps"] if s[1] == "FAILED"]
        assert failed_steps
        assert failed_steps[-1][2]["failed_operations"] == [
            "record_customer_receipt"
        ]
        assert "create_invoice" in failed_steps[-1][2]["succeeded_operations"]

    @pytest.mark.asyncio
    async def test_e_llm_expected_outcome_cannot_override_failure(
        self, monkeypatch
    ):
        """Test E — an LLM/plan ``expected_outcome`` can never manufacture
        success.

        The plan claims ``expected_outcome = "Invoice created successfully"``
        while the actual invoice mutation FAILS.  The user-visible and
        persisted result must say FAILED and must not repeat the claim:
        an LLM expected outcome is not a verified database outcome.
        """
        rec = self._run(monkeypatch, invoice_success=False)

        real_planner = agent_mod.run_planner

        def lying_planner(*args, **kwargs):
            plan = real_planner(*args, **kwargs)
            plan.expected_outcome = "Invoice created successfully"
            return plan

        monkeypatch.setattr(agent_mod, "run_planner", lying_planner)

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.FAILED
        assert response.verification_status == "FAILED"
        assert "Invoice created successfully" not in response.summary
        assert "created with journal entry" not in response.summary
        # The persisted result row is mechanical truth, not the narrative.
        last = rec["results"][-1]
        assert last["status"] == "FAILED"
        assert last["verification_status"] == "FAILED"
        assert "Invoice created successfully" not in last["summary"]

    @pytest.mark.asyncio
    async def test_f_no_false_journal_verification_on_primary_failure(
        self, monkeypatch
    ):
        """Test F — a journal produced by an AUXILIARY operation cannot render
        a FAILED primary mutation VERIFIED.

        ``create_customer`` returns a journal-entry-shaped payload while the
        primary ``create_invoice`` fails and produces no journal at all.  The
        journal-verification loop must not run for the auxiliary entry, and
        the execution must stay FAILED / NOT VERIFIED.
        """
        rec = self._run(
            monkeypatch,
            invoice_success=False,
            extra_outcomes={
                "create_customer": {
                    "success": True,
                    "data": {
                        "id": "cus-1",
                        "entry": {"id": str(uuid.uuid4())},
                    },
                },
            },
        )

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.FAILED
        assert response.verification_status == "FAILED"
        # Nothing was accepted as proof of success: the verification loop
        # never even reached the auxiliary entry.
        assert rec["journals"] == []
        last = rec["results"][-1]
        assert last["status"] == "FAILED"
        assert last["verification_status"] == "FAILED"
        assert last["result_data"]["succeeded_operations"] == [
            "create_customer"
        ]
        # The invoice produced no journal, and no VERIFIED claim was made.
        assert last["result_data"]["failed_operations"][0]["tool"] == (
            "create_invoice"
        )

    @pytest.mark.asyncio
    async def test_g_phase9_guard_no_false_narrative_without_validation_phase(
        self, monkeypatch
    ):
        """Test G — belt-and-braces: even if the plan bypassed the VALIDATION
        phase (``requires_validation=False`` — unreachable for a real mutation
        intent), a failed primary mutation can still never produce
        COMPLETED / VERIFIED, and the persisted summary must be mechanical
        execution truth rather than the LLM narrative or ``expected_outcome``.
        """
        rec = self._run(monkeypatch, invoice_success=False)

        real_planner = agent_mod.run_planner

        def bypass_validation_planner(*args, **kwargs):
            plan = real_planner(*args, **kwargs)
            plan.requires_validation = False
            plan.expected_outcome = "Invoice created successfully"
            return plan

        monkeypatch.setattr(
            agent_mod, "run_planner", bypass_validation_planner
        )

        response = await resume_with_confirmation(
            session_id=uuid.UUID(rec["session"]["id"]),
            approved=True, user_id=USER, organization_id=ORG,
        )

        assert response.status == ExecutionStatus.FAILED
        assert response.verification_status == "FAILED"
        assert "Invoice created successfully" not in response.summary
        assert "create_invoice" in response.summary
        last = rec["results"][-1]
        assert last["status"] == "FAILED"
        assert last["verification_status"] == "FAILED"
        assert "Invoice created successfully" not in last["summary"]

