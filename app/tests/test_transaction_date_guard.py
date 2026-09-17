"""Work Stream A - route_tool_call transaction-date guard tests."""

import asyncio
import uuid
from datetime import date

import app.tool_router as tr
from app.models.schemas import ToolCall, ToolResult


def _auth():
    """A minimal authorization context for mutation routing."""
    from app.auth import AuthContext

    return AuthContext(
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        role_code="ACCOUNTANT",
        role_permissions=["accounting:full", "ai:full", "sales:manage"],
    )


def _patch_security(monkeypatch):
    """Stub the security layers so these tests stay hermetic.

    ``route_tool_call`` now REQUIRES an authorization context for mutations
    (a missing context previously skipped the permission check AND parameter
    validation).  With a context present it also runs parameter validation.
    These tests exercise the transaction-date guard only, so both layers are
    stubbed to a pass and no database is touched.
    """
    from types import SimpleNamespace

    async def _permit(slug, *, auth):
        return True

    async def _valid(*, organization_id, operation, data):
        return SimpleNamespace(valid=True, errors=[])

    monkeypatch.setattr(tr, "authorize_tool", _permit)
    monkeypatch.setattr(tr, "validate_operation", _valid)


def _route(monkeypatch, handler, read_only, slug, arguments):
    monkeypatch.setattr(
        tr, "get_handler", lambda s: {"handler": handler, "read_only": read_only}
    )
    _patch_security(monkeypatch)
    auth = _auth()
    return asyncio.run(
        tr.route_tool_call(
            ToolCall(tool_name=slug, arguments=arguments),
            organization_id=auth.organization_id,
            user_id=auth.user_id,
            auth=auth,
        )
    )


class TestMutationDateGuard:
    def test_missing_date_defaults_to_today_and_is_flagged(self, monkeypatch):
        captured = {}

        async def handler(org, **kw):
            captured.update(kw)
            return ToolResult(tool_name="create_expense", success=True, data={"id": "e1"})

        res = _route(
            monkeypatch, handler, False, "create_expense", {"amount": 5000.0}
        )
        assert res.success
        assert captured["expense_date"] == date.today().isoformat()
        # Honest defaulting: the result states the assumed date.
        assert res.data["date_defaulted"] is True
        assert res.data["transaction_date"] == date.today().isoformat()

    def test_transaction_date_maps_to_native_param(self, monkeypatch):
        captured = {}

        async def handler(org, **kw):
            captured.update(kw)
            return ToolResult(tool_name="create_invoice", success=True, data={"id": "i1"})

        res = _route(
            monkeypatch,
            handler,
            False,
            "create_invoice",
            {"transaction_date": "2026-08-15", "customer_id": "c1"},
        )
        assert res.success
        assert captured["invoice_date"] == "2026-08-15"
        assert "date_defaulted" not in (res.data or {})

    def test_explicit_native_date_is_untouched(self, monkeypatch):
        captured = {}

        async def handler(org, **kw):
            captured.update(kw)
            return ToolResult(tool_name="create_invoice", success=True, data={"id": "i1"})

        _route(
            monkeypatch,
            handler,
            False,
            "create_invoice",
            {"invoice_date": "2026-03-01"},
        )
        assert captured["invoice_date"] == "2026-03-01"

    def test_invalid_date_still_defaults_honestly(self, monkeypatch):
        captured = {}

        async def handler(org, **kw):
            captured.update(kw)
            return ToolResult(tool_name="create_expense", success=True, data={})

        _route(
            monkeypatch, handler, False, "create_expense",
            {"expense_date": "31/02/2026"},
        )
        assert captured["expense_date"] == date.today().isoformat()

    def test_read_only_tools_are_never_touched(self, monkeypatch):
        captured = {}

        async def handler(org, **kw):
            captured.update(kw)
            return ToolResult(tool_name="list_bank_accounts", success=True, data={})

        _route(monkeypatch, handler, True, "list_bank_accounts", {})
        assert "transaction_date" not in captured


class TestClosedPeriodGuidance:
    def test_generic_period_error_gets_guidance(self, monkeypatch):
        async def handler(org, **kw):
            raise RuntimeError("Accounting period 2025-12 is CLOSED for posting")

        monkeypatch.setattr(
            tr, "get_handler",
            lambda s: {"handler": handler, "read_only": False},
        )
        monkeypatch.setattr(
            tr, "normalize_error",
            lambda exc, operation: {"category": "OTHER", "reason": "x"},
        )
        _patch_security(monkeypatch)
        auth = _auth()
        res = asyncio.run(
            tr.route_tool_call(
                ToolCall(tool_name="create_expense", arguments={"expense_date": "2025-12-05"}),
                organization_id=auth.organization_id,
                user_id=auth.user_id,
                auth=auth,
            )
        )
        assert res.success is False
        assert "reopen the period in Settings" in res.error
        assert "2025-12" in res.error

    def test_unrelated_error_left_alone(self, monkeypatch):
        async def handler(org, **kw):
            raise ValueError("Customer not found: X")

        monkeypatch.setattr(
            tr, "get_handler",
            lambda s: {"handler": handler, "read_only": False},
        )
        _patch_security(monkeypatch)
        auth = _auth()
        res = asyncio.run(
            tr.route_tool_call(
                ToolCall(tool_name="create_invoice", arguments={}),
                organization_id=auth.organization_id,
                user_id=auth.user_id,
                auth=auth,
            )
        )
        assert res.success is False
        assert "reopen the period" not in res.error
