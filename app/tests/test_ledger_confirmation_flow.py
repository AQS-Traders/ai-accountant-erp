"""Revenue-ledger clarification → decision → sale → verification flow.

Pins the END-TO-END contract for the dedicated-revenue-ledger question:

  ASK (once, persisted) → ANSWER (any channel) → DECISION APPLIED EXACTLY ONCE
  → sale executed exactly once with the resolved account → journal verified.

Root-cause regression guard: the review question used to be returned WITHOUT
a persisted ``ai.clarifications`` row, so ``resolve_clarification`` silently
discarded the answer, the planner never saw ``revenue_ledger_decision`` and
the SAME question was re-asked forever.  The live database confirmed it:
zero ``ai.clarifications`` rows contain "Revenue ledger check:".

Also pins:
  * authorization — a session from another organisation/user cannot be resumed;
  * resume idempotency — a duplicate clarify/confirm never re-executes;
  * CREATE / USE_EXISTING decisions, named-account resolution, no silent
    fallback, concurrent-creation safety;
  * the approved-plan snapshot (migration 079) surviving confirmation resume.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

import app.agent as agent_mod
from app.agent import (
    _cash_sale_fast_path_call,
    _revenue_ledger_review_gate,
    resume_with_clarification,
    resume_with_confirmation,
)
from app.models.schemas import (
    AgentResponse,
    ExecutionPlan,
    ExecutionStatus,
    ToolCall,
)
from app.planner import plan
from app.reasoning import nature_question_for_intent
from app.services import revenue_ledger_service as rls

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_ORG = uuid.UUID("99999999-9999-9999-9999-999999999999")
OTHER_USER = uuid.UUID("88888888-8888-8888-8888-888888888888")

NATURE_Q = nature_question_for_intent("record_cash_sale")
SALE_MESSAGE = "sold 5 chairs for 3000 cash today"


def _acct(code: str, name: str, parent: str | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "code": code,
        "name": name,
        "account_type": "REVENUE",
        "normal_balance": "CREDIT",
        "parent_account_id": parent,
    }


class FakeAccountRepo:
    """In-memory chart of accounts honouring the (organization_id, code)
    unique constraint — the database-level idempotency backstop."""

    def __init__(self, accounts):
        self.accounts = list(accounts)
        self.next_code = "4011"
        self.insert_attempts = 0

    async def get_chart_of_accounts(
        self, organization_id, *, account_type=None, limit=500, is_active=True
    ):
        rows = self.accounts
        if account_type:
            rows = [r for r in rows if r.get("account_type") == account_type]
        return rows[:limit]

    async def next_available_code(self, organization_id, requested_code, *, max_probes=200):
        return self.next_code

    async def create_account(self, *, organization_id, code, name, **kw):
        self.insert_attempts += 1
        if any(a["name"].strip().lower() == name.strip().lower() for a in self.accounts):
            raise ValueError(
                'duplicate key value violates unique constraint '
                '"accounts_organization_id_code_key"'
            )
        row = {
            "id": str(uuid.uuid4()),
            "code": code,
            "name": name,
            "account_type": kw.get("account_type"),
            "normal_balance": kw.get("normal_balance"),
            "parent_account_id": kw.get("parent_account_id"),
            "description": kw.get("description"),
        }
        self.accounts.append(row)
        return row


@pytest.fixture
def chart(monkeypatch):
    """A multi-revenue chart with NO dedicated 'Chairs' ledger."""
    repo = FakeAccountRepo([
        _acct("4000", "Operating Revenue"),
        _acct("4010", "Software Development Revenue"),
        _acct("4020", "Consulting Revenue"),
    ])
    monkeypatch.setattr(rls, "a_repo", repo)
    return repo


# ---------------------------------------------------------------------------
# Planner: every answer channel normalises to the decision contract
# ---------------------------------------------------------------------------

class TestPlannerDecisionMerge:
    """Typed "yes" / "create" / "ok" AND the rendered chip all map to CREATE;
    anything else names the existing account to use."""

    def _merged(self, answer):
        return plan(SALE_MESSAGE, clarification_history=[
            {"question": NATURE_Q, "answer": "a"},
            {"question": "Revenue ledger check: nothing is recorded against "
                         "'chairs' yet. Create a dedicated ledger? Reply YES.",
             "answer": answer},
        ])

    @pytest.mark.parametrize("answer", ["yes", "YES", "create", "CREATE", "ok", "y"])
    def test_affirmative_answers_map_to_create(self, answer):
        merged = self._merged(answer)
        assert merged.extracted_entities["revenue_ledger_decision"] == "CREATE"

    def test_existing_account_names_use_existing(self):
        p = self._merged("Software Development Revenue")
        assert p.extracted_entities["revenue_ledger_decision"] == "USE_EXISTING"
        assert p.extracted_entities["revenue_account_name"] == (
            "Software Development Revenue"
        )

    def test_chip_label_is_a_use_existing_answer(self):
        p = self._merged("Use the existing 'Operating Revenue' account")
        assert p.extracted_entities["revenue_ledger_decision"] == "USE_EXISTING"
        assert p.extracted_entities["revenue_account_name"] == (
            "Use the existing 'Operating Revenue' account"
        )


class TestExtractAccountName:
    def test_chip_label_extracts_the_quoted_name(self):
        assert rls.extract_account_name(
            "Use the existing 'Operating Revenue' account"
        ) == "Operating Revenue"

    def test_bare_name_passes_through(self):
        assert rls.extract_account_name("operating revenue") == "operating revenue"

    def test_empty_is_empty(self):
        assert rls.extract_account_name("") == ""
        assert rls.extract_account_name(None) == ""


class TestApplyDecision:
    @pytest.mark.asyncio
    async def test_create_creates_exactly_one_dedicated_ledger(self, chart):
        account, created = await rls.apply_decision(ORG, "chairs", "CREATE")
        assert created is True
        # The label preserves the item's case (planner extracts "chairs").
        assert account["name"] == "chairs Sales"
        assert account["account_type"] == "REVENUE"
        assert chart.insert_attempts == 1

    @pytest.mark.asyncio
    async def test_repeated_create_reuses_the_ledger(self, chart):
        first, created1 = await rls.apply_decision(ORG, "chairs", "CREATE")
        second, created2 = await rls.apply_decision(ORG, "chairs", "CREATE")
        assert created1 is True and created2 is False
        assert first["id"] == second["id"]
        assert chart.insert_attempts == 1

    @pytest.mark.asyncio
    async def test_named_existing_account_resolves_exactly(self, chart):
        named = next(
            a for a in chart.accounts if a["name"] == "Software Development Revenue"
        )
        account, created = await rls.apply_decision(
            ORG, "chairs", "USE_EXISTING",
            named_account="software development revenue",
        )
        assert created is False
        assert account["id"] == named["id"]  # NOT the general revenue account

    @pytest.mark.asyncio
    async def test_chip_label_resolves_the_quoted_account(self, chart):
        account, _ = await rls.apply_decision(
            ORG, "chairs", "USE_EXISTING",
            named_account="Use the existing 'Operating Revenue' account",
        )
        assert account["name"] == "Operating Revenue"

    @pytest.mark.asyncio
    async def test_unknown_account_name_never_falls_back(self, chart):
        account, created = await rls.apply_decision(
            ORG, "chairs", "USE_EXISTING", named_account="Nonexistent Ledger"
        )
        assert account is None and created is False

    @pytest.mark.asyncio
    async def test_ambiguous_name_is_rejected(self, chart):
        chart.accounts.append(_acct("4030", "Consulting Revenue"))
        account, _ = await rls.apply_decision(
            ORG, "chairs", "USE_EXISTING", named_account="Consulting Revenue"
        )
        assert account is None

    @pytest.mark.asyncio
    async def test_plain_use_existing_takes_general_revenue(self, chart):
        account, created = await rls.apply_decision(ORG, "chairs", "USE_EXISTING")
        assert created is False and account["name"] == "Operating Revenue"


# ---------------------------------------------------------------------------
# The review gate — applies the decision, re-asks on garbage
# ---------------------------------------------------------------------------

def _fake_plan(entities):
    return ExecutionPlan(
        intent="record_cash_sale",
        potential_tools=["record_cash_sale"],
        extracted_entities=entities,
    )


class TestGateDecisionApplication:
    @pytest.mark.asyncio
    async def test_create_decision_binds_the_account_id(self, chart):
        entities = {"item_description": "chairs", "revenue_ledger_decision": "CREATE"}
        plan_obj = _fake_plan(entities)
        out = await _revenue_ledger_review_gate(
            organization_id=ORG, execution_plan=plan_obj
        )
        assert out is None  # no question — proceed to execution
        merged = plan_obj.extracted_entities
        assert merged["revenue_account_id"]
        assert merged["revenue_account_resolved"] == "created"

    @pytest.mark.asyncio
    async def test_invalid_answer_reasks_with_targeted_error(self, chart):
        entities = {
            "item_description": "chairs",
            "revenue_ledger_decision": "USE_EXISTING",
            "revenue_account_name": "Nonexistent Ledger",
        }
        plan_obj = _fake_plan(entities)
        out = await _revenue_ledger_review_gate(
            organization_id=ORG, execution_plan=plan_obj
        )
        assert out is not None
        assert "could not be applied" in out["question"]
        assert "revenue_account_id" not in plan_obj.extracted_entities
        assert all(isinstance(o, str) for o in out["options"])

    @pytest.mark.asyncio
    async def test_named_existing_account_never_reasks(self, chart):
        entities = {
            "item_description": "chairs",
            "revenue_ledger_decision": "USE_EXISTING",
            "revenue_account_name": "Software Development Revenue",
        }
        plan_obj = _fake_plan(entities)
        out = await _revenue_ledger_review_gate(
            organization_id=ORG, execution_plan=plan_obj
        )
        assert out is None
        named = next(
            a for a in chart.accounts if a["name"] == "Software Development Revenue"
        )
        assert plan_obj.extracted_entities["revenue_account_id"] == named["id"]

    def test_created_account_id_reaches_the_sale_tool(self, chart):
        """The resolved ledger id must ride INTO record_cash_sale."""
        entities = {
            "amount": 3000.0,
            "transaction_date": "2026-09-18",
            "transaction_nature": "GOODS",
            "item_description": "chairs",
            "revenue_account_id": str(uuid.uuid4()),
        }
        calls = _cash_sale_fast_path_call(entities)
        assert calls is not None
        assert calls[0].tool_name == "record_cash_sale"
        assert calls[0].arguments["revenue_account_id"] == entities["revenue_account_id"]


# ---------------------------------------------------------------------------
# Concurrent ledger creation — the unique-constraint loser recovers
# ---------------------------------------------------------------------------

class TestConcurrentLedgerCreation:
    @pytest.mark.asyncio
    async def test_two_concurrent_creates_produce_one_account(self, chart):
        """Both coroutines pass the pre-check lookup before either inserts;
        the second insert loses on the unique constraint and must recover
        the winner instead of raising or duplicating the account."""

        async def race_leg():
            existing = await rls.find_stream_ledger(ORG, "chairs")
            if existing is not None:
                return existing, False
            try:
                # Sleep INSIDE the create so both legs pass the pre-check.
                created = FakeAccountRepo.create_account(
                    chart,
                    organization_id=ORG,
                    code=chart.next_code,
                    name=rls.suggest_ledger_name("chairs"),
                    account_type="REVENUE",
                    normal_balance="CREDIT",
                )
                await asyncio.sleep(0.02)
                return await created, True
            except ValueError:
                recovered = await rls.find_stream_ledger(ORG, "chairs")
                assert recovered is not None, "constraint loser must recover"
                return recovered, False

        results = await asyncio.gather(race_leg(), race_leg())
        accounts = [r[0] for r in results]
        assert all(a is not None for a in accounts)
        assert accounts[0]["id"] == accounts[1]["id"]
        names = [a["name"] for a in chart.accounts if a["name"] == "chairs Sales"]
        assert len(names) == 1
        assert chart.insert_attempts == 2  # one winner + one loser


# ---------------------------------------------------------------------------
# Clarify resume — authorization, validation, idempotency
# ---------------------------------------------------------------------------

def _session_row(status="WAITING_FOR_USER", org=ORG, user=USER):
    return {
        "id": str(uuid.uuid4()),
        "organization_id": str(org),
        "user_id": str(user),
        "user_request": SALE_MESSAGE,
        "conversation_id": "conv-1",
        "status": status,
        "current_phase": "AWAITING_CLARIFICATION",
    }


def _patch_execute(monkeypatch):
    stub = AsyncMock(return_value=AgentResponse(
        status=ExecutionStatus.COMPLETED, summary="resumed"
    ))
    monkeypatch.setattr(agent_mod, "execute", stub)
    return stub


class TestClarifyResumeRouting:
    @pytest.mark.asyncio
    async def test_answer_is_recorded_and_merged_before_resume(
        self, monkeypatch, chart
    ):
        session = _session_row()
        executed = _patch_execute(monkeypatch)
        resolve = AsyncMock(return_value={"id": "clar-1"})
        history = AsyncMock(return_value=[
            {"question": "Revenue ledger check: create 'Chairs Sales'?", "answer": "yes"},
        ])
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(return_value=session))
        monkeypatch.setattr(agent_mod, "resolve_clarification", resolve)
        monkeypatch.setattr(agent_mod, "get_clarification_history", history)

        resp = await resume_with_clarification(
            session_id=uuid.UUID(session["id"]),
            user_answer="yes",
            user_id=USER,
            organization_id=ORG,
        )
        assert resolve.await_count == 1
        assert resolve.await_args.kwargs["user_response"] == "yes"
        assert executed.await_count == 1
        passed_history = executed.await_args.kwargs["clarification_history"]
        assert passed_history[-1]["answer"] == "yes"
        assert executed.await_args.kwargs["conversation_id"] == "conv-1"
        assert resp.summary == "resumed"

    @pytest.mark.asyncio
    async def test_cross_organisation_session_is_rejected(self, monkeypatch, chart):
        session = _session_row(org=OTHER_ORG)
        executed = _patch_execute(monkeypatch)
        resolve = AsyncMock()
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(return_value=session))
        monkeypatch.setattr(agent_mod, "resolve_clarification", resolve)

        resp = await resume_with_clarification(
            session_id=uuid.UUID(session["id"]),
            user_answer="yes",
            user_id=USER,
            organization_id=ORG,  # caller is in a DIFFERENT organisation
        )
        assert resp.status == ExecutionStatus.FAILED
        assert resp.summary == "Session not found."
        assert executed.await_count == 0
        assert resolve.await_count == 0

    @pytest.mark.asyncio
    async def test_cross_user_session_is_rejected(self, monkeypatch, chart):
        session = _session_row(user=OTHER_USER)
        executed = _patch_execute(monkeypatch)
        resolve = AsyncMock()
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(return_value=session))
        monkeypatch.setattr(agent_mod, "resolve_clarification", resolve)

        resp = await resume_with_clarification(
            session_id=uuid.UUID(session["id"]),
            user_answer="yes",
            user_id=USER,
            organization_id=ORG,
        )
        assert resp.status == ExecutionStatus.FAILED
        assert executed.await_count == 0
        assert resolve.await_count == 0

    @pytest.mark.asyncio
    async def test_empty_answer_stays_pending(self, monkeypatch, chart):
        session = _session_row()
        executed = _patch_execute(monkeypatch)
        resolve = AsyncMock()
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(return_value=session))
        monkeypatch.setattr(agent_mod, "resolve_clarification", resolve)

        for answer in ("", "   ", None):
            resp = await resume_with_clarification(
                session_id=uuid.UUID(session["id"]),
                user_answer=answer,
                user_id=USER,
                organization_id=ORG,
            )
            assert resp.status == ExecutionStatus.AWAITING_CLARIFICATION
            assert resp.summary == "An answer is required."
        assert resolve.await_count == 0
        assert executed.await_count == 0

    @pytest.mark.asyncio
    async def test_duplicate_resume_replays_without_reexecuting(
        self, monkeypatch, chart
    ):
        """A repeated HTTP request / double click after the answer round
        already consumed the session must NEVER re-enter execute()."""
        session = _session_row(status="COMPLETED")
        executed = _patch_execute(monkeypatch)
        # fetch_one: 1) session, 2) stored execution result
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(side_effect=[
            session,
            {"summary": "Sale recorded — VERIFIED", "status": "COMPLETED"},
        ]))

        resp = await resume_with_clarification(
            session_id=uuid.UUID(session["id"]),
            user_answer="yes",
            user_id=USER,
            organization_id=ORG,
        )
        assert resp.status == ExecutionStatus.COMPLETED
        assert resp.summary == "Sale recorded — VERIFIED"
        assert executed.await_count == 0

    @pytest.mark.asyncio
    async def test_duplicate_resume_without_stored_result_is_honest(
        self, monkeypatch, chart
    ):
        session = _session_row(status="COMPLETED")
        executed = _patch_execute(monkeypatch)
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(side_effect=[
            session,
            None,  # no replayable result (reaped)
        ]))
        resp = await resume_with_clarification(
            session_id=uuid.UUID(session["id"]),
            user_answer="yes",
            user_id=USER,
            organization_id=ORG,
        )
        assert resp.status == ExecutionStatus.FAILED
        assert "already processed" in resp.summary
        assert "not duplicated" in resp.summary
        assert executed.await_count == 0


class TestConfirmResumeRouting:
    PLAN_SNAPSHOT = [
        {
            "tool_name": "record_cash_sale",
            "arguments": {
                "amount": 3000.0,
                "transaction_date": "2026-09-18",
                "description": "chairs",
                "revenue_account_id": "abc-123",
            },
        }
    ]

    def _pending_session(self, monkeypatch, executed=None, resolve=None):
        session = _session_row(status="WAITING_FOR_USER")
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(return_value=session))
        monkeypatch.setattr(
            agent_mod, "fetch_many",
            AsyncMock(return_value=[{
                "id": "conf-1",
                "execution_session_id": session["id"],
                "user_confirmed": None,
                "plan": self.PLAN_SNAPSHOT,
            }]),
        )
        monkeypatch.setattr(
            agent_mod, "resolve_confirmation",
            resolve or AsyncMock(return_value={"id": "conf-1"}),
        )
        monkeypatch.setattr(
            agent_mod, "get_clarification_history", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(agent_mod, "_update_status", AsyncMock())
        return session, executed

    @pytest.mark.asyncio
    async def test_approved_confirmation_reuses_the_approved_plan(
        self, monkeypatch, chart
    ):
        executed = _patch_execute(monkeypatch)
        session, _ = self._pending_session(monkeypatch)
        resp = await resume_with_confirmation(
            session_id=uuid.UUID(session["id"]),
            approved=True,
            user_id=USER,
            organization_id=ORG,
        )
        assert executed.await_count == 1
        kwargs = executed.await_args.kwargs
        assert kwargs["confirmation_granted"] is True
        # The approved plan is REUSED — no re-planning into different tools.
        reused = kwargs["approved_tool_calls"]
        assert reused is not None
        assert reused[0].tool_name == "record_cash_sale"
        assert reused[0].arguments["revenue_account_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_rejection_never_executes(self, monkeypatch, chart):
        executed = _patch_execute(monkeypatch)
        session, _ = self._pending_session(monkeypatch)
        resp = await resume_with_confirmation(
            session_id=uuid.UUID(session["id"]),
            approved=False,
            user_id=USER,
            organization_id=ORG,
        )
        assert resp.status == ExecutionStatus.REJECTED
        assert executed.await_count == 0

    @pytest.mark.asyncio
    async def test_duplicate_confirmation_never_reexecutes(self, monkeypatch, chart):
        """A second POST /api/ai/confirm must replay — never re-execute."""
        session = _session_row(status="COMPLETED")
        executed = _patch_execute(monkeypatch)
        resolve = AsyncMock()
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(side_effect=[
            session,
            {"summary": "Sale recorded — VERIFIED", "status": "COMPLETED"},
        ]))
        monkeypatch.setattr(agent_mod, "resolve_confirmation", resolve)

        resp = await resume_with_confirmation(
            session_id=uuid.UUID(session["id"]),
            approved=True,
            user_id=USER,
            organization_id=ORG,
        )
        assert resp.status == ExecutionStatus.COMPLETED
        assert resp.summary == "Sale recorded — VERIFIED"
        assert executed.await_count == 0
        assert resolve.await_count == 0

    @pytest.mark.asyncio
    async def test_cross_organisation_confirmation_is_rejected(self, monkeypatch, chart):
        session = _session_row(org=OTHER_ORG)
        executed = _patch_execute(monkeypatch)
        resolve = AsyncMock()
        monkeypatch.setattr(agent_mod, "fetch_one", AsyncMock(return_value=session))
        monkeypatch.setattr(agent_mod, "resolve_confirmation", resolve)

        resp = await resume_with_confirmation(
            session_id=uuid.UUID(session["id"]),
            approved=True,
            user_id=USER,
            organization_id=ORG,
        )
        assert resp.status == ExecutionStatus.FAILED
        assert resp.summary == "Session not found."
        assert executed.await_count == 0
        assert resolve.await_count == 0


# ---------------------------------------------------------------------------
# FULL FLOW — ask (persisted) → answer → decision → sale → verified
# ---------------------------------------------------------------------------

def _stub_context():
    """Attribute bag mirroring AgentContext for the deterministic path."""
    from types import SimpleNamespace

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
    )


class TestFullLedgerFlow:
    """Drives execute() + resume_with_clarification() through the REAL
    planner, the REAL revenue gate and the REAL decision application —
    only the process boundaries (DB writes, tool execution, journal
    verification) are stubbed."""

    @pytest.fixture
    def flow(self, monkeypatch):
        session = {
            "id": str(uuid.uuid4()),
            "organization_id": str(ORG),
            "user_id": str(USER),
            "user_request": SALE_MESSAGE,
            "conversation_id": "conv-flow",
            "status": "PENDING",
            "current_phase": "RECEIVED",
        }
        clarifications = []
        confirmations = []
        tool_runs = []

        async def fake_create_session(**kw):
            return session

        async def fake_create_clarification(**kw):
            clarifications.append(kw)
            return {"id": f"clar-{len(clarifications)}", **kw}

        async def fake_create_confirmation(**kw):
            confirmations.append(kw)
            return {"id": f"conf-{len(confirmations)}", **kw}

        async def fake_execute_planned(tool_calls, executor):
            for tc in tool_calls:
                tool_runs.append((tc.tool_name, dict(tc.arguments or {})))
            return [
                {
                    "success": True,
                    "tool_name": tc.tool_name,
                    "data": {
                        "entry": {"id": str(uuid.uuid4())},
                        "journal_posted": True,
                        "revenue_account_id": (tc.arguments or {}).get(
                            "revenue_account_id"
                        ),
                    },
                }
                for tc in tool_calls
            ]

        monkeypatch.setattr(agent_mod, "create_execution_session", fake_create_session)
        monkeypatch.setattr(agent_mod, "_close_superseded_sessions", AsyncMock())
        monkeypatch.setattr(agent_mod, "seed_clarification_history", AsyncMock())
        monkeypatch.setattr(
            agent_mod, "_load_org_preferences", AsyncMock(return_value={})
        )
        monkeypatch.setattr(agent_mod, "_log_step", AsyncMock())
        monkeypatch.setattr(agent_mod, "_update_status", AsyncMock())
        monkeypatch.setattr(agent_mod, "flush_step_logs", AsyncMock())
        async def fake_build_context(**kw):
            ctx = _stub_context()
            # The real build_context merges the planner's entities (including
            # answers merged from prior clarification rounds) into the context.
            ctx.extracted_entities = dict(kw.get("entity_hints") or {})
            return ctx

        monkeypatch.setattr(
            agent_mod, "build_context", AsyncMock(side_effect=fake_build_context)
        )
        monkeypatch.setattr(
            agent_mod, "_party_resolution_question", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            agent_mod, "_settlement_check_question", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            agent_mod, "_catalog_check_question", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(agent_mod, "create_clarification", fake_create_clarification)
        monkeypatch.setattr(agent_mod, "create_confirmation", fake_create_confirmation)
        async def fake_classify(**kw):
            """Offline classifier stub: the nature came from the user's
            answer; no account hint (the ledger decision handles revenue)."""
            from app.classifier import TransactionClassification

            entities = kw.get("entities") or {}
            return TransactionClassification(
                transaction_nature=entities.get("transaction_nature"),
                confidence="HIGH",
                source="USER_ANSWER",
                entity=entities.get("item_description"),
                account_hint_id=None,
                requires_clarification=False,
            )

        monkeypatch.setattr(
            "app.classifier.classify_transaction",
            AsyncMock(side_effect=fake_classify),
        )
        monkeypatch.setattr(
            "app.services.preference_service.record_answer_preference",
            AsyncMock(),
        )
        monkeypatch.setattr(
            agent_mod, "create_execution_result",
            AsyncMock(return_value={"id": "result-1"}),
        )
        monkeypatch.setattr(
            agent_mod, "execute_planned_tool_calls", fake_execute_planned
        )
        monkeypatch.setattr(
            agent_mod, "verify_journal", AsyncMock(return_value={"verified": True})
        )
        return {
            "session": session,
            "clarifications": clarifications,
            "confirmations": confirmations,
            "tool_runs": tool_runs,
        }

    @pytest.mark.asyncio
    async def test_ask_persists_clarification_and_answer_completes_the_sale(
        self, monkeypatch, flow, chart
    ):
        # ---- ROUND 1: the question --------------------------------------
        # (The goods/service nature was answered in an earlier round of the
        # same conversation — only the revenue-ledger decision is pending.)
        prior = [{"question": NATURE_Q, "answer": "a"}]
        first = await agent_mod.execute(
            user_message=SALE_MESSAGE,
            user_id=USER,
            organization_id=ORG,
            conversation_id="conv-flow",
            clarification_history=prior,
        )
        assert first.status == ExecutionStatus.AWAITING_CLARIFICATION
        assert first.required_information == ["revenue_ledger_decision"]
        assert first.options and all(isinstance(o, str) for o in first.options)
        # ROOT CAUSE: the question MUST be persisted as a pending
        # clarification row (and never as a generic confirmation).
        assert len(flow["clarifications"]) == 1
        assert flow["clarifications"][0]["required_fields"] == [
            "revenue_ledger_decision"
        ]
        assert flow["confirmations"] == []

        # ---- ROUND 2: the user answers YES -------------------------------
        session = flow["session"]
        session["status"] = "WAITING_FOR_USER"
        history = prior + [
            {"question": flow["clarifications"][0]["question"], "answer": "yes"},
        ]
        monkeypatch.setattr(
            agent_mod, "fetch_one", AsyncMock(return_value=session)
        )
        monkeypatch.setattr(
            agent_mod, "resolve_clarification",
            AsyncMock(return_value={"id": "clar-1"}),
        )
        monkeypatch.setattr(
            agent_mod, "get_clarification_history", AsyncMock(return_value=history)
        )

        second = await resume_with_clarification(
            session_id=uuid.UUID(session["id"]),
            user_answer="yes",
            user_id=USER,
            organization_id=ORG,
        )
        assert second.status == ExecutionStatus.COMPLETED
        # Exactly ONE sale tool run, carrying the resolved account id…
        assert len(flow["tool_runs"]) == 1
        tool_name, tool_args = flow["tool_runs"][0]
        assert tool_name == "record_cash_sale"
        assert tool_args.get("revenue_account_id")
        # …which is EXACTLY the ONE dedicated ledger created:
        ledgers = [a for a in chart.accounts if a["name"] == "chairs Sales"]
        assert len(ledgers) == 1
        assert tool_args["revenue_account_id"] == ledgers[0]["id"]
        assert chart.insert_attempts == 1

    @pytest.mark.asyncio
    async def test_migration_079_file_is_forward_only_and_idempotent(self):
        import pathlib
        sql = (
            pathlib.Path(__file__).resolve().parents[2]
            / "database" / "migrations"
            / "079_revenue_review_and_resume_idempotency.sql"
        ).read_text(encoding="utf-8")
        assert "create unique index if not exists" in sql
        assert "add column plan jsonb" in sql
        assert "if not exists" in sql.lower()
        assert "drop " not in sql.lower()
        assert "delete from" not in sql.lower()


# ---------------------------------------------------------------------------
# Sale journal — Dr Cash / Cr reviewed ledger, balanced, verified from DB
# ---------------------------------------------------------------------------

class TestSaleJournalBalancedAndVerified:
    @pytest.mark.asyncio
    async def test_sale_journal_is_balanced_and_db_verified(self, chart, monkeypatch):
        import app.services.accounting_service as accounting_service
        from app.repositories import account_repository as real_repo
        from app.accounting_engine import verify_journal
        from app.tools import _record_cash_sale

        # 1. The user approves CREATE — exactly one dedicated ledger.
        reviewed, created = await rls.apply_decision(ORG, "chairs", "CREATE")
        assert created is True
        cash = {"id": str(uuid.uuid4()), "name": "Cash", "account_type": "ASSET"}

        # 2. Stub ONLY the persistence boundary; the engine's line building,
        #    the balance check and verify_journal's logic run for real.
        captured: dict = {}

        async def fake_resolve(organization_id, *, account_name):
            return cash if account_name == "Cash" else None

        async def fake_chart(organization_id, *, account_type=None, limit=200, **kw):
            rows = chart.accounts
            if account_type:
                rows = [r for r in rows if r.get("account_type") == account_type]
            return rows[:limit]

        async def fake_prepare_journal(**kw):
            lines = kw["lines"]
            td = sum(float(l.get("debit", 0)) for l in lines)
            tc = sum(float(l.get("credit", 0)) for l in lines)
            entry = {
                "id": str(uuid.uuid4()),
                "journal_number": "JE-0001",
                "status": "DRAFT",
            }
            captured.update(lines=lines, total_debit=td, total_credit=tc, entry=entry)
            return {"entry": entry, "lines": lines, "total_debit": td, "total_credit": tc}

        async def fake_get_entry(organization_id, *, entry_id):
            return captured["entry"] if str(entry_id) == captured["entry"]["id"] else None

        async def fake_get_lines(*, entry_id):
            return captured["lines"] if str(entry_id) == captured["entry"]["id"] else []

        monkeypatch.setattr(accounting_service, "resolve_account", fake_resolve)
        monkeypatch.setattr(accounting_service, "prepare_journal", fake_prepare_journal)
        monkeypatch.setattr(accounting_service, "get_entry", fake_get_entry)
        monkeypatch.setattr(accounting_service, "get_lines", fake_get_lines)
        monkeypatch.setattr(real_repo, "get_chart_of_accounts", fake_chart)

        # 3. Run the trusted sale tool with the REVIEWED revenue account.
        result = await _record_cash_sale(
            ORG, amount=3000.0, transaction_date="2026-09-18",
            description="chairs", revenue_account_id=reviewed["id"],
        )
        assert result.success is True
        assert result.data["revenue_account"] == "chairs Sales"
        assert result.data["revenue_account_source"] == "reviewed"

        # 4. The journal is balanced and credits EXACTLY the reviewed ledger.
        assert abs(captured["total_debit"] - captured["total_credit"]) < 0.01
        assert captured["total_debit"] == 3000.0
        debit_line = next(l for l in captured["lines"] if float(l.get("debit", 0)) > 0)
        credit_line = next(l for l in captured["lines"] if float(l.get("credit", 0)) > 0)
        assert debit_line["account_id"] == cash["id"]
        assert credit_line["account_id"] == reviewed["id"]

        # 5. Verification against the (stubbed) database succeeds — the
        #    response may honestly say VERIFIED.
        vr = await verify_journal(
            ORG, entry_id=uuid.UUID(captured["entry"]["id"])
        )
        assert vr["verified"] is True
        assert vr["total_debit"] == 3000.0
        assert vr["total_credit"] == 3000.0
