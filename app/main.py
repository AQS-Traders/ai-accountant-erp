"""
ERP AI Agent — FastAPI Application
=====================================
HTTP endpoints for the AI agent.

Endpoints:
  POST /api/ai/execute         — Send a natural-language message
  POST /api/ai/clarify         — Answer a clarification question
  POST /api/ai/confirm         — Approve/reject a confirmation
  GET  /api/ai/sessions        — List recent execution sessions
  GET  /api/ai/sessions/{id}   — Get a single session with details
  GET  /api/health             — Health check
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.auth import (
    AuthContext,
    UserContext,
    authenticate_header,
    authenticate_user_header,
)
from app.config import get_settings
from app.database import fetch_many, fetch_one, insert_one, set_session_phase
from app.models.schemas import (
    AgentResponse,
    ClarificationAnswer,
    ConfirmationDecision,
    UserRequest,
)

# NOTE: `app.agent` (planner → reasoning → tools → AI providers) is
# intentionally NOT imported at module level. On serverless platforms the
# whole module graph is imported per cold start; importing the agent stack
# here pushed initialisation past the function time limit ("Worker timed
# out"). Each AI endpoint lazy-imports what it needs instead.

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    settings = get_settings()
    log.info("app.startup", env=settings.app_env, port=settings.app_port)

    # Pre-warm the AI orchestrator (Qwen primary / Gemini fallback).
    # Provider initialisation is lazy and MUST NEVER block or crash
    # startup — a broken/missing provider only degrades availability.
    try:
        from app.ai_orchestrator import get_client
        client = get_client()
        try:
            client._get_qwen()
            log.info("app.qwen_client", status="ready",
                     model=get_settings().qwen_model)
        except Exception as exc:
            log.warning("app.qwen_client_warm_failed", error=str(exc))
        try:
            client._get_gemini()
            log.info("app.gemini_client", status="ready")
        except Exception as exc:
            log.warning("app.gemini_client_warm_failed", error=str(exc))
    except Exception as exc:
        log.warning("app.ai_orchestrator_warm_failed", error=str(exc))

    # Pre-import the heavy agent stack in the BACKGROUND (never blocks
    # startup, so cold-start budgets are respected). The first user request
    # then skips the multi-second `import app.agent` — the agent starts
    # working noticeably sooner.
    def _prewarm_agent() -> None:
        try:
            import importlib

            importlib.import_module("app.agent")
            log.info("app.agent_prewarm", status="ready")
        except Exception as exc:  # noqa: BLE001 — prewarm is best-effort
            log.warning("app.agent_prewarm_failed", error=str(exc)[:200])

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _prewarm_agent)
    except Exception as exc:  # noqa: BLE001
        log.warning("app.agent_prewarm_scheduling_failed", error=str(exc))
    yield
    log.info("app.shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ERP AI Agent",
    description="AI-powered accounting & financial ERP agent",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — added at module level before any requests are served.
# Guarded: on serverless platforms (e.g. Vercel) the function cold-starts
# by importing this module for EVERY request. If required environment
# variables are missing there, Settings() would raise and crash the
# function (FUNCTION_INVOCATION_FAILED) — hiding even /api/health from
# diagnostics. Degrade to empty CORS origins instead; endpoints that
# need config will still fail loudly at request time with a clear error.
try:
    _cors_origins: List[str] = get_settings().cors_origin_list
    _config_ok: bool = True
except Exception as exc:  # pragma: no cover — misconfigured deployment
    log.error("config.settings_invalid", error=str(exc))
    _cors_origins = []
    _config_ok = False
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth dependency — supports JWT (production) and header fallback (dev)
# ---------------------------------------------------------------------------
async def get_current_user(
    auth: AuthContext = Depends(authenticate_header),
) -> AuthContext:
    """FastAPI dependency: authenticate via JWT or legacy headers.

    Returns an AuthContext with user_id, organization_id, role, and permissions.
    The organisation_id is ALWAYS resolved from the user's membership,
    never trusted from client-supplied headers alone.
    """
    return auth


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health_check():
    """Liveness probe — safe to call even when the environment is misconfigured."""
    return {"status": "ok", "version": "1.0.0", "config_ok": _config_ok}


@app.get("/api/ai/providers")
async def ai_providers(
    force: bool = False,
    auth: AuthContext = Depends(get_current_user),
):
    """AI provider health/status — is Qwen (primary) or Gemini (fallback) available?

    Never raises: each provider reports configured/available/detail
    independently, so the ERP can degrade gracefully. Results are cached
    briefly (see provider_health_ttl_seconds); pass ?force=true to refresh.

    AUTH REQUIRED: probing providers performs real round-trips to the AI
    vendors (each selecting/initialising a client), so an unauthenticated
    caller could both burn vendor quota and read the internal workspace
    endpoint. Only an authenticated org member may trigger it.
    """
    from app.ai_orchestrator import get_client

    try:
        status = await get_client().get_providers_status(force=force)
        return {"status": "ok", **status}
    except Exception as exc:  # noqa: BLE001 — status must never 500
        return {"status": "error", "detail": str(exc)[:200], "providers": {}}


@app.post("/api/ai/execute", response_model=AgentResponse)
async def ai_execute(
    request: UserRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Process a natural-language message through the AI agent."""
    from app.agent import execute  # lazy: heavy agent stack (serverless cold-start)

    log.info(
        "api.execute",
        user_id=str(auth.user_id),
        org_id=str(auth.organization_id),
        role=auth.role_code,
        message_len=len(request.message),
    )

    response = await execute(
        user_message=request.message,
        user_id=auth.user_id,
        organization_id=auth.organization_id,
        auth=auth,
        conversation_id=request.conversation_id,
        attachments=request.attachments,
    )
    return response


