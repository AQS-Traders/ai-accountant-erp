"""Idempotency and atomic posting for financial mutations.

Pins the replacement of the amount-based duplicate guard (migration 075) and
the single-transaction document posting (migration 076).

The old guard: one tool slug, one execution session, keyed on the AMOUNT, and
nothing persisted — so a retry through a browser, HTTP, the provider, the
worker or a restart produced a duplicate document, while two legitimately
identical transactions in one session were collapsed into one (a MISSING
financial record).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app import idempotency as idem

ORG = uuid.uuid4()
USER = uuid.uuid4()
SESSION = uuid.uuid4()


class TestRequestHash:
    def test_identical_payload_hashes_identically(self):
        a = idem.request_hash("create_invoice", {"amount": 100, "customer": "X"})
        b = idem.request_hash("create_invoice", {"customer": "X", "amount": 100})
        assert a == b, "key order must not change the hash"

    def test_different_payload_hashes_differently(self):
        a = idem.request_hash("create_invoice", {"amount": 100})
        b = idem.request_hash("create_invoice", {"amount": 200})
        assert a != b, "1300 vs 2000 must not be treated as the same request"

    def test_uuid_payload_is_hashable(self):
        """UUIDs (and Decimals) must not blow up the canonicalisation."""
        h = idem.request_hash("create_invoice", {"customer_id": uuid.uuid4()})
        assert isinstance(h, str) and len(h) == 64


class TestDeriveKey:
    def test_explicit_key_wins(self):
        assert idem.derive_key(
            explicit="my-key", session_id=SESSION, operation="create_invoice"
        ) == "my-key"

    def test_derived_key_is_stable_per_run_and_tool(self):
        first = idem.derive_key(
            explicit=None, session_id=SESSION, operation="create_invoice"
        )
        second = idem.derive_key(
            explicit=None, session_id=SESSION, operation="create_invoice"
        )
        assert first == second, "a retry in the same run must collide"

    def test_different_tool_gets_a_different_key(self):
        a = idem.derive_key(explicit=None, session_id=SESSION, operation="create_invoice")
        b = idem.derive_key(explicit=None, session_id=SESSION, operation="record_expense")
        assert a != b

    def test_no_session_and_no_explicit_key_means_no_claim(self):
        assert idem.derive_key(
            explicit=None, session_id=None, operation="create_invoice"
        ) is None


class TestKeyConflictDetection:
    def test_detects_the_database_refusal(self):
        exc = Exception(
            "idempotency key k was already used for create_invoice with "
            "different parameters"
        )
        assert idem.is_key_conflict(exc) is True

    def test_ignores_unrelated_errors(self):
        assert idem.is_key_conflict(Exception("connection reset")) is False


class TestSafeCompleteNeverBreaksTheMutation:
    @pytest.mark.asyncio
    async def test_no_op_without_an_operation_id(self):
        with patch.object(idem, "complete", new=AsyncMock()) as m:
            await idem.safe_complete(None, result={"ok": True})
            m.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_ledger_failure(self):
        """The financial write already happened; a ledger failure is logged."""
        with patch.object(idem, "complete", new=AsyncMock(side_effect=RuntimeError("down"))):
            await idem.safe_complete(uuid.uuid4(), result={"ok": True})

    @pytest.mark.asyncio
    async def test_uuid_payload_is_serialised(self):
        captured = {}

        async def _fake_complete(op_id, *, status, result=None, error=None):
            captured["result"] = result

        with patch.object(idem, "complete", _fake_complete):
            await idem.safe_complete(uuid.uuid4(), result={"id": uuid.uuid4()})
        assert isinstance(captured["result"]["id"], str)


# ---------------------------------------------------------------------------
# Router integration: the claim drives the outcome.
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


async def _route(tr, handler, monkeypatch):
    from app.models.schemas import ToolCall

    monkeypatch.setattr(
        tr, "get_handler", lambda s: {"handler": handler, "read_only": False}
    )
    return await tr.route_tool_call(
        ToolCall(tool_name="create_invoice", arguments={"amount": 100}),
        organization_id=ORG, user_id=USER, session_id=SESSION, auth=_auth(),
    )


class TestRouterIdempotency:
    @pytest.mark.asyncio
    async def test_replay_returns_the_original_result_without_re_executing(self, monkeypatch):
        import app.tool_router as tr

        ran = {"n": 0}

        async def handler(org, **kw):
            ran["n"] += 1

        _stub_security(monkeypatch, tr)

        async def _claim(**kw):
            return {"state": "replay", "result": {"invoice_number": "INV-1"}}

        monkeypatch.setattr(tr.idem, "claim", _claim)

        res = await _route(tr, handler, monkeypatch)
        assert res.success is True
        assert res.data["invoice_number"] == "INV-1"
        assert res.data["replayed"] is True
        assert ran["n"] == 0, "a replay must NOT execute the handler again"

    @pytest.mark.asyncio
    async def test_in_progress_is_refused_not_repeated(self, monkeypatch):
        import app.tool_router as tr

        ran = {"n": 0}

        async def handler(org, **kw):
            ran["n"] += 1

        _stub_security(monkeypatch, tr)

        async def _claim(**kw):
            return {"state": "in_progress"}

        monkeypatch.setattr(tr.idem, "claim", _claim)

        res = await _route(tr, handler, monkeypatch)
        assert res.success is False
        assert "already being processed" in res.error
        assert ran["n"] == 0

    @pytest.mark.asyncio
    async def test_reused_key_with_a_different_payload_is_refused(self, monkeypatch):
        import app.tool_router as tr

        ran = {"n": 0}

        async def handler(org, **kw):
            ran["n"] += 1

        _stub_security(monkeypatch, tr)

        async def _claim(**kw):
            raise Exception(
                "idempotency key k was already used for create_invoice with "
                "different parameters"
            )

        monkeypatch.setattr(tr.idem, "claim", _claim)

        res = await _route(tr, handler, monkeypatch)
        assert res.success is False
        assert "already used for a different request" in res.error
        assert ran["n"] == 0

    @pytest.mark.asyncio
    async def test_claimed_executes_and_records_the_outcome(self, monkeypatch):
        import app.tool_router as tr
        from app.models.schemas import ToolResult

        _stub_security(monkeypatch, tr)
        completed = {}

        async def handler(org, **kw):
            return ToolResult(tool_name="create_invoice", success=True, data={"id": "i1"})

        async def _claim(**kw):
            return {"state": "claimed", "id": "op-1"}

        async def _safe_complete(op_id, *, result=None, error=None):
            completed.update({"op": op_id, "result": result, "error": error})

        monkeypatch.setattr(tr.idem, "claim", _claim)
        monkeypatch.setattr(tr.idem, "safe_complete", _safe_complete)

        res = await _route(tr, handler, monkeypatch)
        assert res.success is True
        assert completed["op"] == "op-1"
        # The stored result is what the caller saw, including the date
        # disclosure added by the transaction-date protocol.
        assert completed["result"]["id"] == "i1"
        assert completed["result"]["date_defaulted"] is True
        assert completed["error"] is None

    @pytest.mark.asyncio
    async def test_ledger_failure_never_blocks_the_mutation(self, monkeypatch):
        """A claim outage must not stop a legitimate financial write."""
        import app.tool_router as tr
        from app.models.schemas import ToolResult

        _stub_security(monkeypatch, tr)

        async def handler(org, **kw):
            return ToolResult(tool_name="create_invoice", success=True, data={"id": "i1"})

        async def _claim(**kw):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(tr.idem, "claim", _claim)

        res = await _route(tr, handler, monkeypatch)
        assert res.success is True