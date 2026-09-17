"""
ERP AI Agent - Database Layer Non-Blocking Execution Tests
==========================================================
The supabase-py client is synchronous httpx under the hood.  database.py
must run every ``.execute()`` through ``asyncio.to_thread`` (the
``_execute`` helper) so that:

* the event loop is never blocked by a REST round-trip, and
* ``asyncio.gather(...)`` fan-outs (context build, audit writes) actually
  run CONCURRENTLY instead of degrading to sequential calls.

Regression for the stage-2 latency bug: blocking ``.execute()`` calls on
the loop made the gathered context fetches sequential (~19s) and could
stall every in-flight request behind one slow call.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.database import _execute, fetch_many


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Stands in for a supabase-py query builder terminal state."""

    def __init__(self, data, log):
        self._data = data
        self._log = log

    def execute(self):
        entry = [
            threading.current_thread() is threading.main_thread(),
            time.monotonic(),
        ]
        self._log.append(entry)
        time.sleep(0.15)  # simulate a real REST round-trip
        entry.append(time.monotonic())
        return _FakeResult(self._data)


@pytest.mark.asyncio
async def test_execute_runs_off_the_event_loop():
    log = []
    result = await _execute(_FakeQuery([{"id": "1"}], log))
    assert result.data == [{"id": "1"}]
    # The sync .execute() must NOT run on the main (event-loop) thread.
    assert log[0][0] is False


@pytest.mark.asyncio
async def test_gathered_executes_overlap():
    """Two gathered queries must run concurrently (thread pool), so the
    wall time is ~one round-trip, not the sum of both."""
    log = []
    results = await asyncio.gather(
        _execute(_FakeQuery([{"id": "1"}], log)),
        _execute(_FakeQuery([{"id": "2"}], log)),
    )
    assert [r.data[0]["id"] for r in results] == ["1", "2"]
    assert len(log) == 2
    (_, start_a, end_a), (_, start_b, end_b) = log
    # The two round-trips must overlap in time.
    assert min(end_a, end_b) - max(start_a, start_b) > 0


@pytest.mark.asyncio
async def test_fetch_many_routes_through_execute(monkeypatch):
    """fetch_many must await the _execute helper (never call the sync
    .execute() inline on the event loop)."""

    class _FakeTable:
        def select(self, *_a, **_kw):
            return self

        def eq(self, *_a, **_kw):
            return self

        def order(self, *_a, **_kw):
            return self

        def range(self, *_a, **_kw):
            return self

    class _FakeClient:
        def table(self, _name):
            return _FakeTable()

    import app.database as db

    executed = []

    async def fake_execute(query):
        executed.append(query)
        return _FakeResult([{"id": "r1", "name": "Row"}])

    monkeypatch.setattr(db, "_execute", fake_execute)
    monkeypatch.setattr(db, "get_service_client", lambda: _FakeClient())

    rows = await fetch_many("customers", filters={"organization_id": "org"})
    assert rows == [{"id": "r1", "name": "Row"}]
    assert len(executed) == 1
