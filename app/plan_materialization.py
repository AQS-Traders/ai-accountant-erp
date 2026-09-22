"""Plan materialization — human-level references become canonical ids.

Production incident 2026-09-20 (session 6a48a432-d9d9-480c-8e47-e6799305bc6f)
===========================================================================

The approved plan was expressed in the user's own terms::

    create_customer  {"name": "ABC Furnitures"}
    create_invoice   {"customer_name": "ABC Furnitures",
                      "line_items": [{"product_name": "chairs", ...}], ...}

``create_invoice`` needs ``customer_id`` / ``items[].product_id``, so the call
died at binding time.  The customer DID get created (it is plan step 1) and the
catalog item DID exist (PRD-000002) — nothing ever turned the approved
human-level reference into the canonical id the tool requires.

This module performs that transformation, and nothing else.

INVARIANTS (each one is pinned by app/tests/test_plan_materialization.py)
------------------------------------------------------------------------
* READ-ONLY: resolution never writes.  It only calls the tenant-scoped,
  permission-checked ``search_*`` services that already exist.
* DECLARED references only: a parameter is touched only when the tool declares
  it here, and only the aliases listed here are consumed.  No arbitrary
  renaming; every other argument is passed through untouched.
* 1 exact match  -> substitute its canonical id (case/space-insensitive
                   equality — the same rule as the party and catalog gates).
* 0 matches      -> for a REQUIRED reference: STOP and ask (never invent an id,
                   never create anything here).  For an optional line link: leave
                   the line unlinked (a legitimate free-text line) and record it.
* >1 matches     -> ambiguity is never resolved silently: a REQUIRED reference
                   stops with the candidates named; an optional link is left
                   unlinked and recorded.
* Tenant isolation: every lookup carries ``organization_id``.
* Confirmation is never bypassed: a prerequisite is used only when the APPROVED
  plan already creates it (the user approved that step).  Nothing is created
  because the model asked; a missing prerequisite produces a question.
* Idempotent across retry/resume: resolution is deterministic, and a repeated
  prerequisite matches the existing per-tool idempotency claim, whose replay
  returns the SAME record id.
* Audit: the original human-level arguments are preserved in every decision
  record, and the approved snapshot itself is never modified.
* After materialization the call must BIND against the tool contract
  (app/tool_contract.py); anything still un-bindable blocks with the reason.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from app.models.schemas import ToolCall

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Declared references — the ONLY arguments this module may consume.
# ---------------------------------------------------------------------------

# Match outcomes.
ONE = "one"
NONE = "none"
MULTIPLE = "multiple"


@dataclass(frozen=True)
class ReferenceSpec:
    """A tool parameter that needs a canonical id, plus its declared aliases."""

    parameter: str
    role: str
    aliases: Tuple[str, ...]
    required: bool = True


# Tools whose declared contract needs canonical ids, and the human-level names
# the approved plan may carry instead (both spellings come from the vocabulary
# the reasoning prompt shows: entity fields and party questions).
DECLARED_REFERENCES: Dict[str, Dict[str, ReferenceSpec]] = {
    "create_invoice": {
        "customer_id": ReferenceSpec(
            "customer_id", "customer", ("customer_name", "party_name")
        ),
    },
    "create_purchase_bill": {
        "supplier_id": ReferenceSpec(
            "supplier_id", "supplier", ("supplier_name", "party_name")
        ),
    },
    "create_expense": {
        "supplier_id": ReferenceSpec(
            "supplier_id", "supplier", ("supplier_name", "payee_name")
        ),
    },
    "record_customer_receipt": {
        "customer_id": ReferenceSpec(
            "customer_id", "customer", ("customer_name", "party_name")
        ),
    },
    "record_supplier_payment": {
        "supplier_id": ReferenceSpec(
            "supplier_id", "supplier", ("supplier_name", "party_name")
        ),
    },
}

# Line-item references: which key names an item on a document line.  The link is
# OPTIONAL (a line may legitimately stay free text), so an unmatched or
# ambiguous item never blocks the document.
LINE_REFERENCE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "create_invoice": ("product_name", "item_name"),
    "create_purchase_bill": ("product_name", "item_name"),
}

# The container key itself, when the approved plan uses the model's vocabulary
# (`line_items`) instead of the tool's (`items`).  Declared, never guessed.
DOCUMENT_LINE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "create_invoice": ("line_items",),
    "create_purchase_bill": ("line_items",),
}

# Which tool CREATES each role.  Used ONLY to recognise a creation step the user
# already approved — never to add a step of our own.
ROLE_CREATION_TOOLS: Dict[str, str] = {
    "customer": "create_customer",
    "supplier": "create_supplier",
    "product": "create_product",
    "service": "create_service",
}
CREATION_TOOL_ROLES: Dict[str, str] = {
    tool: role for role, tool in ROLE_CREATION_TOOLS.items()
}

# Lookup failure (as opposed to "no match"): fail closed and say so honestly.
ERROR = "error"
PREREQUISITE = "prerequisite"
ALIAS = "alias"


@dataclass
class Resolution:
    role: str
    requested: str
    status: str
    match_id: Optional[str] = None
    candidates: List[str] = field(default_factory=list)


@dataclass
class Decision:
    """One materialization decision — durable, with the original arguments."""

    tool_name: str
    index: int
    parameter: str
    role: str
    requested: str
    status: str
    resolved_id: Optional[str] = None
    source: str = "books"
    candidates: List[str] = field(default_factory=list)
    optional: bool = False
    original_arguments: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "step": self.index + 1,
            "tool": self.tool_name,
            "parameter": self.parameter,
            "role": self.role,
            "requested": self.requested,
            "status": self.status,
            "source": self.source,
            "resolved_id": self.resolved_id,
            "candidates": self.candidates,
            "optional": self.optional,
            "original_arguments": self.original_arguments,
        }


@dataclass
class PlanMaterialization:
    calls: List[ToolCall]
    decisions: List[Decision]
    blocks: List[str]
    deferred: Dict[int, List[ReferenceSpec]] = field(default_factory=dict)
    batches: List[List[int]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocks)


async def _search(role: str, organization_id: uuid.UUID, query: str, limit: int):
    """Tenant-scoped lookup for *role* — reuses the existing search services."""
    if role == "customer":
        from app.services import customer_service

        return await customer_service.search(organization_id, query=query, limit=limit)
    if role == "supplier":
        from app.services import supplier_service

        return await supplier_service.search(organization_id, query=query, limit=limit)
    if role == "product":
        from app.services import product_service

        return await product_service.search(organization_id, query=query, limit=limit)
    if role == "service":
        from app.services import service_service

        return await service_service.search(organization_id, query=query, limit=limit)
    return []


def _exact_matches(rows: Sequence[Dict[str, Any]], name: Any) -> List[Dict[str, Any]]:
    """EXACT matches only — the party/catalog gate's equality rule."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return []
    return [
        row
        for row in rows or ()
        if str(row.get("name") or "").strip().lower() == wanted
    ]


