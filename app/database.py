"""
ERP AI Agent — Supabase Database Layer
========================================
Thin async wrapper around the Supabase Python client.

* Service-role client is used for server-side operations (AI agent, admin).
* All data access respects organisation_id scoping — never trust client-supplied
  org_id; always resolve from authenticated user membership.
* The Gemini API key is fetched from Supabase Vault via
  ``public.get_gemini_api_key()`` (service_role only).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from supabase import Client, create_client

from app.config import get_settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Client singletons
# ---------------------------------------------------------------------------
_service_client: Optional[Client] = None


def get_service_client() -> Client:
    """Return a process-wide Supabase service-role client."""
    global _service_client
    if _service_client is None:
        settings = get_settings()
        _service_client = create_client(
            settings.supabase_url, settings.supabase_service_role_key
        )
        log.info("supabase.service_client", status="initialised")
    return _service_client


# ---------------------------------------------------------------------------
# Gemini API key (from Vault)
# ---------------------------------------------------------------------------
def get_gemini_api_key() -> str:
    """Retrieve the Gemini API key from Supabase Vault.

    Calls ``public.get_gemini_api_key()`` which is a SECURITY DEFINER
    function granting access only to the service_role.
    """
    client = get_service_client()
    result = client.rpc("get_gemini_api_key").execute()
    if not result.data:
        raise RuntimeError("Gemini API key not found in Supabase Vault")
    key = result.data
    if isinstance(key, list):
        key = key[0] if key else None
    if not key:
        raise RuntimeError("Gemini API key is empty in Supabase Vault")
    return str(key)


# ---------------------------------------------------------------------------
# Generic helpers — thin wrappers used by repositories
# ---------------------------------------------------------------------------

def _table(table_name: str):
    """Shortcut to get a table query-builder from the service client.

    Tables prefixed with ``ai_`` are routed to the ``ai`` schema (Control Plane).
    All other tables use the default ``public`` schema.
    """
    client = get_service_client()
    if table_name.startswith("ai_"):
        # Strip the prefix and route to ai schema
        real_name = table_name[3:]  # "ai_tools" -> "tools"
        return client.schema("ai").table(real_name)
    return client.table(table_name)


async def _execute(query):
    """Run a SYNC supabase-py ``.execute()`` inside a worker thread.

    supabase-py is synchronous httpx under the hood.  Calling it directly
    inside ``async def`` helpers BLOCKED the event loop on every request
    AND silently turned ``asyncio.gather(...)`` fan-outs (context build,
    audit writes) into sequential round-trips — the stage-2 latency bug.
    ``asyncio.to_thread`` restores real concurrency for gathered calls and
    keeps the loop responsive while a REST call is in flight.
    """
    return await asyncio.to_thread(query.execute)


# ---- SELECT helpers -------------------------------------------------------

async def fetch_one(
    table_name: str,
    *,
    filters: Dict[str, Any],
    select: str = "*",
) -> Optional[Dict[str, Any]]:
    """Return a single row matching *filters*, or None."""
    q = _table(table_name).select(select)
    for col, val in filters.items():
        q = q.eq(col, val)
    rows = (await _execute(q.limit(1))).data
    return rows[0] if rows else None


async def fetch_many(
    table_name: str,
    *,
    filters: Dict[str, Any],
    select: str = "*",
    order: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return multiple rows matching *filters*."""
    q = _table(table_name).select(select)
    for col, val in filters.items():
        q = q.eq(col, val)
    if order:
        # Callers pass PostgREST-style "column.direction" strings. supabase-py's
        # .order() expects a bare column name and appends the direction itself;
        # passing the full string produced "start_date.asc.asc" (PGRST100).
        _col, _, _direction = order.partition(".")
        q = q.order(_col, desc=(_direction == "desc"))
    q = q.range(offset, offset + limit - 1)
    return (await _execute(q)).data or []


