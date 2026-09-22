"""Plan materialization — human-level references become canonical ids.

Production incident 2026-09-20 (session 6a48a432-d9d9-480c-8e47-e6799305bc6f)
===========================================================================

The approved plan said ``create_invoice {"customer_name": "ABC Furnitures",
"line_items": [{"product_name": "chairs"}]}``; the tool needs ``customer_id`` /
``items[].product_id``.  The customer was created by plan step 1 and the catalog
item existed (PRD-000002) — nothing ever turned the approved references into the
ids the tool requires, so the call died at binding time.

These tests pin EVERY invariant PR-B claims:

  1. resolution is read-only (no insert/update/RPC is reachable from it);
  2. only DECLARED aliases are consumed, and every other argument — and the
     approved snapshot — is passed through untouched;
  3. one exact match -> its canonical id; none -> ask; several -> ask with the
     candidates named (never a silent pick);
  4. tenant isolation: every lookup carries the caller's organization_id;
  5. nothing is created that the user did not approve, and no id is ever
     invented — a failed prerequisite yields a refusal, not a guess;
  6. the arguments handed to the tool BIND (the PR-A contract check passes);
  7. the original human-level plan is preserved for audit.

No live provider, no live database — the search services are stubbed.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest

from app.models.schemas import ToolCall
from app.plan_materialization import (
    ERROR,
    MULTIPLE,
    NONE,
    ONE,
    PREREQUISITE,
    bind_deferred,
    captured_ids,
    prepare_plan,
    resolve_reference,
    step_payload,
)
from app.tool_contract import validate_arguments

ORG = uuid.uuid4()
CUSTOMER_ID = "11111111-2222-3333-4444-555555555555"
PRODUCT_ID = "4c35aaaf-c35f-4f80-9697-750c2b255abc"


def _rows(*names: str) -> List[Dict[str, Any]]:
    return [{"id": str(uuid.uuid4()), "name": name} for name in names]


def _stub_search(monkeypatch, role: str, rows_by_query: Dict[str, Any], seen=None):
    """Patch the tenant-scoped search service for *role*."""
    import importlib

    module_name = {
        "customer": "customer_service",
        "supplier": "supplier_service",
        "product": "product_service",
        "service": "service_service",
    }[role]
    module = importlib.import_module(f"app.services.{module_name}")

    async def _search(organization_id, *, query: str, limit: int = 25):
        if seen is not None:
            seen.append((str(organization_id), query))
        return rows_by_query.get(query, [])

    monkeypatch.setattr(module, "search", _search)


def _contracts() -> Dict[str, Dict[str, Any]]:
    from app.tools import tool_contracts

    return tool_contracts()


INCIDENT_PLAN = [
    ToolCall(tool_name="create_customer", arguments={"name": "ABC Furnitures"}),
    ToolCall(
        tool_name="create_invoice",
        arguments={
            "due_date": "2026-10-20",
            "line_items": [
                {"quantity": 2, "unit_price": 16666.67, "product_name": "chairs"}
            ],
            "invoice_date": "2026-09-20",
            "tax_category": "Nill",
            "customer_name": "ABC Furnitures",
        },
    ),
]

# The same plan once the model has stopped inventing keys (post-PR-A flow):
# everything left is either canonical or a DECLARED reference.
INCIDENT_PLAN_EXECUTABLE = [
    ToolCall(tool_name="create_customer", arguments={"name": "ABC Furnitures"}),
    ToolCall(
        tool_name="create_invoice",
        arguments={
            "due_date": "2026-10-20",
            "line_items": [
                {"quantity": 2, "unit_price": 16666.67, "product_name": "chairs"}
            ],
            "invoice_date": "2026-09-20",
            "customer_name": "ABC Furnitures",
        },
    ),
]


@pytest.mark.asyncio
async def test_one_exact_match_resolves_to_its_id(monkeypatch):
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": [{"id": CUSTOMER_ID, "name": "abc furnitures"}]})

    resolution = await resolve_reference(
        organization_id=ORG, role="customer", name="ABC Furnitures"
    )

    assert resolution.status == ONE
    assert resolution.match_id == CUSTOMER_ID


@pytest.mark.asyncio
async def test_no_match_is_reported_as_none(monkeypatch):
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": []})

    resolution = await resolve_reference(
        organization_id=ORG, role="customer", name="ABC Furnitures"
    )

    assert resolution.status == NONE
    assert resolution.match_id is None


@pytest.mark.asyncio
async def test_several_matches_are_never_collapsed(monkeypatch):
    _stub_search(
        monkeypatch,
        "customer",
        {"ABC Furnitures": _rows("ABC Furnitures", "ABC Furnitures", "ACME")},
    )

    resolution = await resolve_reference(
        organization_id=ORG, role="customer", name="ABC Furnitures"
    )

    assert resolution.status == MULTIPLE
    assert resolution.match_id is None
    assert resolution.candidates.count("ABC Furnitures") == 2


@pytest.mark.asyncio
async def test_lookup_failure_is_an_error_not_a_missing_record(monkeypatch):
    import importlib

    module = importlib.import_module("app.services.customer_service")

    async def _boom(organization_id, *, query: str, limit: int = 25):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(module, "search", _boom)

    resolution = await resolve_reference(
        organization_id=ORG, role="customer", name="ABC Furnitures"
    )

    assert resolution.status == ERROR


@pytest.mark.asyncio
async def test_every_lookup_carries_the_caller_organization(monkeypatch):
    seen: List[Any] = []
    _stub_search(monkeypatch, "customer", {"ABC": [{"id": CUSTOMER_ID, "name": "ABC"}]}, seen=seen)

    await resolve_reference(organization_id=ORG, role="customer", name="ABC")

    assert seen == [(str(ORG), "ABC")]


# ---------------------------------------------------------------------------
# The incident's plan — what materialization does with it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_references_are_resolved_and_everything_else_is_untouched(monkeypatch):
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": []})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN_EXECUTABLE, contracts=_contracts()
    )

    assert result.blocked is False, result.blocks
    # the invoice waits for the customer that plan step 1 creates
    assert result.batches == [[0], [1]]
    invoice = result.calls[1]
    assert "customer_name" not in (invoice.arguments or {})   # declared alias consumed
    assert "customer_id" not in (invoice.arguments or {})     # never invented here
    assert "line_items" not in (invoice.arguments or {})      # container alias moved
    item = invoice.arguments["items"][0]
    assert item["product_id"] == PRODUCT_ID                   # exact catalog match
    assert item["description"] == "chairs"                    # the stated item name
    assert "product_name" not in item
    assert invoice.arguments["invoice_date"] == "2026-09-20"  # scalars untouched


@pytest.mark.asyncio
async def test_the_created_id_is_bound_and_the_call_then_binds(monkeypatch):
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": []})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN_EXECUTABLE, contracts=_contracts()
    )
    bound, problems = bind_deferred(
        call=result.calls[1],
        specs=result.deferred[1],
        provided_ids={"customer": CUSTOMER_ID},
        index=1,
        decisions=result.decisions,
    )

    assert problems == []
    assert bound.arguments["customer_id"] == CUSTOMER_ID
    assert validate_arguments(
        "create_invoice", bound.arguments, _contracts()["create_invoice"]
    ) == []
    decision = [d for d in result.decisions if d.status == PREREQUISITE][0]
    assert decision.source == "prerequisite:create_customer"
    assert decision.resolved_id == CUSTOMER_ID


@pytest.mark.asyncio
async def test_a_missing_prerequisite_record_yields_no_invented_id(monkeypatch):
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": []})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN_EXECUTABLE, contracts=_contracts()
    )
    bound, problems = bind_deferred(
        call=result.calls[1],
        specs=result.deferred[1],
        provided_ids={},  # the creation step failed / never ran
        index=1,
        decisions=result.decisions,
    )

    assert problems, "a missing prerequisite must be reported"
    assert "customer_id" not in (bound.arguments or {})
    assert "no id was invented" in problems[0]


@pytest.mark.asyncio
async def test_no_creation_step_and_no_match_asks_instead_of_creating(monkeypatch):
    """Nothing may be created because the model asked for it."""
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": []})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG,
        calls=[INCIDENT_PLAN_EXECUTABLE[1]],  # the invoice alone: no approved creation
        contracts=_contracts(),
    )

    assert result.blocked is True
    assert "no customer named 'ABC Furnitures' exists" in result.blocks[0]
    assert "create it first (confirm it)" in result.blocks[0]
    assert result.calls[0].arguments.get("customer_id") is None


@pytest.mark.asyncio
async def test_ambiguous_reference_stops_and_names_the_candidates(monkeypatch):
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": _rows("ABC Furnitures", "ABC Furnitures")})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN_EXECUTABLE, contracts=_contracts()
    )

    assert result.blocked is True
    assert "more than one customer matches" in result.blocks[0]
    assert result.calls[1].arguments.get("customer_id") is None  # never a silent pick


@pytest.mark.asyncio
async def test_an_unmatched_item_does_not_block_the_document(monkeypatch):
    """A line may legitimately be free text — the catalog need not stock it."""
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": []})
    _stub_search(monkeypatch, "product", {"chairs": []})
    _stub_search(monkeypatch, "service", {"chairs": []})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN_EXECUTABLE, contracts=_contracts()
    )

    assert result.blocked is False, result.blocks
    item = result.calls[1].arguments["items"][0]
    assert "product_id" not in item and "service_id" not in item
    assert item["description"] == "chairs"
    assert "product_name" not in item


# ---------------------------------------------------------------------------
# Runtime helpers + the audit record
# ---------------------------------------------------------------------------


def test_captured_ids_come_only_from_successful_creation_steps():
    calls = [
        ToolCall(tool_name="create_customer", arguments={"name": "ABC"}),
        ToolCall(tool_name="create_invoice", arguments={}),
    ]
    results = [
        {"success": False, "error": "boom"},          # failed creation
        {"success": True, "id": "not-a-customer-id"},  # not a creation tool
    ]

    assert captured_ids(calls=calls, results=results) == {}


def test_captured_ids_read_the_real_record_id():
    calls = [
        ToolCall(tool_name="create_customer", arguments={"name": "ABC"}),
        ToolCall(tool_name="create_product", arguments={"name": "chairs"}),
    ]
    results = [
        {"success": True, "id": CUSTOMER_ID, "name": "ABC"},
        {"success": True, "data": {"id": PRODUCT_ID, "name": "chairs"}},
    ]

    assert captured_ids(calls=calls, results=results) == {
        "customer": CUSTOMER_ID,
        "product": PRODUCT_ID,
    }


def test_step_payload_preserves_the_original_human_level_plan():
    from app.plan_materialization import Decision, PlanMaterialization

    original = [ToolCall(tool_name="create_invoice", arguments={"customer_name": "ABC Furnitures"})]
    materialization = PlanMaterialization(
        calls=[ToolCall(tool_name="create_invoice", arguments={"customer_id": CUSTOMER_ID})],
        decisions=[
            Decision(
                tool_name="create_invoice", index=0, parameter="customer_id",
                role="customer", requested="ABC Furnitures", status=ONE,
                resolved_id=CUSTOMER_ID,
                original_arguments={"customer_name": "ABC Furnitures"},
            )
        ],
        blocks=[],
    )

    payload = step_payload(materialization, original=original)

    assert payload["original_plan"][0]["arguments"]["customer_name"] == "ABC Furnitures"
    assert payload["canonical_plan"][0]["arguments"]["customer_id"] == CUSTOMER_ID
    assert payload["decisions"][0]["requested"] == "ABC Furnitures"
    assert payload["decisions"][0]["original_arguments"]["customer_name"] == "ABC Furnitures"
    assert payload["blocks"] == []


@pytest.mark.asyncio
async def test_resolution_never_mutates_the_database(monkeypatch):
    """Resolution is a READ: not one write path may be reachable from it."""
    import app.database as database

    def _forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("resolution attempted a WRITE")

    monkeypatch.setattr(database, "insert_one", _forbidden)
    monkeypatch.setattr(database, "update_one", _forbidden)
    monkeypatch.setattr(database, "call_rpc", _forbidden)
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": [{"id": CUSTOMER_ID, "name": "ABC Furnitures"}]})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN_EXECUTABLE, contracts=_contracts()
    )

    # the existing customer resolves, the invoice no longer needs a prerequisite
    assert result.blocked is False, result.blocks
    assert result.calls[1].arguments["customer_id"] == CUSTOMER_ID
    assert result.batches == [[0, 1]]


@pytest.mark.asyncio
async def test_the_approved_plan_object_is_never_mutated(monkeypatch):
    """Materialization COPIES: the human-level plan stays intact for the audit."""
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": [{"id": CUSTOMER_ID, "name": "ABC Furnitures"}]})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})
    plan = [
        ToolCall(
            tool_name="create_invoice",
            arguments={
                "items": [
                    {"quantity": 2, "unit_price": 16666.67, "product_name": "chairs"}
                ],
                "customer_name": "ABC Furnitures",
            },
        )
    ]

    await prepare_plan(organization_id=ORG, calls=plan, contracts=_contracts())

    assert plan[0].arguments["customer_name"] == "ABC Furnitures"
    assert "customer_id" not in plan[0].arguments
    assert plan[0].arguments["items"][0]["product_name"] == "chairs"
    assert "product_id" not in plan[0].arguments["items"][0]


@pytest.mark.asyncio
async def test_the_verbatim_incident_arguments_are_still_blocked(monkeypatch):
    """Defence in depth: PR-A's contract gate still refuses `tax_category`."""
    _stub_search(monkeypatch, "customer", {"ABC Furnitures": [{"id": CUSTOMER_ID, "name": "ABC Furnitures"}]})
    _stub_search(monkeypatch, "product", {"chairs": [{"id": PRODUCT_ID, "name": "chairs"}]})

    result = await prepare_plan(
        organization_id=ORG, calls=INCIDENT_PLAN, contracts=_contracts()
    )

    assert result.blocked is True
    assert any("'tax_category'" in block for block in result.blocks)
