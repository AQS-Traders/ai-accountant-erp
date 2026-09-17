"""
ERP AI Agent — Resume-by-conversation staleness tests
=====================================================
Regression guard for the "the agent runs for ever without any input" defect.

A run only advances while its serverless invocation is alive. When the host
kills that invocation (FUNCTION_INVOCATION_TIMEOUT / cold-start eviction)
nothing is left to write the terminal state, so the row stays PENDING /
PLANNING / EXECUTING for ever. GET /api/ai/sessions/latest-active used to
return such a row, so EVERY dashboard load re-attached to a dead run: an
endless "Processing" spinner that polled the API in a loop with no user input
involved at all (the observed live defect: a 10-hour-old WAITING_FOR_USER row
plus a 22-hour-old EXECUTING row both re-attached as "in progress").

The endpoint must therefore:
  * never re-attach a run that stopped moving,
  * close it (FAILED) so the leak cannot accumulate,
  * still re-attach genuinely live runs, including one parked on a question.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.main as main
from app.main import _ACTIVE_RUN_TTL_SECONDS, _AWAITING_USER_TTL_SECONDS

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _auth() -> SimpleNamespace:
    """The endpoint only reads organization_id / user_id from the AuthContext."""
    return SimpleNamespace(organization_id=ORG, user_id=USER)


def _row(*, status: str, age_seconds: float, phase: str | None = None) -> dict:
    """One ai.execution_sessions row, last written ``age_seconds`` ago."""
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "status": status,
        "current_phase": phase or status,
        "created_at": stamp,
        "updated_at": stamp,
    }


class _Recorder:
    """Fake DB layer: serves canned session rows, records every closure."""

    def __init__(self, rows):
        self.rows = rows
        self.closed: list[tuple] = []
        self.filters: dict | None = None

    async def fetch_many(self, table_name, *, filters, select="*", order=None,
                         limit=100, offset=0):
        assert table_name == "ai_execution_sessions"
        self.filters = filters
        return list(self.rows)

    async def set_session_phase(self, session_id, *, phase, status, completed=False):
        self.closed.append((str(session_id), phase, status, completed))
        return {"id": str(session_id)}


@pytest.fixture
def recorder(monkeypatch):
    """Install canned rows on the endpoint's DB touch-points."""

    def _install(rows):
        rec = _Recorder(rows)
        monkeypatch.setattr(main, "fetch_many", rec.fetch_many)
        monkeypatch.setattr(main, "set_session_phase", rec.set_session_phase)
        return rec

    return _install


# ---------------------------------------------------------------------------
# Live runs are still re-attached
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_executing_run_is_returned(recorder):
    rec = recorder([_row(status="EXECUTING", age_seconds=5)])
    out = await main.latest_active_session(_auth())
    assert out["found"] is True
    assert out["session"]["status"] == "EXECUTING"
    assert rec.closed == []


@pytest.mark.asyncio
async def test_fresh_question_run_is_still_returned(recorder):
    """A user who is mid-question keeps their run when they refresh."""
    rec = recorder([_row(status="WAITING_FOR_USER", age_seconds=120)])
    out = await main.latest_active_session(_auth())
    assert out["found"] is True
    assert out["session"]["status"] == "WAITING_FOR_USER"
    assert rec.closed == []


# ---------------------------------------------------------------------------
# Stranded runs are closed, never re-attached
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stranded_executing_run_is_closed_and_not_returned(recorder):
    row = _row(status="EXECUTING", age_seconds=_ACTIVE_RUN_TTL_SECONDS + 60)
    rec = recorder([row])
    out = await main.latest_active_session(_auth())
    assert out == {"found": False, "session": None}
    assert rec.closed == [(row["id"], "FAILED", "FAILED", True)]


@pytest.mark.asyncio
async def test_stranded_question_run_is_closed(recorder):
    """The exact live defect: a 10-hour-old unanswered question."""
    row = _row(status="WAITING_FOR_USER", age_seconds=_AWAITING_USER_TTL_SECONDS + 60)
    rec = recorder([row])
    out = await main.latest_active_session(_auth())
    assert out == {"found": False, "session": None}
    assert rec.closed == [(row["id"], "FAILED", "FAILED", True)]


@pytest.mark.asyncio
async def test_terminal_runs_are_never_returned_nor_closed(recorder):
    rec = recorder([
        _row(status="COMPLETED", age_seconds=1),
        _row(status="CANCELLED", age_seconds=1),
        _row(status="FAILED", age_seconds=1),
    ])
    out = await main.latest_active_session(_auth())
    assert out == {"found": False, "session": None}
    assert rec.closed == []


@pytest.mark.asyncio
async def test_scan_walks_past_stranded_rows_to_the_live_one(recorder):
    """A stranded run must not mask a genuinely live run behind it."""
    stranded = _row(status="EXECUTING", age_seconds=_ACTIVE_RUN_TTL_SECONDS + 60)
    live = _row(status="PLANNING", age_seconds=3)
    rec = recorder([stranded, live])
    out = await main.latest_active_session(_auth())
    assert out["found"] is True
    assert out["session"]["session_id"] == live["id"]
    assert rec.closed == [(stranded["id"], "FAILED", "FAILED", True)]


@pytest.mark.asyncio
async def test_returned_session_reports_age_for_the_client(recorder):
    recorder([_row(status="EXECUTING", age_seconds=42)])
    out = await main.latest_active_session(_auth())
    assert 40 <= out["session"]["age_seconds"] <= 60
    assert out["session"]["updated_at"]


@pytest.mark.asyncio
async def test_scan_stays_org_and_user_scoped(recorder):
    rec = recorder([])
    await main.latest_active_session(_auth())
    assert rec.filters == {"organization_id": str(ORG), "user_id": str(USER)}


@pytest.mark.asyncio
async def test_a_failed_closure_never_breaks_the_resume_endpoint(monkeypatch):
    """Best-effort by design: the read endpoint must stay available."""
    async def _boom(session_id, *, phase, status, completed=False):
        raise RuntimeError("supabase down")

    async def _rows(table_name, *, filters, select="*", order=None,
                    limit=100, offset=0):
        return [_row(status="EXECUTING", age_seconds=_ACTIVE_RUN_TTL_SECONDS + 60)]

    monkeypatch.setattr(main, "fetch_many", _rows)
    monkeypatch.setattr(main, "set_session_phase", _boom)
    out = await main.latest_active_session(_auth())
    assert out == {"found": False, "session": None}


# ---------------------------------------------------------------------------
# Age parsing
# ---------------------------------------------------------------------------

def test_row_age_seconds_parses_zulu_naive_and_garbage():
    now = datetime.now(timezone.utc)
    zulu = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    assert main._row_age_seconds({"updated_at": zulu}) == pytest.approx(30, abs=5)

    naive = (now.replace(tzinfo=None) - timedelta(seconds=30)).isoformat()
    assert main._row_age_seconds({"updated_at": naive}) == pytest.approx(30, abs=5)

    # Falls back to the older timestamps when updated_at is missing.
    assert main._row_age_seconds(
        {"created_at": (now - timedelta(seconds=10)).isoformat()}
    ) == pytest.approx(10, abs=5)

    assert main._row_age_seconds({"updated_at": "not-a-date"}) is None
    assert main._row_age_seconds({}) is None