async def resolve_reference(
    *, organization_id: uuid.UUID, role: str, name: Any, limit: int = 5
) -> Resolution:
    """Resolve one human-level reference against the live tenant books."""
    text = str(name or "").strip()
    if not text:
        return Resolution(role=role, requested=text, status=NONE)
    try:
        rows = await _search(role, organization_id, text, limit=limit)
    except Exception as exc:  # noqa: BLE001 — a failed lookup never invents an id
        log.warning(
            "plan_materialization.lookup_failed", role=role, error=str(exc)[:200]
        )
        return Resolution(role=role, requested=text, status=ERROR)
    matches = _exact_matches(rows, text)
    if len(matches) == 1:
        return Resolution(
            role=role,
            requested=text,
            status=ONE,
            match_id=str(matches[0].get("id")),
        )
    if len(matches) > 1:
        return Resolution(
            role=role,
            requested=text,
            status=MULTIPLE,
            candidates=[str(m.get("name") or "") for m in matches],
        )
    return Resolution(role=role, requested=text, status=NONE)


def _block_message(resolution: Resolution, *, tool_name: str, name: str) -> str:
    """The question/stop for a REQUIRED reference that cannot be resolved."""
    if resolution.status == MULTIPLE:
        options = ", ".join(repr(c) for c in resolution.candidates[:5])
        return (
            f"{tool_name}: more than one {resolution.role} matches '{name}' ({options}) — "
            "which one should be used? Nothing has been recorded."
        )
    if resolution.status == ERROR:
        return (
            f"{tool_name}: the {resolution.role} records could not be read, so '{name}' could "
            "not be checked. Nothing has been recorded — please try again."
        )
    return (
        f"{tool_name}: no {resolution.role} named '{name}' exists in your books, and the "
        "approved plan does not create one — create it first (confirm it) or name an "
        "existing record."
    )


