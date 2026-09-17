"""
ERP AI Agent - Cache Tests
===========================
Work Stream A4: in-process caches for (a) Gemini tool definitions
(ai.tool_parameters, 5-minute TTL + explicit invalidation) and
(b) the agent constitution file (mtime-checked).
"""

from __future__ import annotations

import os
import time

import pytest


class TestToolDefinitionCache:

    def _install_db(self, monkeypatch, counter):
        import app.database as db
        import app.tool_router as tr

        async def fake_fetch_many(table, **kw):
            counter["fetch_many"] += 1
            return [{"slug": "search_customer", "id": "tool-1"}]

        class _FakeResponse:
            data = []

        class _FakeQuery:
            def select(self, *a, **kw):
                return self

            def in_(self, *a, **kw):
                return self

            def limit(self, *a, **kw):
                return self

            def execute(self):
                return _FakeResponse()

        class _FakeTable:
            def table(self, name):
                return _FakeQuery()

        class _FakeClient:
            def schema(self, name):
                return _FakeTable()

        monkeypatch.setattr(tr, "fetch_many", fake_fetch_many)
        monkeypatch.setattr(db, "get_service_client", lambda: _FakeClient())
        # Fresh cache state per test
        tr._invalidate_tool_definition_cache_internal()

    @pytest.mark.asyncio
    async def test_db_hit_once_across_n_calls(self, monkeypatch):
        counter = {"fetch_many": 0}
        self._install_db(monkeypatch, counter)
        from app.tool_router import get_gemini_tool_definitions

        for _ in range(5):
            tools = await get_gemini_tool_definitions()
            assert any(t["name"] == "search_customer" for t in tools)
        assert counter["fetch_many"] == 1, "tool definitions were re-fetched per call"

    @pytest.mark.asyncio
    async def test_invalidation_refreshes(self, monkeypatch):
        counter = {"fetch_many": 0}
        self._install_db(monkeypatch, counter)
        from app.tool_router import (
            get_gemini_tool_definitions,
            invalidate_tool_definition_cache,
        )

        await get_gemini_tool_definitions()
        await get_gemini_tool_definitions()
        assert counter["fetch_many"] == 1
        invalidate_tool_definition_cache()
        await get_gemini_tool_definitions()
        assert counter["fetch_many"] == 2, "invalidation must force a re-fetch"

    @pytest.mark.asyncio
    async def test_ttl_expiry_refetches(self, monkeypatch):
        counter = {"fetch_many": 0}
        self._install_db(monkeypatch, counter)
        from app.tool_router import (
            get_gemini_tool_definitions,
            _TOOL_DEFS_TTL_SECONDS,
        )
        import app.tool_router as tr

        await get_gemini_tool_definitions()
        assert counter["fetch_many"] == 1
        # Simulate TTL passage (cache holds (loaded_at, tools) tuple).
        loaded_at, cached_tools = tr._TOOL_DEFS_CACHE
        tr._TOOL_DEFS_CACHE = (
            loaded_at - _TOOL_DEFS_TTL_SECONDS - 1,
            cached_tools,
        )
        await get_gemini_tool_definitions()
        assert counter["fetch_many"] == 2, "expired cache must re-fetch"


class TestConstitutionCache:

    _mtime_counter = 1_000_000_000

    def _write(self, path, text):
        """Write text and stamp a DISTINCT, monotonically increasing mtime
        (filesystem mtime granularity is too coarse to rely on)."""
        path.write_text(text, encoding="utf-8")
        TestConstitutionCache._mtime_counter += 10
        t = TestConstitutionCache._mtime_counter
        os.utime(path, (t, t))

    def test_file_read_once_across_n_calls(self, tmp_path, monkeypatch):
        import app.prompts as prompts

        f = tmp_path / "constitution.md"
        self._write(f, "RULES v1")
        monkeypatch.setattr(prompts, "CONSTITUTION_PATH", f)
        prompts._reset_constitution_cache()

        reads = {"n": 0}
        real_read = type(f).read_text

        def counting_read_text(self, *a, **kw):
            reads["n"] += 1
            return real_read(self, *a, **kw)

        monkeypatch.setattr(type(f), "read_text", counting_read_text)

        for _ in range(5):
            assert prompts.load_constitution() == "RULES v1"
        assert reads["n"] == 1, "constitution was re-read per call"

    def test_mtime_change_rereads(self, tmp_path, monkeypatch):
        import app.prompts as prompts

        f = tmp_path / "constitution.md"
        self._write(f, "RULES v1")
        monkeypatch.setattr(prompts, "CONSTITUTION_PATH", f)
        prompts._reset_constitution_cache()

        assert prompts.load_constitution() == "RULES v1"
        self._write(f, "RULES v2")
        assert prompts.load_constitution() == "RULES v2", (
            "an edited constitution must be picked up on the next load")

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        import app.prompts as prompts

        monkeypatch.setattr(prompts, "CONSTITUTION_PATH", tmp_path / "nope.md")
        prompts._reset_constitution_cache()
        assert prompts.load_constitution() == ""
