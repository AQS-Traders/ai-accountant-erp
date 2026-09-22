"""Regression tests — tool-failure observability + financial-claim terminalisation.

Production incident (session 6a48a432-d9d9-480c-8e47-e6799305bc6f)
=================================================================

``create_invoice`` raised inside the trusted execution path::

    TypeError: create_invoice() got an unexpected keyword argument 'line_items'

The approved plan (ai.confirmations.plan, session 28f2e338) carried LLM-chosen
argument names — ``line_items``, ``customer_name``, ``tax_category`` — none of
which the service signature accepts, so Python failed at CALL-BINDING time,
before any database work: no invoice row, no journal, and only ~1 s between the
idempotency claim (18:26:34.616) and the recorded failure.

Two defects then made that failure unroot-causable:

* ``app/tool_router.py``'s generic ``except Exception`` returned a ToolResult
  WITHOUT releasing the financial claim, so ``public.financial_operations``
  row 59777966-aef6-4672-adfd-ba86971e60ff stayed ``IN_PROGRESS`` with
  ``error = NULL`` — a zombie claim that hid the cause AND blocked the
  legitimate retry for the whole stale window;
* ``ai.tool_calls.error_details`` was structurally un-writable: the writer had
  no parameter for it and the caller dropped ``ToolResult.error``, so all 26
  FAILED tool-call rows in production hold NULL.

These tests pin the corrected invariants:

* every failure path AFTER the claim terminalises it, storing the RAW
  exception type + message (Tests A, B, C, D);
* the model-facing message stays sanitised while the real cause is recorded
  (Test A — internals must not leak into the model's context, but the ledger
  must keep the truth);
* a successful call's claim handling is unchanged (Test E);
* a FAILED tool call always reaches ``ai.tool_calls`` with a non-null reason,
  and the DB writer actually sends the column (Tests F, G).

No live provider, no live database — boundaries are stubbed.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import app.database as db
from app.agent import _tool_audit_error_text
from app.models.schemas import ToolCall, ToolResult

ORG = uuid.uuid4()
USER = uuid.uuid4()
SESSION = uuid.uuid4()

# The exact production failure (verbatim): the LLM-plan path passed argument
# names the Python contract does not define.
PRODUCTION_TYPE_ERROR = (
    "create_invoice() got an unexpected keyword argument 'line_items'"
)


# ---------------------------------------------------------------------------
# Router harness (mirrors app/tests/test_idempotency_and_atomicity.py)
# ---------------------------------------------------------------------------


def _auth():
    from app.auth import AuthContext

    return AuthContext(
        user_id=USER, organization_id=ORG,
        role_code="ACCOUNTANT", role_permissions=["ai:full"],
    )


def _stub_security(monkeypatch, tr):
    from types import SimpleNamespace

    async def _permit(slug, *, auth):
        return True

    async def _valid(*, organization_id, operation, data):
        return SimpleNamespace(valid=True, errors=[])

    monkeypatch.setattr(tr, "authorize_tool", _permit)
    monkeypatch.setattr(tr, "validate_operation", _valid)


async def _route(tr, handler, monkeypatch, arguments=None):
    monkeypatch.setattr(
        tr, "get_handler", lambda s: {"handler": handler, "read_only": False}
    )
    return await tr.route_tool_call(
        ToolCall(
            tool_name="create_invoice",
            arguments=arguments if arguments is not None else {"amount": 100},
        ),
        organization_id=ORG, user_id=USER, session_id=SESSION, auth=_auth(),
    )


def _claim_recorder():
    """Return (fake_claim, fake_safe_complete, ledger) capturing the outcome."""
    ledger = {}

    async def _claim(**kw):
        return {"state": "claimed", "id": "op-1"}

    async def _safe_complete(op_id, *, result=None, error=None):
        ledger.update({"op": op_id, "result": result, "error": error})

    return _claim, _safe_complete, ledger


# ---------------------------------------------------------------------------
# DEFECT: a failed mutation left its claim IN_PROGRESS forever
# ---------------------------------------------------------------------------


class TestClaimTerminalisation:
    @pytest.mark.asyncio
    async def test_unexpected_exception_closes_the_claim_with_the_raw_cause(
        self, monkeypatch
    ):
        """Test A — the production shape: TypeError from a bad argument name."""
        import app.tool_router as tr

        _stub_security(monkeypatch, tr)
        _claim, _safe_complete, ledger = _claim_recorder()
        monkeypatch.setattr(tr.idem, "claim", _claim)
        monkeypatch.setattr(tr.idem, "safe_complete", _safe_complete)

        async def handler(org, **kw):
            raise TypeError(PRODUCTION_TYPE_ERROR)

        res = await _route(tr, handler, monkeypatch)

        # The caller still sees a failure with a sanitised message…
        assert res.success is False
        assert res.error
        assert "line_items" not in res.error  # no internals leaked to the model
        # …while the claim is terminalised with the RAW cause.
        assert ledger["op"] == "op-1"
        assert ledger["result"] is None
        assert "TypeError" in ledger["error"]
        assert "line_items" in ledger["error"]

    @pytest.mark.asyncio
    async def test_every_post_claim_exception_type_terminalises(self, monkeypatch):
        """Test B — no exception type may leave a zombie claim behind."""
        import app.tool_router as tr

        for exc in (
            TypeError(PRODUCTION_TYPE_ERROR),
            KeyError("id"),
            RuntimeError("connection reset"),
            ValueError("bad value"),
        ):
            _stub_security(monkeypatch, tr)
            _claim, _safe_complete, ledger = _claim_recorder()
            monkeypatch.setattr(tr.idem, "claim", _claim)
            monkeypatch.setattr(tr.idem, "safe_complete", _safe_complete)

            async def handler(org, **kw):
                raise exc

            res = await _route(tr, handler, monkeypatch)

            assert res.success is False, type(exc).__name__
            assert ledger.get("op") == "op-1", (
                f"{type(exc).__name__} left the claim IN_PROGRESS"
            )
            assert type(exc).__name__ in str(ledger["error"])

    @pytest.mark.asyncio
    async def test_date_mapping_failure_also_closes_the_claim(self, monkeypatch):
        """Test C — the guarded region starts at the date mapping, so a raise
        there cannot escape past the claim either."""
        import app.tool_router as tr

        _stub_security(monkeypatch, tr)
        _claim, _safe_complete, ledger = _claim_recorder()
        monkeypatch.setattr(tr.idem, "claim", _claim)
        monkeypatch.setattr(tr.idem, "safe_complete", _safe_complete)

        def _boom(_value):
            raise RuntimeError("date parser exploded")

        monkeypatch.setattr(tr, "parse_transaction_date", _boom)

        ran = {"n": 0}

        async def handler(org, **kw):
            ran["n"] += 1

        # A supplied date is what routes the call through the parser.
        res = await _route(
            tr, handler, monkeypatch, arguments={"invoice_date": "2026-09-20"}
        )

        assert res.success is False
        assert ran["n"] == 0
        assert ledger.get("op") == "op-1"

    @pytest.mark.asyncio
    async def test_no_claim_means_nothing_to_release(self, monkeypatch):
        """Test D — without a claim (no session key) the path is unchanged."""
        import app.tool_router as tr

        _stub_security(monkeypatch, tr)

        async def _claim(**kw):
            return {"state": "claimed"}  # no id -> no claim was taken

        called = {"n": 0}

        async def _safe_complete(op_id, *, result=None, error=None):
            called["n"] += 1

        monkeypatch.setattr(tr.idem, "claim", _claim)
        monkeypatch.setattr(tr.idem, "safe_complete", _safe_complete)

        async def handler(org, **kw):
            raise TypeError(PRODUCTION_TYPE_ERROR)

        res = await _route(tr, handler, monkeypatch)

        assert res.success is False
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_successful_call_claim_handling_is_unchanged(self, monkeypatch):
        """Test E — the success path still stores the result, never an error."""
        import app.tool_router as tr

        _stub_security(monkeypatch, tr)
        _claim, _safe_complete, ledger = _claim_recorder()
        monkeypatch.setattr(tr.idem, "claim", _claim)
        monkeypatch.setattr(tr.idem, "safe_complete", _safe_complete)

        async def handler(org, **kw):
            return ToolResult(
                tool_name="create_invoice", success=True, data={"id": "i1"}
            )

        res = await _route(tr, handler, monkeypatch)

        assert res.success is True
        assert ledger["op"] == "op-1"
        assert ledger["error"] is None
        assert ledger["result"]["id"] == "i1"


# ---------------------------------------------------------------------------
# DEFECT: ai.tool_calls recorded no reason for a FAILED call
# ---------------------------------------------------------------------------


class TestToolCallFailureRecording:
    def test_failure_text_carries_category_message_and_raw_cause(self):
        """Test F — the durable text explains WHY, not just THAT it failed."""
        tr = ToolResult(
            tool_name="create_invoice",
            success=False,
            error="An unexpected error occurred while executing this operation.",
            error_category="UNKNOWN_ERROR",
            error_details={
                "category": "UNKNOWN_ERROR",
                "message": f"TypeError: {PRODUCTION_TYPE_ERROR}",
            },
        )
        text = _tool_audit_error_text(tr)

        assert text
        assert "UNKNOWN_ERROR" in text
        assert "unexpected error" in text
        assert PRODUCTION_TYPE_ERROR in text  # the raw cause survives

    def test_failure_without_a_message_still_records_a_reason(self):
        tr = ToolResult(tool_name="create_invoice", success=False)
        text = _tool_audit_error_text(tr)

        assert text
        assert "create_invoice" in text

    def test_success_records_no_error(self):
        tr = ToolResult(tool_name="create_invoice", success=True, data={"id": "i1"})
        assert _tool_audit_error_text(tr) is None

    def test_missing_result_is_not_an_error_payload(self):
        assert _tool_audit_error_text(None) is None


class _Recorder:
    """Captures the insert payloads of the ai.* writers."""

    def __init__(self):
        self.inserts = []
        self.fetch_many_tables = {}

    async def insert_one(self, table_name, *, data):
        self.inserts.append((table_name, data))
        return {"id": str(uuid.uuid4()), **data}

    async def fetch_one(self, table_name, *, filters, select="*"):
        return None

    async def fetch_many(self, table_name, *, filters, select="*", order=None,
                         limit=100, offset=0):
        return self.fetch_many_tables.get(table_name, [])


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(db, "insert_one", rec.insert_one)
    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "fetch_many", rec.fetch_many)
    monkeypatch.setattr(db, "_TOOL_ID_CACHE", None)
    return rec


def test_create_tool_call_writes_error_details_for_a_failed_call(recorder):
    """Test G — the column is now really sent (historically: 26/26 NULL)."""
    tool_uuid = "99999999-9999-9999-9999-999999999999"
    recorder.fetch_many_tables["ai_tools"] = [
        {"id": tool_uuid, "slug": "create_invoice"},
    ]

    asyncio.run(db.create_tool_call(
        session_id=SESSION,
        tool_name="create_invoice",
        tool_input={"line_items": [{"quantity": 2}]},
        tool_output={"data": "...", "success": False},
        status="FAILED",
        error_details=f"[UNKNOWN_ERROR] raw: TypeError: {PRODUCTION_TYPE_ERROR}",
    ))

    table, data = recorder.inserts[0]
    assert table == "ai_tool_calls"
    assert data["status"] == "FAILED"
    assert data["error_details"]
    assert PRODUCTION_TYPE_ERROR in data["error_details"]


def test_create_tool_call_omits_error_details_when_there_is_none(recorder):
    tool_uuid = "99999999-9999-9999-9999-999999999999"
    recorder.fetch_many_tables["ai_tools"] = [
        {"id": tool_uuid, "slug": "search_customer"},
    ]

    asyncio.run(db.create_tool_call(
        session_id=SESSION,
        tool_name="search_customer",
        tool_input={"query": "ABC"},
        tool_output={"data": "[]", "success": True},
    ))

    _table, data = recorder.inserts[0]
    assert data["status"] == "COMPLETED"
    assert "error_details" not in data
