"""Security hardening — mutation authorization, membership status, caches.

Pins the fixes for three findings:

* a MUTATION used to execute with ``auth=None`` (the AI worker passed it),
  which skipped BOTH the permission check and parameter validation;
* ``_resolve_membership`` used to fall back to an UNFILTERED lookup when no
  ACTIVE membership was found, authenticating SUSPENDED/REMOVED members;
* the permissions cache lived for the whole process, so permission changes
  never took effect until a restart (and the worker never restarts).
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi import HTTPException

import app.auth as auth
import app.permissions as perms
import app.tool_router as tr
from app.models.schemas import ToolCall

PERM_ROW = {"capability": "sales", "allowed_tools": [], "denied_tools": []}


class TestMembershipStatusIsEnforced:
    @pytest.mark.asyncio
    async def test_inactive_membership_is_not_authenticated(self, monkeypatch):
        """No ACTIVE membership => not a member. No unfiltered fallback."""
        queries = []

        async def _fetch_many(table, **kwargs):
            queries.append(kwargs.get("filters", {}))
            return []

        monkeypatch.setattr(auth, "fetch_many", _fetch_many)
        assert await auth._resolve_membership(uuid.uuid4()) is None
        # Exactly ONE query, and it must be status-filtered. A second,
        # unfiltered query is precisely the vulnerability that was removed.
        assert len(queries) == 1
        assert queries[0].get("status") == "ACTIVE"

    @pytest.mark.asyncio
    async def test_build_auth_context_denies_removed_user(self, monkeypatch):
        async def _none(user_id):
            return None

        monkeypatch.setattr(auth, "_resolve_membership", _none)
        with pytest.raises(HTTPException) as exc:
            await auth.build_auth_context_for_user(uuid.uuid4())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_build_auth_context_reads_org_and_role_from_db(self, monkeypatch):
        """The organization must come from live membership, never a payload."""
        org = uuid.uuid4()

        async def _member(user_id):
            return {"organization_id": str(org), "role_id": "role-1"}

        async def _role(role_id):
            return {"code": "ACCOUNTANT", "permissions": ["accounting:full"]}

        monkeypatch.setattr(auth, "_resolve_membership", _member)
        monkeypatch.setattr(auth, "_resolve_role", _role)

        ctx = await auth.build_auth_context_for_user(uuid.uuid4())
        assert ctx.organization_id == org
        assert ctx.role_code == "ACCOUNTANT"
        assert ctx.role_permissions == ["accounting:full"]


class TestMutationRequiresAuth:
    @pytest.mark.asyncio
    async def test_mutation_without_auth_is_refused(self, monkeypatch):
        async def handler(org, **kw):  # pragma: no cover - must never run
            raise AssertionError("mutation handler must not be called")

        monkeypatch.setattr(
            tr, "get_handler", lambda s: {"handler": handler, "read_only": False}
        )
        res = await tr.route_tool_call(
            ToolCall(tool_name="create_invoice", arguments={}),
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            auth=None,
        )
        assert res.success is False
        assert "Authorization context missing" in res.error

    @pytest.mark.asyncio
    async def test_read_only_without_auth_still_runs(self, monkeypatch):
        """Read-only tools keep working without a context (unchanged)."""
        called = {}

        async def handler(org, **kw):
            called["ran"] = True
            return {"ok": True}

        monkeypatch.setattr(
            tr, "get_handler", lambda s: {"handler": handler, "read_only": True}
        )
        await tr.route_tool_call(
            ToolCall(tool_name="get_trial_balance", arguments={}),
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            auth=None,
        )
        assert called.get("ran") is True


class TestPermissionsCache:
    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, monkeypatch):
        perms.invalidate_cache()
        calls = {"n": 0}

        async def _fetch_many(table, **kwargs):
            calls["n"] += 1
            return [PERM_ROW]

        monkeypatch.setattr(perms, "fetch_many", _fetch_many)

        await perms._load_permissions()
        await perms._load_permissions()
        assert calls["n"] == 1, "second call inside the TTL must be cached"

        # Age the entry past the TTL: the next call must re-read.
        perms._permissions_cache_at = (
            time.monotonic() - perms._PERMISSIONS_CACHE_TTL_SECONDS - 1
        )
        await perms._load_permissions()
        assert calls["n"] == 2, "expired entry must be refreshed"

    @pytest.mark.asyncio
    async def test_transient_failure_does_not_poison_the_cache(self, monkeypatch):
        perms.invalidate_cache()

        async def _good(table, **kwargs):
            return [PERM_ROW]

        monkeypatch.setattr(perms, "fetch_many", _good)
        first = await perms._load_permissions()
        assert first

        # Source now fails AND the cached entry is stale.
        async def _bad(table, **kwargs):
            raise RuntimeError("permissions source unavailable")

        monkeypatch.setattr(perms, "fetch_many", _bad)
        perms._permissions_cache_at = (
            time.monotonic() - perms._PERMISSIONS_CACHE_TTL_SECONDS - 1
        )

        again = await perms._load_permissions()
        assert again == first, "last known good snapshot must be served"

    @pytest.mark.asyncio
    async def test_first_load_failure_still_raises(self, monkeypatch):
        """With nothing cached, a failure must surface — never silently allow."""
        perms.invalidate_cache()

        async def _bad(table, **kwargs):
            raise RuntimeError("permissions source unavailable")

        monkeypatch.setattr(perms, "fetch_many", _bad)
        with pytest.raises(RuntimeError):
            await perms._load_permissions()

    def test_invalidate_clears_timestamp(self):
        perms._permissions_cache = [PERM_ROW]
        perms._permissions_cache_at = time.monotonic()
        perms.invalidate_cache()
        assert perms._permissions_cache is None
        assert perms._permissions_cache_at == 0.0