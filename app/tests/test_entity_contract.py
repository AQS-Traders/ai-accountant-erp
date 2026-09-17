"""
ERP AI Agent — Canonical affected-entity / accounting-impact contract tests.

Regression guard for the AIActionCard crash
(``Cannot read properties of undefined (reading 'replace')``): the backend
contract builders MUST always emit a non-empty string ``type`` for every
affected entity, regardless of how malformed the tool result is.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.entity_contract import (
    build_accounting_impact,
    build_affected_entities,
    build_affected_entity,
)
from app.models.schemas import AffectedEntity, AgentResponse, ExecutionStatus, ToolResult


def _tr(tool_name: str, data, success: bool = True) -> ToolResult:
    return ToolResult(tool_name=tool_name, success=success, data=data)


# ---------------------------------------------------------------------------
# affected_entities — malformed / null / missing inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        "string-result",
        12345,
        {"id": None},
        {"id": ""},
        {"id": "   "},
        {"name": "only-a-name"},
        {"entry": None},
        {"entry": {}},
    ],
)
def test_no_entity_emitted_when_no_identifiable_record(data):
    assert build_affected_entity("create_supplier", data) is None


@pytest.mark.parametrize("data", [{"id": 123}, {"id": None, "entry": {"id": "abc"}}])
def test_type_is_always_non_empty_string(data):
    entity = build_affected_entity("create_supplier", data)
    if entity is not None:
        assert isinstance(entity["type"], str)
        assert entity["type"] != ""


def test_malformed_tool_results_never_break_or_violate_contract():
    results = [
        _tr("create_supplier", None),
        _tr("create_customer", []),
        _tr("create_invoice", {"id": "inv-1", "invoice_number": "INV-7", "total": 999}),
        _tr("weird_unknown_tool", {"id": "x-1"}),
        _tr("create_supplier", {"id": "s-1", "name": None, "total": "12.5"}),
    ]
    entities = build_affected_entities(results)
    assert len(entities) == 3
    for entity in entities:
        # Pydantic contract validation — type must be a non-empty string.
        assert AffectedEntity(**entity).type
        assert isinstance(entity["type"], str) and entity["type"].strip()


def test_journal_entry_nesting_maps_to_canonical_type():
    data = {
        "entry": {"id": "je-1", "journal_number": "JE-9"},
        "lines": [
            {"account_id": "a1", "debit": 5000, "credit": 0},
            {"account_id": "a2", "debit": 0, "credit": 5000},
        ],
        "total_debit": 5000,
        "total_credit": 5000,
    }
    entity = build_affected_entity("prepare_journal", data)
    assert entity["type"] == "journal_entry"
    assert entity["action"] == "prepared"
    assert entity["id"] == "je-1"
    assert entity["number"] == "JE-9"
    assert entity["total"] == 5000.0


def test_fallback_mapping_for_unknown_tools():
    entity = build_affected_entity("frobnicate_thing", {"id": "t-1"})
    assert entity["type"] == "thing"
    assert entity["action"] == "updated"


def test_failed_and_hidden_results_are_skipped():
    results = [
        _tr("create_supplier", {"id": "s-1"}, success=False),
        _tr("search_supplier", [{"id": "a"}, {"id": "b"}]),
    ]
    assert build_affected_entities(results) == []


def test_agent_response_validates_canonical_entities():
    response = AgentResponse(
        status=ExecutionStatus.COMPLETED,
        affected_entities=[{"type": "bill", "action": "created", "id": "b-1", "total": 25000}],
        accounting_impact=[{"account": "Rent Expense", "debit": 25000}],
    )
    assert response.affected_entities[0].type == "bill"
    assert response.accounting_impact[0].account == "Rent Expense"


def test_agent_response_rejects_empty_entity_type():
    with pytest.raises(ValidationError):
        AgentResponse(
            status=ExecutionStatus.COMPLETED,
            affected_entities=[{"type": "", "id": "x"}],
        )


# ---------------------------------------------------------------------------
# accounting_impact — malformed lines / resolver failures
# ---------------------------------------------------------------------------


def test_accounting_impact_contract_with_resolver_failures():
    journal = _tr(
        "prepare_journal",
        {
            "entry": {"id": "je-1"},
            "lines": [
                {"account_id": "a1", "debit": 5000, "credit": 0, "description": "Rent"},
                {"account_id": "a2", "debit": 0, "credit": 5000},
                {"account_id": None, "debit": 0, "credit": 1},
                {"account_id": "a3"},  # no amounts -> skipped
                "not-a-dict",
            ],
            "total_debit": 5000,
        },
    )

    async def bad_resolver(_account_id):
        raise RuntimeError("db down")

    async def run():
        return await build_accounting_impact([journal], resolve_account_name=bad_resolver)

    lines = asyncio.run(run())
    assert len(lines) == 3
    for line in lines:
        assert isinstance(line["account"], str) and line["account"].strip()
        assert line.get("debit") is not None or line.get("credit") is not None
        assert line.get("debit") is None or line.get("credit") is None  # never both


def test_accounting_impact_uses_resolved_names_and_deduplicates():
    journal = _tr(
        "prepare_journal",
        {
            "lines": [
                {"account_id": "a1", "debit": 5000, "credit": 0},
                {"account_id": "a1", "debit": 5000, "credit": 0},  # duplicate
            ],
        },
    )
    journal_again = _tr(
        "post_journal",
        {
            "lines": [
                {"account_id": "a1", "debit": 5000, "credit": 0},  # duplicate across tools
            ],
        },
    )

    async def resolver(account_id):
        return "Rent Expense" if account_id == "a1" else None

    async def run():
        return await build_accounting_impact(
            [journal, journal_again], resolve_account_name=resolver
        )

    lines = asyncio.run(run())
    assert lines == [{"account": "Rent Expense", "debit": 5000.0}]
