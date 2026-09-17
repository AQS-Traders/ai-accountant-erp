"""
ERP AI Agent — Control-Plane Writer Contract Tests
===================================================
Verifies that every ai.* execution-table writer in database.py sends the
exact columns that exist in the live Supabase schema (verified against
information_schema on 2026-08-29). Regression guard for the column-mismatch
blocker (audit finding C1) and the phase/status enum conflation (C2).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import app.database as db
from app.agent import _PHASE_STATUS_MAP
from app.models.schemas import ExecutionStatus, SessionStatus

SESSION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

VALID_SESSION_STATUSES = {s.value for s in SessionStatus}
VALID_STEP_TYPES = {
    "REASON", "RETRIEVE", "VALIDATE", "CONFIRM", "EXECUTE", "VERIFY", "RESPOND",
}

# Rows mirroring the live ai.agents / ai.instruction_versions seeds.
AGENT_ROW = {
    "id": "4c706ac0-d865-4067-afa4-21f78bacc5f0",
    "current_instruction_version": "1.2.0",
    "default_model_configuration_id": "b31b65b9-7c7c-4e18-9cce-d8f049c24eed",
}
INSTRUCTION_VERSION_ROW = {"id": "bdc58091-c125-426e-842d-655c1657a9d7"}


class Recorder:
    """Async mock capturing every insert/update made by the writers."""

    def __init__(self):
        self.inserts = []  # (table_name, data)
        self.updates = []  # (table_name, row_id, data)
        self.fetch_one_results = []  # consumed in order
        self.fetch_many_tables = {}  # table_name -> rows

    async def insert_one(self, table_name, *, data):
        self.inserts.append((table_name, data))
        return {"id": str(uuid.uuid4()), **data}

    async def update_one(self, table_name, *, row_id, data):
        self.updates.append((table_name, row_id, data))
        return {"id": str(row_id), **data}

    async def fetch_one(self, table_name, *, filters, select="*"):
        if self.fetch_one_results:
            return self.fetch_one_results.pop(0)
        return None

    async def fetch_many(self, table_name, *, filters, select="*", order=None,
                         limit=100, offset=0):
        return self.fetch_many_tables.get(table_name, [])


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(db, "insert_one", rec.insert_one)
    monkeypatch.setattr(db, "update_one", rec.update_one)
    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "fetch_many", rec.fetch_many)
    # Reset module-level caches so each test resolves ids freshly.
    monkeypatch.setattr(db, "_CONTROL_PLANE_IDS", None)
    monkeypatch.setattr(db, "_TOOL_ID_CACHE", None)
    return rec


# ---------------------------------------------------------------------------
# create_execution_session
# ---------------------------------------------------------------------------

def test_create_execution_session_payload(recorder):
    recorder.fetch_one_results = [AGENT_ROW, INSTRUCTION_VERSION_ROW]

    asyncio.run(db.create_execution_session(
        organization_id=ORG_ID,
        user_id=USER_ID,
        user_message="I bought a laptop from ABC Computers for Rs.150,000 on credit.",
        conversation_id="conv-1",
    ))

    table, data = recorder.inserts[0]
    assert table == "ai_execution_sessions"
    assert data["user_request"].startswith("I bought")
    assert data["status"] == "PENDING"
    assert data["current_phase"] == "RECEIVED"
    assert data["conversation_id"] == "conv-1"
    # FK ids resolved from ai.agents, not hardcoded or omitted.
    assert data["agent_id"] == AGENT_ROW["id"]
    assert data["instruction_version_id"] == INSTRUCTION_VERSION_ROW["id"]
    assert data["model_configuration_id"] == AGENT_ROW["default_model_configuration_id"]
    # Columns that do NOT exist in ai.execution_sessions must never be sent.
    for forbidden in ("user_message", "constitution_version", "model_configuration"):
        assert forbidden not in data


def test_create_execution_session_requires_active_agent(recorder):
    recorder.fetch_one_results = []  # no agent row

    with pytest.raises(RuntimeError, match="ACTIVE agent"):
        asyncio.run(db.create_execution_session(
            organization_id=ORG_ID, user_id=USER_ID, user_message="hello",
        ))
    assert recorder.inserts == []


# ---------------------------------------------------------------------------
# create_execution_step
# ---------------------------------------------------------------------------

def test_create_execution_step_payload_and_order(recorder):
    recorder.fetch_many_tables["ai_execution_steps"] = [
        {"id": "step-1"}, {"id": "step-2"},
    ]

    asyncio.run(db.create_execution_step(
        session_id=SESSION_ID,
        step_type="CONTEXT_LOADING",
        step_data={"accounts": 5},
    ))

    table, data = recorder.inserts[0]
    assert table == "ai_execution_steps"
    assert data["execution_session_id"] == str(SESSION_ID)
    assert data["step_order"] == 3  # after two existing steps
    assert data["step_type"] == "RETRIEVE"  # CONTEXT_LOADING maps to RETRIEVE
    assert data["description"] == "CONTEXT_LOADING"
    assert data["status"] == "COMPLETED"
    assert "session_id" not in data
    assert "step_data" not in data


def test_step_type_map_only_emits_valid_enum_values():
    phases = {p.value for p in ExecutionStatus}
    assert set(db._STEP_TYPE_MAP) == phases
    assert set(db._STEP_TYPE_MAP.values()) <= VALID_STEP_TYPES


# ---------------------------------------------------------------------------
# create_tool_call
# ---------------------------------------------------------------------------

def test_create_tool_call_resolves_tool_id(recorder):
    tool_uuid = "99999999-9999-9999-9999-999999999999"
    recorder.fetch_many_tables["ai_tools"] = [
        {"id": tool_uuid, "slug": "search_customer"},
    ]

    result = asyncio.run(db.create_tool_call(
        session_id=SESSION_ID,
        tool_name="search_customer",
        tool_input={"query": "ABC"},
        tool_output={"data": "[...]", "success": True},
    ))

    table, data = recorder.inserts[0]
    assert table == "ai_tool_calls"
    assert result is not None
    assert data["tool_id"] == tool_uuid  # FK resolved from slug
    assert data["call_order"] == 1
    assert data["input_payload"] == {"query": "ABC"}
    assert data["output_payload"]["success"] is True
    for forbidden in ("tool_name", "tool_input", "tool_output", "session_id", "step_id"):
        assert forbidden not in data


def test_create_tool_call_unknown_slug_skips_insert(recorder):
    result = asyncio.run(db.create_tool_call(
        session_id=SESSION_ID,
        tool_name="no_such_tool",
        tool_input={},
    ))
    assert result is None
    assert recorder.inserts == []


# ---------------------------------------------------------------------------
# create_clarification / create_confirmation
# ---------------------------------------------------------------------------

def test_create_clarification_payload(recorder):
    asyncio.run(db.create_clarification(
        session_id=SESSION_ID,
        question="Cash or credit?",
        required_fields=["payment_terms"],
    ))

    # Session phase update happens first.
    upd_table, upd_id, upd_data = recorder.updates[0]
    assert upd_table == "ai_execution_sessions"
    assert upd_data["current_phase"] == "AWAITING_CLARIFICATION"
    assert upd_data["status"] == "WAITING_FOR_USER"

    table, data = recorder.inserts[0]
    assert table == "ai_clarifications"
    assert data["execution_session_id"] == str(SESSION_ID)
    assert data["question"] == "Cash or credit?"
    assert data["required_information"] == ["payment_terms"]
    assert data["options"] == []
    assert data["status"] == "WAITING_FOR_USER"
    for forbidden in ("session_id", "required_fields"):
        assert forbidden not in data


def test_create_confirmation_payload(recorder):
    asyncio.run(db.create_confirmation(
        session_id=SESSION_ID,
        action_type="record_credit_purchase",
        description="Execute: record_credit_purchase — ABC Computers",
        risk_level="HIGH",
    ))

    upd_table, _, upd_data = recorder.updates[0]
    assert upd_data["current_phase"] == "AWAITING_CONFIRMATION"
    assert upd_data["status"] == "WAITING_FOR_USER"

    table, data = recorder.inserts[0]
    assert table == "ai_confirmations"
    assert data["execution_session_id"] == str(SESSION_ID)
    assert data["action_type"] == "record_credit_purchase"
    assert data["description"].startswith("Execute:")
    assert data["risk_level"] == "HIGH"
    assert data["confirmation_required"] is True
    for forbidden in ("action_description", "confirmation_data", "session_id", "status"):
        assert forbidden not in data


def test_resolve_confirmation_marks_pending_row(recorder):
    confirmation_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    recorder.fetch_many_tables["ai_confirmations"] = [
        {"id": confirmation_id, "user_confirmed": None},
    ]

    asyncio.run(db.resolve_confirmation(
        session_id=SESSION_ID, approved=True, user_id=USER_ID,
    ))

    upd_table, upd_id, upd_data = recorder.updates[0]
    assert upd_table == "ai_confirmations"
    assert upd_id == uuid.UUID(confirmation_id)
    assert upd_data["user_confirmed"] is True
    assert "confirmed_at" in upd_data


# ---------------------------------------------------------------------------
# create_execution_result
# ---------------------------------------------------------------------------

def test_create_execution_result_payload(recorder):
    asyncio.run(db.create_execution_result(
        session_id=SESSION_ID,
        result_data={"summary": "done", "tool_count": 1},
        summary="done",
        action_type="record_credit_purchase",
        affected_entities=[{"tool": "prepare_journal", "id": "je-1"}],
        verification_status="VERIFIED",
        status="COMPLETED",
    ))

    table, data = recorder.inserts[0]
    assert table == "ai_execution_results"
    assert data["execution_session_id"] == str(SESSION_ID)
    assert data["result_payload"]["tool_count"] == 1
    assert data["summary"] == "done"
    assert data["action_type"] == "record_credit_purchase"
    assert data["affected_entities"][0]["id"] == "je-1"
    assert data["verification_status"] == "VERIFIED"
    assert data["status"] == "COMPLETED"
    assert data["completed_at"]  # timestamp present
    for forbidden in ("session_id", "result_data"):
        assert forbidden not in data


# ---------------------------------------------------------------------------
# Phase → status mapping (agent)
# ---------------------------------------------------------------------------

def test_phase_status_map_covers_all_phases():
    assert set(_PHASE_STATUS_MAP) == set(ExecutionStatus)


def test_phase_status_map_statuses_valid():
    for status, completed in _PHASE_STATUS_MAP.values():
        assert status in VALID_SESSION_STATUSES
        assert isinstance(completed, bool)


def test_terminal_phases_close_session():
    for phase in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED,
                  ExecutionStatus.CANCELLED, ExecutionStatus.REJECTED):
        status, completed = _PHASE_STATUS_MAP[phase]
        assert completed is True
        assert status in {"COMPLETED", "FAILED", "CANCELLED"}


def test_awaiting_phases_pause_session():
    for phase in (ExecutionStatus.AWAITING_CLARIFICATION,
                  ExecutionStatus.AWAITING_CONFIRMATION):
        status, completed = _PHASE_STATUS_MAP[phase]
        assert status == "WAITING_FOR_USER"
        assert completed is False