async def _materialize_items(
    *,
    organization_id: uuid.UUID,
    index: int,
    tool_name: str,
    args: Dict[str, Any],
    decisions: List[Decision],
    original: Dict[str, Any],
    limit: int,
) -> None:
    """Link each document line to an EXACT catalog entry when one exists.

    The link is OPTIONAL: a line may legitimately stay free text (the catalog
    need not stock the item), so an unmatched or ambiguous item is recorded and
    left UNLINKED — it never blocks the document and never guesses between
    candidates.
    """
    aliases = LINE_REFERENCE_TOOLS.get(tool_name)
    items = args.get("items")
    if not aliases or not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("product_id") or item.get("service_id"):
            continue
        alias = next((key for key in aliases if item.get(key)), None)
        if alias is None:
            continue
        name = str(item[alias]).strip()
        if not name:
            continue
        if not str(item.get("description") or "").strip():
            # The item name the user stated fills the line's required
            # description — a transcription of the approved plan, not a guess.
            item["description"] = name
            decisions.append(
                Decision(
                    tool_name=tool_name, index=index,
                    parameter="items[].description", role="line",
                    requested=name, status="stated", original_arguments=original,
                )
            )
        for role, key in (("product", "product_id"), ("service", "service_id")):
            resolution = await resolve_reference(
                organization_id=organization_id, role=role, name=name, limit=limit
            )
            if resolution.status == ONE:
                item[key] = resolution.match_id
                item.pop(alias, None)
                decisions.append(
                    Decision(
                        tool_name=tool_name, index=index, parameter=f"items[].{key}",
                        role=role, requested=name, status=ONE,
                        resolved_id=resolution.match_id, optional=True,
                        original_arguments=original,
                    )
                )
                break
            if resolution.status in (MULTIPLE, ERROR):
                item.pop(alias, None)
                decisions.append(
                    Decision(
                        tool_name=tool_name, index=index, parameter=f"items[].{key}",
                        role=role, requested=name, status=resolution.status,
                        candidates=resolution.candidates, optional=True,
                        original_arguments=original,
                    )
                )
                break
        else:
            # No exact match in either catalog: a free-text line.  Recorded,
            # left unlinked, never guessed.
            item.pop(alias, None)
            decisions.append(
                Decision(
                    tool_name=tool_name, index=index, parameter="items[]",
                    role="product", requested=name, status=NONE, optional=True,
                    original_arguments=original,
                )
            )


def _batches(
    total: int,
    creation_index: Dict[str, int],
    deferred: Dict[int, List[ReferenceSpec]],
) -> List[List[int]]:
    """Split the plan so a dependent call runs only AFTER its prerequisite."""
    if not deferred:
        return [list(range(total))] if total else []
    split_after = sorted(
        {
            creation_index[spec.role]
            for specs in deferred.values()
            for spec in specs
            if spec.role in creation_index
        }
    )
    batches: List[List[int]] = []
    start = 0
    for index in split_after:
        if index >= start:
            batches.append(list(range(start, index + 1)))
            start = index + 1
    if start < total:
        batches.append(list(range(start, total)))
    return batches


