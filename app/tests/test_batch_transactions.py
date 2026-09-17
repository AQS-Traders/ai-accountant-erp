"""
ERP AI Agent - Batch Transaction Tests (Work Stream B)
=======================================================
"record these 3 expenses: ..." / multi-clause requests must resolve in
ONE user request with ONE result per document:

* the planner detects enumeration patterns and produces a BATCH plan
  (one sub-intent per document),
* clarification is CONSOLIDATED - all pending sub-items in ONE round
  (numbered per document, answered via the existing multi-question UI +
  _explode_multi_answers),
* confirmation is ONE card covering all mutations,
* execution is sequential and independent: a failed sub-document never
  rolls back successful siblings, and the result card states exactly
  which succeeded and which failed.
"""

from __future__ import annotations

import pytest

from app.models.schemas import ToolCall, ToolResult
from app.planner import plan, split_batch_request
from app.agent import _build_batch_breakdown


class TestBatchSplitting:
    """Enumeration-pattern detection (conservative)."""

    def test_numbered_lines_split_into_segments(self):
        msg = "Record these 3 expenses:\n1) Office supplies Rs.5,000\n2) Fuel Rs.3,000\n3) Tea Rs.1,000"
        segments = split_batch_request(msg)
        assert len(segments) == 3
        assert "Office supplies Rs.5,000" in segments[0]
        assert "Tea Rs.1,000" in segments[2]

    def test_inline_also_split(self):
        msg = "Bought a laptop for Rs.150,000; also bought a mouse for Rs.2,000"
        segments = split_batch_request(msg)
        assert len(segments) == 2

    def test_and_also_split(self):
        msg = "Paid the office rent Rs.50,000 and also paid the electricity bill Rs.12,000"
        segments = split_batch_request(msg)
        assert len(segments) == 2

    def test_plain_message_never_split(self):
        assert split_batch_request("I bought a laptop from ABC Computers for Rs.150,000 on credit.") == []

    def test_narrative_never_split(self):
        """Prose without transactional segments must NOT be chopped."""
        msg = "First I checked the trial balance then I reviewed the profit and loss"
        # 'then' splits, but segments must ALL be transactional - this one isn't.
        assert split_batch_request(msg) == []

    def test_item_quantity_not_split(self):
        """'buy 3 chairs and 2 desks from X' stays with the item-intake
        protocol - it is ONE document with item lines, not N documents."""
        msg = "Buy 3 chairs and 2 desks from Ghulam Furnitures for Rs.25,000"
        assert split_batch_request(msg) == []


class TestBatchPlanning:
    def test_batch_plan_produces_one_subintent_per_document(self):
        msg = (
            "Record these 3 expenses:\n"
            "1) Office supplies Rs.5,000\n"
            "2) Fuel Rs.3,000\n"
            "3) Tea and biscuits Rs.1,000"
        )
        p = plan(msg)
        assert p.batch_items is not None
        assert len(p.batch_items) == 3
        assert [item["position"] for item in p.batch_items] == [1, 2, 3]
        assert all(item["intent"] == "record_expense" for item in p.batch_items)

    def test_batch_entities_are_prefixed_per_item(self):
        """Sibling values must never contaminate each other (no first-hit)."""
        msg = (
            "Record these 2 expenses:\n"
            "1) Office supplies Rs.5,000\n"
            "2) Fuel Rs.3,000"
        )
        p = plan(msg)
        assert p.batch_items is not None
        # Per-item amounts are distinguishable.
        assert p.extracted_entities.get("item_1_amount") == 5000.0
        assert p.extracted_entities.get("item_2_amount") == 3000.0

    def test_batch_confirmation_covers_all_mutations(self):
        """requires_confirmation = ANY sub-intent requires it."""
        msg = (
            "Record these 2 transactions:\n"
            "1) Bought a laptop from ABC Traders on credit for Rs.150,000\n"
            "2) Office supplies expense Rs.5,000"
        )
        p = plan(msg)
        assert p.batch_items is not None
        assert p.requires_confirmation is True  # item 1 is a credit purchase

    def test_batch_clarification_consolidated_in_one_round(self):
        """Gaps across ALL sub-items are gathered into ONE questionnaire,
        numbered per document so _explode_multi_answers maps the answers."""
        msg = (
            "Record these 2 expenses:\n"
            "1) mystery expense\n"
            "2) another expense"
        )
        p = plan(msg)
        assert p.batch_items is not None
        if p.requires_clarification:
            # ONE consolidated round: every question prefixed with its document.
            assert p.clarification_questions
            assert all(q.startswith("For document ") for q in p.clarification_questions)

    def test_single_transaction_plan_has_no_batch(self):
        p = plan("I spent Rs.5,000 on office supplies")
        assert p.batch_items is None


class TestBatchResultBreakdown:
    """The result card states exactly which documents succeeded/failed."""

    def _mutation(self, name, success, error=None):
        return (
            ToolCall(tool_name=name, arguments={}),
            ToolResult(
                tool_name=name,
                success=success,
                data={"id": "x"} if success else None,
                error=error,
            ),
        )

    def test_all_succeeded(self):
        calls = []
        results = []
        for name in ("create_expense", "create_expense", "create_expense"):
            tc, tr = self._mutation(name, True)
            calls.append(tc)
            results.append(tr)
        text = _build_batch_breakdown(
            [{"position": i} for i in range(1, 4)], calls, results
        )
        assert "3 succeeded, 0 failed" in text
        assert "Document 1 (create_expense): created." in text

    def test_partial_failure_keeps_siblings(self):
        calls, results = [], []
        tc1, tr1 = self._mutation("create_expense", True)
        tc2, tr2 = self._mutation("create_expense", False, error="Supplier required")
        tc3, tr3 = self._mutation("create_expense", True)
        calls += [tc1, tc2, tc3]
        results += [tr1, tr2, tr3]
        text = _build_batch_breakdown(
            [{"position": i} for i in range(1, 4)], calls, results
        )
        assert "Document 2 (create_expense): FAILED - Supplier required" in text
        assert "Document 1" in text and "created." in text
        assert "successful siblings were not rolled back" in text

    def test_fewer_mutations_than_planned_is_stated(self):
        tc, tr = self._mutation("create_expense", True)
        text = _build_batch_breakdown(
            [{"position": i} for i in range(1, 4)], [tc], [tr]
        )
        assert "2 not executed" in text

    def test_read_only_calls_never_counted_as_documents(self):
        calls = [
            ToolCall(tool_name="search_supplier", arguments={}),
            ToolCall(tool_name="create_expense", arguments={}),
        ]
        results = [
            ToolResult(tool_name="search_supplier", success=True, data=[]),
            ToolResult(tool_name="create_expense", success=True, data={"id": "e1"}),
        ]
        text = _build_batch_breakdown(
            [{"position": 1}, {"position": 2}], calls, results
        )
        assert "search_supplier" not in text
        assert "1 succeeded, 0 failed" in text