async def search_ilike(
    table_name: str,
    *,
    column: str,
    value: str,
    organization_id: uuid.UUID,
    select: str = "*",
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Fuzzy search using ILIKE within an organisation scope."""
    return (
        await _execute(
            _table(table_name)
            .select(select)
            .eq("organization_id", str(organization_id))
            .ilike(column, f"%{value}%")
            .limit(limit)
        )
    ).data or []


# ---- INSERT helpers -------------------------------------------------------

async def insert_one(table_name: str, *, data: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a single row and return it."""
    rows = (await _execute(_table(table_name).insert(data))).data
    if not rows:
        raise RuntimeError(f"Insert into {table_name} returned no rows")
    return rows[0]


async def insert_many(
    table_name: str, *, data: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Insert multiple rows and return them."""
    return (await _execute(_table(table_name).insert(data))).data or []


# ---- UPDATE helpers -------------------------------------------------------

async def update_one(
    table_name: str,
    *,
    row_id: uuid.UUID,
    data: Dict[str, Any],
    organization_id: Optional[uuid.UUID] = None,
) -> Optional[Dict[str, Any]]:
    """Update a row by primary key and return the updated row.

    DEFENCE IN DEPTH: the backend uses the service-role key, so RLS does not
    apply and a primary key alone would reach ANY tenant's row.  Pass
    ``organization_id`` for tenant-owned tables to add it to the WHERE clause
    as a second guard, so a foreign row id can never be updated even if a
    future caller forgets the org-scoped read that normally precedes this.
    """
    q = _table(table_name).update(data).eq("id", str(row_id))
    if organization_id is not None:
        q = q.eq("organization_id", str(organization_id))
    rows = (await _execute(q)).data
    return rows[0] if rows else None


# ---- DELETE helpers -------------------------------------------------------

async def delete_one(
    table_name: str,
    *,
    row_id: uuid.UUID,
    organization_id: Optional[uuid.UUID] = None,
) -> bool:
    """Delete a row by primary key. Returns True if deleted.

    ``organization_id`` is an optional second guard for tenant-owned tables —
    see :func:`update_one`.
    """
    q = _table(table_name).delete().eq("id", str(row_id))
    if organization_id is not None:
        q = q.eq("organization_id", str(organization_id))
    result = await _execute(q)
    return bool(result.data)


# ---- RPC helpers ----------------------------------------------------------

async def call_rpc(function_name: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
    """Call a Supabase RPC function and return the result data."""
    rpc = get_service_client().rpc(function_name, params or {})
    return (await _execute(rpc)).data


# ---- AI Control Plane helpers ---------------------------------------------

# Cached control-plane FK ids: agent / instruction version / model config.
_CONTROL_PLANE_IDS: Optional[Dict[str, Any]] = None

# Cached slug -> ai.tools.id map (tool_calls.tool_id is a FK to ai.tools).
_TOOL_ID_CACHE: Optional[Dict[str, str]] = None


async def _get_control_plane_ids() -> Dict[str, Any]:
    """Resolve (and cache) the active agent, instruction version and model config ids."""
    global _CONTROL_PLANE_IDS
    if _CONTROL_PLANE_IDS is not None:
        return _CONTROL_PLANE_IDS
    agent = await fetch_one("ai_agents", filters={"status": "ACTIVE"})
    if not agent:
        raise RuntimeError("No ACTIVE agent found in ai.agents — cannot create sessions")
    ids: Dict[str, Any] = {"agent_id": agent["id"]}
    instruction_version = await fetch_one(
        "ai_instruction_versions",
        filters={
            "agent_id": agent["id"],
            "version": agent.get("current_instruction_version") or "1.2.0",
        },
    )
    ids["instruction_version_id"] = (
        instruction_version["id"] if instruction_version else None
    )
    ids["model_configuration_id"] = agent.get("default_model_configuration_id")
    _CONTROL_PLANE_IDS = ids
    return ids


async def _resolve_tool_id(tool_slug: str) -> Optional[str]:
    """Resolve a tool slug to its ``ai.tools.id`` (cached bulk lookup)."""
    global _TOOL_ID_CACHE
    if _TOOL_ID_CACHE is None:
        rows = await fetch_many(
            "ai_tools",
            filters={"status": "IMPLEMENTED"},
            select="id,slug",
            limit=200,
        )
        _TOOL_ID_CACHE = {row["slug"]: row["id"] for row in rows}
    return _TOOL_ID_CACHE.get(tool_slug)


async def get_agent(organization_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Fetch the active ERP agent."""
    return await fetch_one(
        "ai_agents",   # routes to ai.agents via _table() prefix logic
        filters={"status": "ACTIVE"},
    )


async def get_tools_for_capability(
    agent_id: uuid.UUID, capability: str
) -> List[Dict[str, Any]]:
    """Return tools allowed for a given capability (from ai.permissions)."""
    perms = await fetch_one(
        "ai_permissions",
        filters={"agent_id": str(agent_id), "capability": capability},
    )
    if not perms:
        return []
    allowed_slugs: List[str] = perms.get("allowed_tools") or []
    if not allowed_slugs:
        return []
    # Fetch tool rows matching slugs
    tools = []
    for slug in allowed_slugs:
        t = await fetch_one("ai_tools", filters={"slug": slug, "status": "IMPLEMENTED"})
        if t:
            tools.append(t)
    return tools


async def get_context_sources_for_intent(intent: str) -> Dict[str, Any]:
    """Return the context rule + resolved sources for a given intent."""
    rule = await fetch_one("ai_context_rules", filters={"intent": intent, "status": "ACTIVE"})
    if not rule:
        return {"rule": None, "sources": []}

    required = rule.get("required_sources") or []
    optional = rule.get("optional_sources") or []
    all_slugs = required + optional

    sources = []
    for slug in all_slugs:
        src = await fetch_one("ai_context_sources", filters={"slug": slug, "status": "ACTIVE"})
        if src:
            sources.append(src)

    return {"rule": rule, "sources": sources}


async def create_execution_session(
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message: str,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new AI execution session (status=PENDING, current_phase=RECEIVED).

    ``agent_id`` / ``instruction_version_id`` / ``model_configuration_id`` are
    resolved from the active agent row in ``ai.agents`` (cached) — matching the
    real ``ai.execution_sessions`` columns.
    """
    cp = await _get_control_plane_ids()
    return await insert_one(
        "ai_execution_sessions",
        data={
            "organization_id": str(organization_id),
            "user_id": str(user_id),
            "agent_id": cp["agent_id"],
            "instruction_version_id": cp.get("instruction_version_id"),
            "model_configuration_id": cp.get("model_configuration_id"),
            "conversation_id": conversation_id,
            "user_request": user_message,
            "status": "PENDING",
            "current_phase": "RECEIVED",
        },
    )


async def update_execution_session(
    session_id: uuid.UUID, *, data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update an execution session."""
    return await update_one("ai_execution_sessions", row_id=session_id, data=data)


async def set_session_phase(
    session_id: uuid.UUID,
    *,
    phase: str,
    status: str,
    completed: bool = False,
) -> Optional[Dict[str, Any]]:
    """Update ``current_phase`` (execution_phase_code) + ``status`` (session_status_code).

    The 13-value phase enum and the 7-value session enum are distinct in the
    database — the caller maps phase→status (see agent._PHASE_STATUS_MAP).
    """
    data: Dict[str, Any] = {"current_phase": phase, "status": status}
    if completed:
        data["completed_at"] = datetime.now(timezone.utc).isoformat()
    return await update_execution_session(session_id, data=data)


# ai.execution_steps.step_type values (step_type_code enum).
_STEP_TYPE_MAP: Dict[str, str] = {
    "RECEIVED": "REASON",
    "INTERPRETING": "REASON",
    "PLANNING": "REASON",
    "CONTEXT_LOADING": "RETRIEVE",
    "AWAITING_CLARIFICATION": "REASON",
    "VALIDATING": "VALIDATE",
    "AWAITING_CONFIRMATION": "CONFIRM",
    "EXECUTING": "EXECUTE",
    "VERIFYING": "VERIFY",
    "COMPLETED": "RESPOND",
    "FAILED": "RESPOND",
    "CANCELLED": "RESPOND",
    "REJECTED": "RESPOND",
}


async def create_execution_step(
    *,
    session_id: uuid.UUID,
    step_type: str,
    step_data: Optional[Dict[str, Any]] = None,
    status: str = "COMPLETED",
) -> Dict[str, Any]:
    """Record an execution step within a session.

    ``step_order`` has no database default — it is computed from the number of
    existing steps for the session.
    """
    existing = await fetch_many(
        "ai_execution_steps",
        filters={"execution_session_id": str(session_id)},
        select="id",
        limit=1000,
    )
    summary = json.dumps(step_data, default=str)[:2000] if step_data else None
    return await insert_one(
        "ai_execution_steps",
        data={
            "execution_session_id": str(session_id),
            "step_order": len(existing) + 1,
            "step_type": _STEP_TYPE_MAP.get(step_type, "REASON"),
            "description": step_type,
            "input_summary": summary,
            "status": status,
        },
    )


async def create_tool_call(
    *,
    session_id: uuid.UUID,
    step_id: Optional[uuid.UUID] = None,
    tool_name: str,
    tool_input: Dict[str, Any],
    tool_output: Optional[Dict[str, Any]] = None,
    status: str = "COMPLETED",
) -> Optional[Dict[str, Any]]:
    """Record a tool call within an execution session.

    ``tool_id`` is a FK to ``ai.tools`` resolved from the slug (cached);
    ``call_order`` is computed per session.
    """
    tool_id = await _resolve_tool_id(tool_name)
    if not tool_id:
        log.warning("control_plane.tool_id_unresolved", tool=tool_name)
        return None
    existing = await fetch_many(
        "ai_tool_calls",
        filters={"execution_session_id": str(session_id)},
        select="id",
        limit=1000,
    )
    return await insert_one(
        "ai_tool_calls",
        data={
            "execution_session_id": str(session_id),
            "execution_step_id": str(step_id) if step_id else None,
            "tool_id": tool_id,
            "call_order": len(existing) + 1,
            "input_payload": tool_input or {},
            "output_payload": tool_output or {},
            "status": status,
        },
    )


async def create_clarification(
    *,
    session_id: uuid.UUID,
    question: str,
    required_fields: Optional[List[str]] = None,
    options: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Create a clarification record and set session to WAITING_FOR_USER."""
    await set_session_phase(
        session_id, phase="AWAITING_CLARIFICATION", status="WAITING_FOR_USER"
    )
    return await insert_one(
        "ai_clarifications",
        data={
            "execution_session_id": str(session_id),
            "question": question,
            "required_information": required_fields or [],
            "options": options or [],
            "status": "WAITING_FOR_USER",
        },
    )


async def resolve_clarification(
    *,
    session_id: uuid.UUID,
    user_response: str,
) -> Optional[Dict[str, Any]]:
    """Mark the session's latest unanswered clarification as answered."""
    pending = await fetch_one(
        "ai_clarifications",
        filters={
            "execution_session_id": str(session_id),
            "status": "WAITING_FOR_USER",
        },
    )
    if not pending:
        return None
    return await update_one(
        "ai_clarifications",
        row_id=uuid.UUID(str(pending["id"])),
        data={
            "user_response": user_response,
            "answered_at": datetime.now(timezone.utc).isoformat(),
            "status": "COMPLETED",
        },
    )


async def get_clarification_history(
    session_id: uuid.UUID,
) -> List[Dict[str, str]]:
    """Return all ANSWERED clarifications for a session as Q&A pairs.

    Used to rebuild conversation context when a session resumes, so
    questions already answered are merged into entity extraction and
    never asked again.
    """
    rows = await fetch_many(
        "ai_clarifications",
        filters={
            "execution_session_id": str(session_id),
            "status": "COMPLETED",
        },
        order="created_at",
        limit=20,
    )
    return [
        {
            "question": str(r.get("question") or ""),
            "answer": str(r.get("user_response") or ""),
        }
        for r in rows
        if r.get("user_response")
    ]


async def seed_clarification_history(
    session_id: uuid.UUID,
    history: List[Dict[str, str]],
) -> None:
    """Carry answered clarifications from a prior session into a resumed one.

    Answering a clarification re-enters ``execute()`` under a NEW session
    row.  Copying the answered Q&A into that new session keeps
    ``get_clarification_history`` complete across rounds, so a question
    answered in round 1 is never re-asked in round 2+.
    """
    if not history:
        return
    for qa in history:
        await insert_one(
            "ai_clarifications",
            data={
                "execution_session_id": str(session_id),
                "question": str(qa.get("question") or ""),
                "user_response": str(qa.get("answer") or ""),
                "required_information": [],
                "options": [],
                "status": "COMPLETED",
                "answered_at": datetime.now(timezone.utc).isoformat(),
            },
        )


async def create_confirmation(
    *,
    session_id: uuid.UUID,
    action_type: str,
    description: str,
    risk_level: str = "MEDIUM",
    plan: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Create a confirmation record and set session to WAITING_FOR_USER.

    ``plan`` snapshots the exact tool calls the user is approving (migration
    079).  The resumed run rebuilds THIS plan instead of re-planning, so
    approval cannot be followed by different tools or a lost entity value.
    """
    await set_session_phase(
        session_id, phase="AWAITING_CONFIRMATION", status="WAITING_FOR_USER"
    )
    return await insert_one(
        "ai_confirmations",
        data={
            "execution_session_id": str(session_id),
            "action_type": action_type,
            "description": description,
            "risk_level": risk_level,
            "confirmation_required": True,
            "plan": plan or [],
        },
    )


async def resolve_confirmation(
    *,
    session_id: uuid.UUID,
    approved: bool,
    user_id: Optional[uuid.UUID] = None,
) -> Optional[Dict[str, Any]]:
    """Record the user's decision on the session's pending confirmation."""
    confirmations = await fetch_many(
        "ai_confirmations",
        filters={"execution_session_id": str(session_id)},
        order="created_at.asc",
        limit=100,
    )
    pending = next(
        (c for c in confirmations if c.get("user_confirmed") is None), None
    )
    if not pending:
        return None
    return await update_one(
        "ai_confirmations",
        row_id=uuid.UUID(str(pending["id"])),
        data={
            "user_confirmed": approved,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "user_id": str(user_id) if user_id else None,
        },
    )


async def create_execution_result(
    *,
    session_id: uuid.UUID,
    result_data: Dict[str, Any],
    summary: Optional[str] = None,
    action_type: Optional[str] = None,
    affected_entities: Optional[List[Dict[str, Any]]] = None,
    verification_status: str = "VERIFIED",
    status: str = "COMPLETED",
) -> Dict[str, Any]:
    """Record the final execution result for a session."""
    return await insert_one(
        "ai_execution_results",
        data={
            "execution_session_id": str(session_id),
            "status": status,
            "summary": summary,
            "action_type": action_type,
            "affected_entities": affected_entities or [],
            "result_payload": result_data,
            "verification_status": verification_status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