async def prepare_plan(
    *,
    organization_id: uuid.UUID,
    calls: Sequence[ToolCall],
    contracts: Optional[Dict[str, Dict[str, Any]]] = None,
    limit: int = 5,
) -> PlanMaterialization:
    """Static, READ-ONLY materialization of an approved plan.

    Fills every declared reference the live books can resolve, recognises the
    references a creation step OF THE SAME APPROVED PLAN will supply, and BLOCKS
    (with an explicit question) on anything else.  Nothing is executed and
    nothing is written here.
    """
    from app.tool_contract import validate_arguments

    canonical: List[ToolCall] = []
    decisions: List[Decision] = []
    blocks: List[str] = []
    deferred: Dict[int, List[ReferenceSpec]] = {}

    creation_index: Dict[str, int] = {}
    for position, call in enumerate(calls):
        role = CREATION_TOOL_ROLES.get(call.tool_name)
        if role and role not in creation_index:
            creation_index[role] = position

    for position, call in enumerate(calls):
        # DEEP copy: materialization must never mutate the caller's plan — the
        # approved human-level arguments stay intact for the audit record.
        original = copy.deepcopy(dict(call.arguments or {}))
        args = copy.deepcopy(original)
        pending: List[ReferenceSpec] = []

        # The line container itself may carry the model's vocabulary
        # (`line_items`).  Move that DECLARED alias onto the canonical `items`
        # first, so the individual line references can be resolved at all.
        for line_alias in DOCUMENT_LINE_ALIASES.get(call.tool_name, ()):
            if not args.get("items") and args.get(line_alias) is not None:
                args["items"] = args.pop(line_alias)
                decisions.append(
                    Decision(
                        tool_name=call.tool_name,
                        index=position,
                        parameter="items",
                        role="line",
                        requested=line_alias,
                        status=ALIAS,
                        original_arguments=original,
                    )
                )

        for parameter, spec in DECLARED_REFERENCES.get(call.tool_name, {}).items():
            if args.get(parameter):
                continue  # a canonical id is already present — never overwritten
            alias = next((key for key in spec.aliases if args.get(key)), None)
            if alias is None:
                continue
            requested = args[alias]
            resolution = await resolve_reference(
                organization_id=organization_id,
                role=spec.role,
                name=requested,
                limit=limit,
            )
            decision = Decision(
                tool_name=call.tool_name,
                index=position,
                parameter=parameter,
                role=spec.role,
                requested=str(requested),
                status=resolution.status,
                candidates=resolution.candidates,
                optional=not spec.required,
                original_arguments=original,
            )
            if resolution.status == ONE:
                args[parameter] = resolution.match_id
                args.pop(alias, None)
                decision.resolved_id = resolution.match_id
                decisions.append(decision)
                continue
            if not spec.required:
                args.pop(alias, None)
                decisions.append(decision)
                continue
            if resolution.status in (MULTIPLE, ERROR):
                # Ambiguity — or an unreadable book — is NEVER settled by
                # leaning on a later creation step.  Stop and ask.
                decisions.append(decision)
                blocks.append(
                    _block_message(resolution, tool_name=call.tool_name, name=str(requested))
                )
                continue
            prerequisite = creation_index.get(spec.role)
            if prerequisite is not None and prerequisite < position:
                # The approved plan creates it earlier: that batch supplies the
                # id via ``bind_deferred`` — never invented here.
                args.pop(alias, None)
                pending.append(spec)
                decisions.append(decision)
                continue
            decisions.append(decision)
            blocks.append(
                _block_message(resolution, tool_name=call.tool_name, name=str(requested))
            )

        await _materialize_items(
            organization_id=organization_id,
            index=position,
            tool_name=call.tool_name,
            args=args,
            decisions=decisions,
            original=original,
            limit=limit,
        )

        canonical.append(ToolCall(tool_name=call.tool_name, arguments=args))
        if pending:
            deferred[position] = pending

    # Final guard: what is handed to the tool must BIND.  Calls still waiting
    # for their prerequisite id are checked by ``bind_deferred``.
    if contracts:
        for position, tool_call in enumerate(canonical):
            if position in deferred:
                continue
            blocks.extend(
                validate_arguments(
                    tool_call.tool_name,
                    tool_call.arguments or {},
                    contracts.get(tool_call.tool_name),
                    declared_reference_inputs().get(tool_call.tool_name),
                )
            )

    return PlanMaterialization(
        calls=canonical,
        decisions=decisions,
        blocks=blocks,
        deferred=deferred,
        batches=_batches(len(canonical), creation_index, deferred),
    )


