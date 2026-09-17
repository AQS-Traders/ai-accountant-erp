"""
ERP AI Agent - Context Loading Parallelism Tests
=================================================
Work Stream A2: build_context() must fetch independent sources
concurrently and one failing source must not kill the rest
(per-query error isolation).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.context_manager import build_context

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _install(monkeypatch, *, org=None, fy=None, period=None, ctx=None,
             fail_customer=False, delay=0.05):
    """Patch every context source with a slow-but-fake async fetch."""
    from app.context_manager import (
        account_repository,
        customer_repository,
        organization_repository,
        project_repository,
        supplier_repository,
    )
    from app.services import preference_service
    import app.context_manager as cm

    async def _slow_ok(value):
        await asyncio.sleep(delay)
        return value

    async def _boom():
        await asyncio.sleep(delay)
        raise RuntimeError("customer source down")

    async def get_org(organization_id=None, **kw):
        return await _slow_ok(org or {"id": str(ORG), "name": "Test Org"})

    async def get_fy(organization_id, **kw):
        return await _slow_ok(fy or {"id": "fy1"})

    async def get_period(organization_id, **kw):
        return await _slow_ok(period or {"id": "p1"})

    async def get_sources(intent, **kw):
        return await _slow_ok(
            ctx
            if ctx is not None
            else {"rule": {"required_sources": ["chart_of_accounts"]}}
        )

    async def search_customers(organization_id, **kw):
        if fail_customer:
            return await _boom()
        return await _slow_ok([{"id": "c1", "name": "ABC"}])

    async def search_suppliers(organization_id, **kw):
        return await _slow_ok([])

    async def get_coa(organization_id, **kw):
        return await _slow_ok([{"id": "a1", "name": "Cash"}])

    async def search_projects(organization_id, **kw):
        return await _slow_ok([])

    monkeypatch.setattr(organization_repository, "get_organization", get_org)
    monkeypatch.setattr(organization_repository, "get_current_financial_year", get_fy)
    monkeypatch.setattr(organization_repository, "get_open_accounting_period", get_period)
    # context_manager does `from app.database import get_context_sources_for_intent`
    # - patch the NAME it actually resolves.
    monkeypatch.setattr(cm, "get_context_sources_for_intent", get_sources)
    # context_manager resolves these as module attributes on the repos
    monkeypatch.setattr(customer_repository, "search_customers", search_customers)
    monkeypatch.setattr(supplier_repository, "search_suppliers", search_suppliers)
    monkeypatch.setattr(account_repository, "get_chart_of_accounts", get_coa)
    monkeypatch.setattr(project_repository, "search_projects", search_projects)
    # Work Stream F: the org-preferences source runs in the same gather -
    # fake it so the test never touches the real database.
    async def get_prefs(organization_id, **kw):
        return await _slow_ok({"payment_method": "CASH"})

    monkeypatch.setattr(preference_service, "get_all_preferences", get_prefs)


class TestParallelContextLoading:

    @pytest.mark.asyncio
    async def test_independent_sources_load_concurrently(self, monkeypatch):
        """Four 0.05s fetches must overlap: wall-clock far below the
        serial sum of all seven sources."""
        _install(monkeypatch)
        start = time.monotonic()
        context = await build_context(
            organization_id=ORG, user_id=USER, intent="record_cash_sale"
        )
        elapsed = time.monotonic() - start
        assert context.organization.get("name") == "Test Org"
        assert len(context.relevant_accounts) == 1
        # Serial sum would be >= 0.35s (7 x 0.05); concurrent ~0.05-0.1s.
        assert elapsed < 0.3, f"context sources were serialized ({elapsed:.2f}s)"

    @pytest.mark.asyncio
    async def test_one_failed_source_does_not_kill_the_rest(self, monkeypatch):
        """Per-query error isolation: a raising source degrades to an
        empty result instead of failing the whole context build."""
        _install(monkeypatch, fail_customer=True)
        context = await build_context(
            organization_id=ORG, user_id=USER, intent="record_cash_sale"
        )
        assert context.relevant_customers == []      # failed source -> []
        assert context.organization.get("name") == "Test Org"
        assert len(context.relevant_accounts) == 1   # healthy sources intact