@app.post("/api/ai/clarify", response_model=AgentResponse)
async def ai_clarify(
    answer: ClarificationAnswer,
    auth: AuthContext = Depends(get_current_user),
):
    """Resume a session with the user's answer to a clarification."""
    from app.agent import (  # lazy: heavy agent stack (serverless cold-start)
        resume_with_clarification,
    )

    response = await resume_with_clarification(
        session_id=answer.session_id,
        user_answer=answer.answer,
        user_id=auth.user_id,
        organization_id=auth.organization_id,
        auth=auth,
    )
    return response


@app.post("/api/ai/confirm", response_model=AgentResponse)
async def ai_confirm(
    decision: ConfirmationDecision,
    auth: AuthContext = Depends(get_current_user),
):
    """Approve or reject a pending confirmation."""
    from app.agent import (  # lazy: heavy agent stack (serverless cold-start)
        resume_with_confirmation,
    )

    response = await resume_with_confirmation(
        session_id=decision.session_id,
        approved=decision.approved,
        user_id=auth.user_id,
        organization_id=auth.organization_id,
        auth=auth,
    )
    return response


# ---------------------------------------------------------------------------
# Work Stream C: BACKGROUND AI RUNS (DB claim/lease queue + worker process)
# ---------------------------------------------------------------------------
# POST /api/ai/jobs queues the run and returns IMMEDIATELY; a supervised
# worker process (scripts/ai_worker.py) claims and executes it via the same
# trusted execute() pipeline. Clients poll GET /api/ai/jobs/{id} or the
# existing /api/ai/progress endpoint for the live state.
# ---------------------------------------------------------------------------
@app.post("/api/ai/jobs")
async def enqueue_ai_job(
    request: UserRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Queue a natural-language AI run and return without waiting."""
    job = await insert_one(
        "ai_worker_jobs",
        data={
            "organization_id": str(auth.organization_id),
            "user_id": str(auth.user_id),
            "job_type": "ai_execute",
            "payload": {
                "message": request.message,
                "conversation_id": request.conversation_id,
                "attachments": [
                    a.model_dump(mode="json")
                    for a in (request.attachments or [])
                ],
            },
        },
    )
    log.info("api.job_queued", job_id=job["id"], user_id=str(auth.user_id))
    return {
        "queued": True,
        "job_id": job["id"],
        "conversation_id": request.conversation_id,
    }


@app.get("/api/ai/jobs/{job_id}")
async def get_ai_job(
    job_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_user),
):
    """Status/result of a queued AI run (owner-scoped)."""
    job = await fetch_one(
        "ai_worker_jobs",
        filters={
            "id": str(job_id),
            "organization_id": str(auth.organization_id),
            "user_id": str(auth.user_id),
        },
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = job.get("payload") or {}
    return {
        "job_id": job["id"],
        "status": job["status"],
        "conversation_id": payload.get("conversation_id"),
        "result": job.get("result"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Work Stream D: STREAMING EXECUTION (SSE)
# ---------------------------------------------------------------------------
# Same trusted execute() pipeline as POST /api/ai/execute, but the response
# streams progress: an `event: step` message for every reasoning step as it
# is recorded, and one `event: final` message with the complete
# AgentResponse once verification finished (the result card only renders on
# `final`). The non-streaming POST contract is unchanged.
# ---------------------------------------------------------------------------
@app.post("/api/ai/execute-stream")
async def ai_execute_stream(
    request: UserRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Run the agent and stream steps + the final response over SSE."""

    async def event_stream():
        # Instant ack: the progress UI starts on the very first byte —
        # before the heavy agent import and the first DB poll.
        yield (
            "event: step\n"
            + "data: "
            + json.dumps(
                {
                    "step_type": "RECEIVED",
                    "phase": "RECEIVED",
                    "status": "started",
                    "created_at": "",
                }
            )
            + "\n\n"
        )
        from app.agent import execute  # lazy: heavy agent stack (serverless cold-start)

        run_task = asyncio.create_task(
            execute(
                user_message=request.message,
                user_id=auth.user_id,
                organization_id=auth.organization_id,
                auth=auth,
                conversation_id=request.conversation_id,
                attachments=request.attachments,
            )
        )
        seen_step_ids: set[str] = set()
        session_row: Optional[Dict[str, Any]] = None
        heartbeat = 0

        while not run_task.done():
            # Resolve the session once via the conversation_id.
            if session_row is None:
                session_row = await fetch_one(
                    "ai_execution_sessions",
                    filters={"conversation_id": request.conversation_id},
                )
            if session_row is not None:
                steps = await fetch_many(
                    "ai_execution_steps",
                    filters={
                        "execution_session_id": session_row["id"],
                    },
                    order="created_at.asc",
                    limit=200,
                )
                for step in steps:
                    sid = str(step.get("id"))
                    if sid in seen_step_ids:
                        continue
                    seen_step_ids.add(sid)
                    yield (
                        "event: step\n"
                        + "data: "
                        + json.dumps(
                            {
                                "step_type": step.get("step_type"),
                                # The PHASE - not the coarse enum.  `description`
                                # holds the real phase name (INTERPRETING,
                                # PLANNING, CONTEXT_LOADING, EXECUTING, ...),
                                # which is what the progress UI maps to a label
                                # and colours.  Sending the enum (REASON/
                                # RETRIEVE) matched nothing in the UI's
                                # PHASE_ORDER, so every run displayed the
                                # fallback string "Agent working" and the
                                # pipeline stages never lit up.  This now
                                # matches /api/ai/progress exactly.
                                "phase": step.get("description") or step.get("step_type"),
                                "status": step.get("status"),
                                "created_at": str(step.get("created_at")),
                            }
                        )
                        + "\n\n"
                    )
            # Keepalive comment every ~15 polls so proxies never time out.
            heartbeat += 1
            if heartbeat % 15 == 0:
                yield ": keepalive\n\n"
            # 150ms poll: stage updates land in the same tick the DB step
            # is written — the run feels instant instead of chunky.
            await asyncio.sleep(0.15)

        response = await run_task
        yield (
            "event: final\n"
            + "data: "
            + response.model_dump_json()
            + "\n\n"
        )
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Reasoning progress — SAFE projection of real agent state (no chain-of-thought)
# ---------------------------------------------------------------------------
@app.get("/api/ai/progress")
async def ai_progress(
    conversation_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """Real-time, user-safe reasoning progress for an in-flight execution.

    The frontend polls this while POST /api/ai/execute runs. It returns the
    REAL recorded execution steps from ai.execution_steps (plus tool slugs
    from ai.tool_calls) — never model prompts, raw reasoning, or hidden
    chain-of-thought. Scoping is strict: session rows must match BOTH the
    caller's organization and user.
    """
    # conversation_id is a UUID column — reject malformed ids gracefully.
    try:
        uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        return {
            "found": False,
            "current_phase": None,
            "status": None,
            "steps": [],
            "tools": [],
        }

    sessions = await fetch_many(
        "ai_execution_sessions",
        filters={
            "conversation_id": conversation_id,
            "organization_id": str(auth.organization_id),
            "user_id": str(auth.user_id),
        },
        order="created_at.desc",
        limit=1,
    )
    if not sessions:
        return {
            "found": False,
            "current_phase": None,
            "status": None,
            "steps": [],
            "tools": [],
        }

    session = sessions[0]
    session_id = session["id"]

    steps: List[Dict[str, Any]] = []
    raw_steps = await fetch_many(
        "ai_execution_steps",
        filters={"execution_session_id": session_id},
        order="created_at.asc",
        limit=50,
    )
    # Whitelisted summary keys — safe for user display.
    allowed_keys = {
        "intent",
        "extracted_entities",
        "attachments",
        "context_chars",
        "customers",
        "suppliers",
        "accounts",
        "projects",
        "documents",
        "transaction_nature",
        "confidence",
        "source",
        "question",
        "reason",
        "reason_failed",
    }
    for step in raw_steps:
        summary: Dict[str, Any] = {}
        raw_summary = step.get("input_summary")
        if isinstance(raw_summary, str) and raw_summary:
            try:
                parsed = json.loads(raw_summary)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                for key in allowed_keys:
                    if key in parsed and parsed[key] is not None:
                        summary[key] = parsed[key]
        steps.append(
            {
                "step_type": step.get("step_type"),
                "phase": step.get("description"),
                "status": step.get("status"),
                "created_at": step.get("created_at"),
                "summary": summary,
            }
        )

    # Tool calls — slugs + status only (never payloads, which may carry data).
    tool_calls = await fetch_many(
        "ai_tool_calls",
        filters={"execution_session_id": session_id},
        limit=50,
    )
    tool_slugs: List[Dict[str, Any]] = []
    if tool_calls:
        tool_rows = await fetch_many("ai_tools", filters={}, limit=200)
        slug_by_id = {t.get("id"): t.get("slug") for t in tool_rows}
        tool_slugs = [
            {
                "tool": slug_by_id.get(tc.get("tool_id")) or "unknown",
                "status": tc.get("status"),
            }
            for tc in tool_calls
        ]

    return {
        "found": True,
        "current_phase": session.get("current_phase"),
        "status": session.get("status"),
        "steps": steps,
        "tools": tool_slugs,
    }


@app.get("/api/ai/sessions")
async def list_sessions(
    limit: int = 20,
    offset: int = 0,
    auth: AuthContext = Depends(get_current_user),
):
    """List recent execution sessions for the current user."""
    sessions = await fetch_many(
        "ai_execution_sessions",
        filters={
            "organization_id": str(auth.organization_id),
            "user_id": str(auth.user_id),
        },
        order="created_at.desc",
        limit=limit,
        offset=offset,
    )
    return {"sessions": sessions, "count": len(sessions)}


# ---------------------------------------------------------------------------
# RESUME-BY-CONVERSATION — the stale-run window
# ---------------------------------------------------------------------------
# A run only ever advances while its serverless invocation is alive. When the
# host kills that invocation (FUNCTION_INVOCATION_TIMEOUT, cold-start
# eviction) nothing is left to write the terminal state, so the row stays
# PENDING / PLANNING / EXECUTING for ever. Returning such a corpse made every
# dashboard load re-attach to a dead run: an endless "Processing" spinner plus
# a poll loop that never stopped, with no user input involved at all. Rows are
# now aged out (and stamped FAILED) so a stranded run is never re-attached and
# the leak cannot accumulate either.
_ACTIVE_RUN_TTL_SECONDS = 180.0         # PENDING / PLANNING / EXECUTING
_AWAITING_USER_TTL_SECONDS = 45 * 60.0  # WAITING_FOR_USER (human latency)
_ACTIVE_RUN_STATUSES = {"PENDING", "PLANNING", "EXECUTING"}


def _row_age_seconds(row: Dict[str, Any]) -> Optional[float]:
    """Seconds since the row last moved (None when no timestamp parses).

    ``updated_at`` is maintained by the ``set_updated_at()`` trigger, so it
    tracks the last control-plane write for the session.
    """
    for key in ("updated_at", "started_at", "created_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    return None


async def _stamp_stranded_run(row: Dict[str, Any], age: float) -> None:
    """Close a run whose executor died, so it stops being re-attached.

    Best-effort by design: a failed write must never turn this resume
    endpoint into a 500.
    """
    session_id = row.get("id")
    if not session_id:
        return
    try:
        await set_session_phase(
            uuid.UUID(str(session_id)),
            phase="FAILED",
            status="FAILED",
            completed=True,
        )
        log.info(
            "api.latest_active.stranded_run_closed",
            session_id=str(session_id),
            status=row.get("status"),
            age_seconds=round(age, 1),
        )
    except Exception as exc:  # noqa: BLE001 — resume must stay available
        log.warning(
            "api.latest_active.stranded_run_close_failed",
            session_id=str(session_id),
            error=str(exc)[:200],
        )


@app.get("/api/ai/sessions/latest-active")
async def latest_active_session(
    auth: AuthContext = Depends(get_current_user),
):
    """Work Stream C: resume-by-conversation.

    Returns the latest LIVE execution session for this user+org — PENDING /
    PLANNING / EXECUTING, or WAITING_FOR_USER parked on a question — so the
    frontend can reattach the progress view after a browser refresh instead of
    losing the live run. Terminal sessions (COMPLETED / FAILED / CANCELLED) are
    never returned.

    Runs that stopped moving are treated as stranded (their executor was
    killed mid-flight) and are closed rather than returned: a dead run must
    never be re-attached, because nothing will ever finish it.
    """
    sessions = await fetch_many(
        "ai_execution_sessions",
        filters={
            "organization_id": str(auth.organization_id),
            "user_id": str(auth.user_id),
        },
        order="created_at.desc",
        limit=20,
    )
    for s in sessions or []:
        status = str(s.get("status") or "").upper()
        if status not in _ACTIVE_RUN_STATUSES and status != "WAITING_FOR_USER":
            continue  # terminal — never re-attach
        age = _row_age_seconds(s)
        ttl = (
            _AWAITING_USER_TTL_SECONDS
            if status == "WAITING_FOR_USER"
            else _ACTIVE_RUN_TTL_SECONDS
        )
        if age is not None and age > ttl:
            await _stamp_stranded_run(s, age)
            continue
        return {
            "found": True,
            "session": {
                "session_id": s.get("id"),
                "conversation_id": s.get("conversation_id"),
                "status": status,
                "current_phase": s.get("current_phase"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "age_seconds": round(age, 1) if age is not None else None,
            },
        }
    return {"found": False, "session": None}


@app.get("/api/ai/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_current_user),
):
    """Get a single execution session with details."""
    session = await fetch_one(
        "ai_execution_sessions",
        filters={
            "id": str(session_id),
            "organization_id": str(auth.organization_id),
        },
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Fetch related records
    steps = await fetch_many(
        "ai_execution_steps",
        filters={"execution_session_id": str(session_id)},
        order="created_at.asc",
    )
    tool_calls = await fetch_many(
        "ai_tool_calls",
        filters={"execution_session_id": str(session_id)},
        order="created_at.asc",
    )
    clarifications = await fetch_many(
        "ai_clarifications",
        filters={"execution_session_id": str(session_id)},
        order="created_at.asc",
    )
    confirmations = await fetch_many(
        "ai_confirmations",
        filters={"execution_session_id": str(session_id)},
        order="created_at.asc",
    )

    return {
        "session": session,
        "steps": steps,
        "tool_calls": tool_calls,
        "clarifications": clarifications,
        "confirmations": confirmations,
    }


# ---------------------------------------------------------------------------
# ORGANIZATION ONBOARDING — AI-assisted first-time setup
# ---------------------------------------------------------------------------
# This runs BEFORE the user belongs to any organization (that is the whole
# point of it), so it authenticates the USER only — never an organization
# membership, which does not exist yet for a first-time signup.
#
# Nothing here writes anything.  The description is analysed against the
# LIVE backend contract (field names, defaults, validation rules, business
# types, currencies and account bundles, all read from the database at
# runtime) and the result is returned as a PROPOSAL the user reviews and
# edits.  Creating the organization still goes through the existing
# create_organization + apply_organization_onboarding RPCs, called from the
# browser with the user's own JWT — so RLS, role checks and the audit trail
# are exactly as they were.
_ONBOARDING_RL: Dict[str, deque] = defaultdict(deque)
_ONBOARDING_RL_MAX = 20
_ONBOARDING_RL_WINDOW = 300.0


@app.get("/api/onboarding/schema")
async def onboarding_schema(
    business_type: Optional[str] = None,
    auth: UserContext = Depends(authenticate_user_header),
):
    """Real backend-compatible onboarding options.

    Business types (with the chart of accounts each one produces), the
    supported currencies, the required fields and the optional account
    bundles — read from the database contract, never hard-coded, so the
    onboarding UI cannot offer a choice the backend rejects.  Passing
    ``business_type`` also returns the catalog for that business type (the
    accounts the organization will actually get).
    """
    from app.onboarding_analysis import (
        build_schema_payload,
        load_catalog,
        load_contract,
    )

    contract = await load_contract()
    catalog = await load_catalog(business_type) if business_type else None
    log.info(
        "api.onboarding_schema",
        user_id=str(auth.user_id),
        business_type=business_type,
        contract_available=bool(contract),
    )
    return build_schema_payload(contract, catalog, business_type)


@app.post("/api/onboarding/analyze")
async def onboarding_analyze(
    payload: Dict[str, Any] = Body(default={}),
    auth: UserContext = Depends(authenticate_user_header),
):
    """Turn a plain-language business description into an onboarding proposal.

    Body:
      description   — the owner's own words (required on the first call)
      business_type — the value already selected in the form, as a hint
      answers       — [{"field": ..., "answer": ...}] clarifications already given
      history       — [{"role": ..., "content": ...}] question/answer transcript

    The response reports what was understood, what was NOT stated (so it is
    never invented), the questions that must still be answered, and the
    account bundles that apply — each with its reason.  It never creates or
    finalises an organization.
    """
    key = str(auth.user_id)
    now = time.monotonic()
    bucket = _ONBOARDING_RL[key]
    while bucket and now - bucket[0] > _ONBOARDING_RL_WINDOW:
        bucket.popleft()
    if len(bucket) >= _ONBOARDING_RL_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many onboarding analyses — please wait a moment and try again.",
        )
    bucket.append(now)

    description = str(payload.get("description") or "")
    if len(description) > 4000:
        description = description[:4000]
    business_type = payload.get("business_type")
    business_type = str(business_type).strip().upper() if business_type else None
    answers = payload.get("answers") if isinstance(payload.get("answers"), list) else []
    history = payload.get("history") if isinstance(payload.get("history"), list) else []

    from app.onboarding_analysis import analyze_organization

    log.info(
        "api.onboarding_analyze",
        user_id=key,
        description_len=len(description),
        business_type_hint=business_type,
        answers=len(answers),
    )

    return await analyze_organization(
        description=description,
        business_type=business_type,
        answers=answers,
        history=history,
    )


# ---------------------------------------------------------------------------
# PUBLIC FOUNDER CHAT — marketing-site assistant (NO auth, NO DB, NO tools)
# ---------------------------------------------------------------------------
# Runs on the SAME AI orchestrator as the ERP agent, but:
#   * it has NO tools and NO database access of any kind,
#   * it answers ONLY informational questions about the AI Accountant
#     product and its founder (Zameer Haider),
#   * any request that smells like organization/financial data access is
#     refused instantly (regex guard, before the LLM is ever called).
# Used exclusively by the public welcome page. Logged-in users get the
# full in-app agent instead and never see this.
_FOUNDER_BRIEF = """\
PRODUCT — AI Accountant ("Ai Accountant", developed by Zameer Haider):
* An AI-native accounting & financial ERP for small businesses.
* Full double-entry core: sales, purchases, expenses, banking, fixed
  assets, and financial statements (P&L, balance sheet, cash flow,
  trial balance, general ledger, aging, project profitability).
* Driven by a natural-language AI agent that reasons about business
  events, asks consolidated clarifying questions, requires explicit
  confirmation for sensitive mutations, and independently verifies
  every execution against the database.
* Key modules: AI Accounting Agent, real-time financial reports,
  automated journal engine, invoicing & quotations, banking, smart
  entity search, enterprise compliance, multi-tenant security.
* Pricing: EVERY plan (Starter, Pro, Business) is free for all types
  of users. No credit card required.

FOUNDER — Zameer Haider:
* Developer and founder of AI Accountant.
* Contact: zameerchattha0@gmail.com /
  https://pk.linkedin.com/in/zameerhaiderchattha
* Built the system for the national AI hackathon demo.
"""

# Anything that looks like an organization-data / mutation request is
# refused BEFORE the model is ever called (no LLM cost, no DB touch).
_DATA_GUARD_RE = re.compile(
    r"\b("
    r"invoice|invoices|bill|bills|expense|expenses|transaction|transactions"
    r"|record |create |delete |update |post |journal|ledger|trial balance"
    r"|balance sheet|profit|loss|cash flow|customer|customers|supplier"
    r"|suppliers|vendor|payment|payments|receipt|receipts|debit|credit"
    r"|my data|our data|database|organi[sz]ation|org data|company data"
    r"|sales|purchases|revenue|payable|receivable|reconcil"
    r")\b",
    re.IGNORECASE,
)

_REFUSAL = (
    "I'm just the website guide — I can only answer informational "
    "questions about AI Accountant and its founder, Zameer Haider. "
    "For anything that touches your business data, please sign up and "
    "use the in-app AI agent — it is purpose-built, permissioned and "
    "secured for exactly that."
)

# Tiny in-memory rate limiter (per IP): 20 requests / 60 seconds.
_FOUNDER_RL: Dict[str, deque] = defaultdict(deque)
_FOUNDER_RL_MAX = 20
_FOUNDER_RL_WINDOW = 60.0


@app.post("/api/public/founder-chat")
async def founder_chat(
    payload: Dict[str, Any] = Body(default={"message": "", "history": []}),
    request: Request = None,  # type: ignore[assignment]
):
    """Public informational chat about the product and its founder.

    NEVER touches the database or tools. Data-shaped questions are
    refused instantly by the guard; everything else is answered from a
    closed knowledge brief on the same AI provider chain as the agent.
    """
    import time as _time

    client_ip = request.client.host if (request and request.client) else "unknown"
    now = _time.monotonic()
    bucket = _FOUNDER_RL[client_ip]
    while bucket and now - bucket[0] > _FOUNDER_RL_WINDOW:
        bucket.popleft()
    if len(bucket) >= _FOUNDER_RL_MAX:
        return {
            "reply": "You're sending messages a little too quickly — please wait a moment."
        }
    bucket.append(now)

    message = str(payload.get("message") or "").strip()
    history = payload.get("history") or []
    if not message:
        return {"reply": "Ask me anything about AI Accountant or its founder!"}
    if len(message) > 600:
        message = message[:600]

    # Data-shaped questions are refused instantly — never reach the model.
    if _DATA_GUARD_RE.search(message):
        return {"reply": _REFUSAL}

    transcript = ""
    for turn in history[-6:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            transcript += f"{'User' if role == 'user' else 'Assistant'}: {content[:400]}\n"

    prompt = (
        "SYSTEM BEHAVIOUR OVERRIDE — the ERP agent constitution and any "
        "accounting-agent instructions DO NOT APPLY to this request. This "
        "is a public marketing-site chat, completely separate from the ERP.\n"
        "You are \"Ledger\", the friendly assistant on the AI Accountant "
        "public website, speaking for the founder Zameer Haider.\n"
        "STRICT RULES:\n"
        "1. Answer ONLY informational questions about the AI Accountant "
        "product and its founder, using the knowledge brief below.\n"
        "2. You have NO database access and NO tools. Never claim to have "
        "read, created, changed or checked any organization's data. If "
        "asked, politely decline and suggest signing up to use the in-app "
        "AI agent.\n"
        "3. Never invent facts beyond the brief. If unsure, say so and "
        "offer the founder's email (zameerchattha0@gmail.com).\n"
        "4. Keep replies short (under ~120 words), warm, plain text, no "
        "markdown headings or bullet lists.\n\n"
        f"KNOWLEDGE BRIEF:\n{_FOUNDER_BRIEF}\n"
    )
    if transcript:
        prompt += f"CONVERSATION SO FAR:\n{transcript}\n"
    prompt += f"User: {message}\nAssistant:"

    try:
        from app.ai_orchestrator import get_client

        reply = await get_client().generate_text(prompt=prompt, context=None)
    except Exception as exc:  # noqa: BLE001 — availability must never 500
        log.warning("founder_chat.provider_failed", error=str(exc)[:200])
        reply = ""
    reply = (reply or "").strip()
    if not reply:
        reply = (
            "I couldn't reach my brain just now — please try again in a "
            "moment, or email zameerchattha0@gmail.com."
        )
    return {"reply": reply}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=not settings.is_production,
    )