def declared_reference_inputs() -> Dict[str, Dict[str, Tuple[str, ...]]]:
    """Canonical parameter -> the DECLARED alias keys a plan may use instead.

    Consumed by the argument-contract gate (app/tool_contract.py): an alias is a
    read-only INPUT to resolution, so it must satisfy both the unknown-key check
    and the required check for its parameter.  Without this the gate forbids the
    only shape a model can express when the referenced record does not exist yet
    — it has no id to pass, and inventing one is forbidden — so the
    creation-then-use sequence would be unplannable (production 2026-09-22).
    """
    declared: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    for tool_name, specs in DECLARED_REFERENCES.items():
        entry: Dict[str, Tuple[str, ...]] = {
            parameter: tuple(spec.aliases) for parameter, spec in specs.items()
        }
        line_aliases = DOCUMENT_LINE_ALIASES.get(tool_name)
        if line_aliases:
            entry["items"] = tuple(line_aliases)
        declared[tool_name] = entry
    return declared


def captured_ids(
    *, calls: Sequence[ToolCall], results: Sequence[Any]
) -> Dict[str, str]:
    """Role -> canonical id, taken from SUCCESSFUL approved creation steps."""
    found: Dict[str, str] = {}
    for call, data in zip(calls, results or []):
        role = CREATION_TOOL_ROLES.get(call.tool_name)
        if not role or not isinstance(data, dict) or not data.get("success"):
            continue
        payload = data.get("data")
        record_id = payload.get("id") if isinstance(payload, dict) else data.get("id")
        if record_id:
            found[role] = str(record_id)
    return found


def bind_deferred(
    *,
    call: ToolCall,
    specs: Sequence[ReferenceSpec],
    provided_ids: Dict[str, str],
    index: int,
    decisions: List[Decision],
) -> Tuple[ToolCall, List[str]]:
    """Bind the ids an approved prerequisite produced — never invent one."""
    args = dict(call.arguments or {})
    problems: List[str] = []
    for spec in specs:
        record_id = provided_ids.get(spec.role)
        if not record_id:
            problems.append(
                f"{call.tool_name}: the {spec.role} it needs was not created by the "
                "approved plan, so the document was NOT recorded — no id was invented."
            )
            continue
        args[spec.parameter] = record_id
        decisions.append(
            Decision(
                tool_name=call.tool_name,
                index=index,
                parameter=spec.parameter,
                role=spec.role,
                requested="",
                status=PREREQUISITE,
                resolved_id=record_id,
                source=f"prerequisite:{ROLE_CREATION_TOOLS.get(spec.role, '')}",
            )
        )
    return ToolCall(tool_name=call.tool_name, arguments=args), problems


def step_payload(
    materialization: PlanMaterialization, *, original: Sequence[ToolCall]
) -> Dict[str, Any]:
    """Durable audit record: the ORIGINAL human-level plan plus every decision.

    The approved snapshot (``ai.confirmations.plan``) is never modified, so this
    step is what lets an auditor reconstruct exactly how a human-level plan
    became the canonical arguments that were executed.
    """
    return {
        "original_plan": [
            {"tool_name": call.tool_name, "arguments": dict(call.arguments or {})}
            for call in original
        ],
        "canonical_plan": [
            {"tool_name": call.tool_name, "arguments": dict(call.arguments or {})}
            for call in materialization.calls
        ],
        "decisions": [decision.as_dict() for decision in materialization.decisions],
        "deferred_steps": {
            str(index + 1): [spec.parameter for spec in specs]
            for index, specs in materialization.deferred.items()
        },
        "blocks": list(materialization.blocks),
    }

