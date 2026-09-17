"""
ERP AI Agent - Parallel Tool Execution Tests
=============================================
Work Stream A1: independent READ-ONLY planned tool calls must execute
CONCURRENTLY while mutation calls stay strictly sequential and preserve
their original relative order. A read-only call planned AFTER a mutation
must observe that mutation (never reordered in front of it).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.models.schemas import ToolCall
from app.tool_execution import execute_planned_tool_calls, is_read_only_tool


class TestReadOnlyParallelism:

    @pytest.mark.asyncio
    async def test_three_read_only_calls_run_concurrently(self):
        """Wall-clock for 3 x 0.1s read-only calls must be well below the
        0.3s serial sum - they must overlap."""
        timeline: list = []

        async def executor(tool_name: str, args: dict) -> dict:
            timeline.append(("start", tool_name, time.monotonic()))
            await asyncio.sleep(0.1)
            timeline.append(("end", tool_name, time.monotonic()))
            return {"success": True, "tool": tool_name}

        calls = [
            ToolCall(tool_name="search_customer", arguments={"query": "a"}),
            ToolCall(tool_name="search_supplier", arguments={"query": "b"}),
            ToolCall(tool_name="search_account", arguments={"query": "c"}),
        ]
        start = time.monotonic()
        results = await execute_planned_tool_calls(calls, executor)
        elapsed = time.monotonic() - start

        assert all(r["success"] for r in results)
        # Serial would be >= 0.3s; concurrent should be ~0.1s.
        assert elapsed < 0.25, f"reads did not run concurrently ({elapsed:.2f}s)"
        # All three reads must have STARTED before the first one finished.
        starts = [t for (kind, _, t) in timeline if kind == "start"]
        ends = [t for (kind, _, t) in timeline if kind == "end"]
        assert max(starts) < min(ends), "reads were serialized"

    @pytest.mark.asyncio
    async def test_mutation_ordering_preserved(self):
        """Two mutations must run strictly one-after-the-other, in the
        planned order - never interleaved or reordered."""
        timeline: list = []

        async def executor(tool_name: str, args: dict) -> dict:
            timeline.append(("start", tool_name, time.monotonic()))
            await asyncio.sleep(0.03)
            timeline.append(("end", tool_name, time.monotonic()))
            return {"success": True, "tool": tool_name}

        calls = [
            ToolCall(tool_name="create_supplier", arguments={}),
            ToolCall(tool_name="create_customer", arguments={}),
        ]
        await execute_planned_tool_calls(calls, executor)

        events = [(kind, name) for kind, name, _ in timeline]
        assert events == [
            ("start", "create_supplier"),
            ("end", "create_supplier"),
            ("start", "create_customer"),
            ("end", "create_customer"),
        ]

    @pytest.mark.asyncio
    async def test_reads_after_mutation_wait_for_it(self):
        """A read-only call planned AFTER a mutation must not start before
        the mutation completed (it may depend on the new state)."""
        timeline: list = []

        async def executor(tool_name: str, args: dict) -> dict:
            timeline.append(("start", tool_name, time.monotonic()))
            await asyncio.sleep(0.05)
            timeline.append(("end", tool_name, time.monotonic()))
            return {"success": True, "tool": tool_name}

        calls = [
            ToolCall(tool_name="create_supplier", arguments={}),
            ToolCall(tool_name="search_supplier", arguments={}),
            ToolCall(tool_name="search_customer", arguments={}),
        ]
        start = time.monotonic()
        results = await execute_planned_tool_calls(calls, executor)
        elapsed = time.monotonic() - start

        assert all(r["success"] for r in results)
        events = [(kind, name, t) for kind, name, t in timeline]
        mut_end = next(t for kind, name, t in events
                       if kind == "end" and name == "create_supplier")
        read_starts = [t for kind, name, t in events
                       if kind == "start" and name.startswith("search")]
        read_ends = [t for kind, name, t in events
                     if kind == "end" and name.startswith("search")]
        assert all(rs >= mut_end for rs in read_starts), (
            "read-only calls started before the mutation finished")
        # DETERMINISTIC overlap proof: the two reads must overlap in time
        # (a later read starts before an earlier read ends) - no flaky
        # wall-clock bounds.
        assert max(read_starts) < min(read_ends), (
            "post-mutation reads were serialized")
        # NOTE: a wall-clock bound (`elapsed < 0.25`) used to sit here.  It was
        # NOT the invariant — it measured how busy the machine was.  Running the
        # full suite alongside anything else pushed it to 0.56s and produced a
        # false failure, while the two deterministic assertions above (reads
        # start only after the mutation ended, and the reads genuinely overlap)
        # passed.  Serialization is already impossible to hide from those, so the
        # timing bound only added flakiness.

    @pytest.mark.asyncio
    async def test_results_align_with_input_order(self):
        async def executor(tool_name: str, args: dict) -> dict:
            await asyncio.sleep(0.01)
            return {"success": True, "tool": tool_name}

        calls = [
            ToolCall(tool_name="search_account", arguments={}),
            ToolCall(tool_name="create_supplier", arguments={}),
            ToolCall(tool_name="search_customer", arguments={}),
        ]
        results = await execute_planned_tool_calls(calls, executor)
        assert [r["tool"] for r in results] == [
            "search_account", "create_supplier", "search_customer"
        ]

    @pytest.mark.asyncio
    async def test_executor_exception_becomes_error_result(self):
        async def executor(tool_name: str, args: dict) -> dict:
            if tool_name == "search_customer":
                raise RuntimeError("boom")
            return {"success": True}

        calls = [
            ToolCall(tool_name="search_customer", arguments={}),
            ToolCall(tool_name="search_supplier", arguments={}),
        ]
        results = await execute_planned_tool_calls(calls, executor)
        assert results[0] == {"success": False, "error": "boom"}
        assert results[1]["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_tool_treated_as_mutation(self):
        """Defence in depth: a tool missing from the registry is NEVER
        assumed read-only - it runs sequentially."""
        assert is_read_only_tool("not_a_registered_tool") is False

        timeline: list = []

        async def executor(tool_name: str, args: dict) -> dict:
            timeline.append(("start", tool_name, time.monotonic()))
            await asyncio.sleep(0.02)
            timeline.append(("end", tool_name, time.monotonic()))
            return {"success": True}

        calls = [
            ToolCall(tool_name="mystery_tool_a", arguments={}),
            ToolCall(tool_name="mystery_tool_b", arguments={}),
        ]
        await execute_planned_tool_calls(calls, executor)

        # DETERMINISTIC proof of sequencing: each call must fully finish
        # before the next starts.  Concurrent execution would interleave as
        # start_a, start_b, ... — and the previous form of this assertion
        # (`timeline[1][1] >= timeline[0][1] + 0.015`, a 15ms tolerance on a
        # 20ms sleep) failed spuriously whenever the machine was loaded,
        # because Windows' timer granularity is itself ~15.6ms.
        events = [(kind, name) for kind, name, _ in timeline]
        assert events == [
            ("start", "mystery_tool_a"),
            ("end", "mystery_tool_a"),
            ("start", "mystery_tool_b"),
            ("end", "mystery_tool_b"),
        ], f"Unknown tools must run strictly sequentially, got {events}"
