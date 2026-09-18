"""
ERP AI Agent — Tool Router
============================
Safely routes Gemini tool calls to backend implementations.

Flow:  Gemini → Tool Call → Registry → Permission Check → Organisation
Scope → Parameter Validation → Risk Check → Validator → Service →
Repository → Supabase

Never executes an arbitrary tool name received from Gemini.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Any, Dict, Optional

import structlog

from app.auth import AuthContext
from app.database import fetch_many
from app.date_parser import parse_transaction_date
import app.error_normalizer as error_norm
from app import idempotency as idem
from app.error_normalizer import normalize_error
from app.models.schemas import ToolCall, ToolResult
from app.permissions import authorize_tool
from app.tools import get_handler, list_tools
from app.validator import validate_operation

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tool-definition cache (Work Stream A4).  ai.tools + ai.tool_parameters are
# read-only metadata: cache the built declarations in-process for 5 minutes.
# ---------------------------------------------------------------------------
_TOOL_DEFS_TTL_SECONDS = 300.0
_TOOL_DEFS_CACHE = None  # (loaded_at: float, tools: list[dict]) | None

# ---------------------------------------------------------------------------
# Work Stream A - transaction-date protocol.  Mutation tools whose native
# date parameter the model must supply.  When the model omits it, the
# router maps a supplied ``transaction_date`` through the deterministic
# parser onto the native parameter, or defaults to TODAY and flags the
# result with ``date_defaulted: true`` so the response can state the
# assumed date honestly (never a silent guess).
# ---------------------------------------------------------------------------
_TXN_DATE_PARAM: Dict[str, str] = {
    "create_invoice": "invoice_date",
    "create_purchase_bill": "bill_date",
    "create_expense": "expense_date",
    "create_quotation": "quotation_date",
    "create_credit_note": "credit_note_date",
    "create_purchase_return": "return_date",
    "record_customer_receipt": "receipt_date",
    "record_supplier_payment": "payment_date",
    "record_expense_payment": "payment_date",
    "record_bank_transfer": "transfer_date",
    "record_cash_sale": "transaction_date",
    "register_fixed_asset": "transaction_date",
    "dispose_fixed_asset": "transaction_date",
    "record_asset_depreciation": "transaction_date",
}

_PERIOD_GUIDANCE = (
    "reopen the period in Settings or choose a date within an open "
    "accounting period"
)


def _is_closed_period_error(message: str) -> bool:
    """Detect a closed/missing accounting-period refusal from the DB."""
    low = (message or "").lower()
    return "period" in low and (
        "closed" in low or "no open" in low or "not open" in low
    )


def invalidate_tool_definition_cache() -> None:
    """Explicitly drop the cached Gemini tool definitions.

    Call after seeding/updating ``ai.tools`` / ``ai.tool_parameters`` so the
    next request rebuilds the declarations from the database.
    """
    _invalidate_tool_definition_cache_internal()


def _invalidate_tool_definition_cache_internal() -> None:
    """Test/internal hook: clears the cache regardless of TTL."""
    global _TOOL_DEFS_CACHE
    _TOOL_DEFS_CACHE = None



async def route_tool_call(
    tool_call: ToolCall,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: Optional[uuid.UUID] = None,
    auth: Optional[AuthContext] = None,
) -> ToolResult:
    """Route a tool call through security and validation layers.

    1. Verify the tool is registered
    2. Check permissions (via ai.permissions)
    3. Validate parameters
    4. Run business validator for mutations
    5. Execute the tool handler
    """
    slug = tool_call.tool_name
    arguments = tool_call.arguments or {}

    # 1. Check registry
    entry = get_handler(slug)
    if not entry:
        log.warning("tool_router.unknown_tool", tool=slug)
        return ToolResult(
            tool_name=slug,
            success=False,
            error=f"Unknown tool: {slug}",
        )

    handler = entry["handler"]
    read_only = entry["read_only"]

    # 2. Permission check (via ai.permissions)
    if auth is None:
        # SECURITY: a MUTATION must never execute without an authorization
        # context.  The AI worker used to pass auth=None, which skipped BOTH
        # the permission check and the parameter validation below, so a
        # queued job could create financial documents with no authorization
        # at all.  Fail closed instead.
        if not read_only:
            log.error("tool_router.missing_auth_for_mutation", tool=slug)
            return ToolResult(
                tool_name=slug,
                success=False,
                error=(
                    "Authorization context missing: mutations cannot run "
                    "without an authenticated user."
                ),
            )
        log.warning("tool_router.read_without_auth", tool=slug)
    else:
        permitted = await authorize_tool(slug, auth=auth)
        if not permitted:
            log.warning(
                "tool_router.permission_denied",
                tool=slug,
                user=str(user_id),
                role=auth.role_code,
            )
            return ToolResult(
                tool_name=slug,
                success=False,
                error=f"Permission denied: your role ({auth.role_code}) cannot use {slug}.",
            )

    # 3. Parameter validation for mutations
    if not read_only:
        validation = await validate_operation(
            organization_id=organization_id,
            operation=slug,
            data=arguments,
        )
        if not validation.valid:
            log.warning(
                "tool_router.validation_failed",
                tool=slug,
                errors=validation.errors,
            )
            return ToolResult(
                tool_name=slug,
                success=False,
                error=f"Validation failed: {'; '.join(validation.errors)}",
            )

    # 3b. EXACTLY-ONCE PROTECTION for financial mutations (migration 075).
    #
    # Replaces the previous amount-based guard, which covered ONE tool slug in
    # ONE session with no persistence and treated two legitimately identical
    # transactions as one — i.e. it could suppress a real financial record.
    #
    #   replay      -> identical retry: return the ORIGINAL stored result
    #   in_progress -> an identical request is already running
    #   40001       -> the key was reused with DIFFERENT parameters
    op_id: Optional[uuid.UUID] = None
    if not read_only and slug in idem.FINANCIAL_MUTATION_TOOLS:
        arguments = dict(arguments)
        explicit_key = arguments.pop("idempotency_key", None)
        key = idem.derive_key(
            explicit=explicit_key, session_id=session_id, operation=slug
        )
        if key:
            try:
                outcome = await idem.claim(
                    organization_id=organization_id,
                    operation=slug,
                    key=key,
                    arguments=arguments,
                    user_id=user_id,
                )
                state = (outcome or {}).get("state")
                if state == "replay":
                    log.info("tool_router.idempotent_replay", tool=slug)
                    replayed = outcome.get("result") or {}
                    return ToolResult(
                        tool_name=slug,
                        success=True,
                        data={**replayed, "replayed": True},
                    )
                if state == "in_progress":
                    log.warning("tool_router.idempotent_in_progress", tool=slug)
                    return ToolResult(
                        tool_name=slug,
                        success=False,
                        error=(
                            "An identical request is already being processed. "
                            "Not repeating it, to avoid a duplicate document."
                        ),
                    )
                op_id = outcome.get("id")
            except Exception as exc:  # noqa: BLE001 — never block on guard failure
                if idem.is_key_conflict(exc):
                    log.warning("tool_router.idempotency_key_conflict", tool=slug)
                    return ToolResult(
                        tool_name=slug,
                        success=False,
                        error=(
                            "This request key was already used for a different "
                            "request. Use a new key for a new operation."
                        ),
                    )
                log.warning(
                    "tool_router.idempotency_claim_failed", error=str(exc)[:200]
                )

    # 3b. Work Stream A - transaction-date protocol: map a resolved
    #     transaction_date onto the tool's native date parameter; when the
    #     model supplied none, default to TODAY and flag the result so the
    #     response states the assumed date honestly (never a silent guess).
    date_param = None if read_only else _TXN_DATE_PARAM.get(slug)
    date_defaulted = False
    if date_param:
        arguments = dict(arguments)
        supplied = arguments.get(date_param) or arguments.get("transaction_date")
        parsed = parse_transaction_date(str(supplied)) if supplied else None
        if parsed and parsed.ok:
            arguments[date_param] = parsed.iso_date
        else:
            arguments[date_param] = date.today().isoformat()
            date_defaulted = True

    # 4. Execute
    try:
        result = await handler(organization_id, **arguments)
        if date_defaulted and result.success:
            if isinstance(result.data, dict):
                result.data["date_defaulted"] = True
                result.data["transaction_date"] = arguments[date_param]
            else:
                result.data = {
                    "date_defaulted": True,
                    "transaction_date": arguments[date_param],
                }
        # Record the outcome LAST, so the stored result is exactly what the
        # caller saw — including the honest date disclosure above.  A replay
        # must not report a different payload from the original attempt.
        if op_id is not None:
            await idem.safe_complete(
                op_id,
                result=(result.data if result.success else None),
                error=(None if result.success else result.error),
            )
        log.info(
            "tool_router.executed",
            tool=slug,
            success=result.success,
            session_id=str(session_id) if session_id else None,
        )
        return result
    except ValueError as exc:
        log.warning("tool_router.value_error", tool=slug, error=str(exc))
        normalized = normalize_error(exc, operation=slug)
        message = str(exc)
        if _is_closed_period_error(message):
            message = f"{message} Guidance: {_PERIOD_GUIDANCE}."
        return ToolResult(
            tool_name=slug,
            success=False,
            error=message,
            error_category=normalized["category"],
            error_details=normalized,
        )
    except Exception as exc:
        log.error("tool_router.unexpected_error", tool=slug, error=str(exc))
        normalized = normalize_error(exc, operation=slug)
        # Model-facing message: for recoverable categories give the model an
        # actionable instruction (e.g. reuse an existing record); genuine
        # infrastructure failures keep the generic system-failure text.
        if normalized["category"] == error_norm.DUPLICATE_RECORD:
            model_message = error_norm.build_duplicate_guidance(normalized.get("entity"))
        elif normalized["category"] == error_norm.INFRASTRUCTURE_ERROR:
            model_message = "An unexpected error occurred while executing this operation."
        elif normalized.get("requires_user_input"):
            model_message = (
                f"{normalized['reason']}. This value must be supplied by the "
                "user — do not guess or use a default."
            )
        else:
            model_message = "An unexpected error occurred while executing this operation."
        if _is_closed_period_error(str(exc)):
            model_message = (
                f"{str(exc)} Guidance: {_PERIOD_GUIDANCE}."
            )
        return ToolResult(
            tool_name=slug,
            success=False,
            error=model_message,
            error_category=normalized["category"],
            error_details=normalized,
        )


async def get_gemini_tool_definitions() -> list[Dict[str, Any]]:
    """Return tool definitions formatted for Gemini's function-calling API.

    Reads real parameter definitions from ``ai.tool_parameters`` so that
    Gemini receives accurate JSON-Schema parameter contracts instead of
    a bare ``organization_id`` placeholder.

    Work Stream A4: definitions are READ-ONLY metadata, so they are cached
    in-process with a 5-minute TTL and refreshed explicitly via
    :func:`invalidate_tool_definition_cache` (the DB is hit once across
    requests instead of once per request).
    """
    global _TOOL_DEFS_CACHE
    now = time.monotonic()
    if _TOOL_DEFS_CACHE is not None:
        loaded_at, tools_cached = _TOOL_DEFS_CACHE
        if now - loaded_at < _TOOL_DEFS_TTL_SECONDS:
            return tools_cached

    # Bulk-load parameter definitions for all IMPLEMENTED tools (cached
    # per-process via the Supabase query; ~78 rows for 36 tools).
    all_tool_rows = await fetch_many(
        "ai_tools",
        filters={"status": "IMPLEMENTED"},
        select="id,slug",
        limit=200,
    )
    slug_to_id = {r["slug"]: r["id"] for r in all_tool_rows}

    tool_params: Dict[str, list] = {}  # tool_id -> [param rows]
    if slug_to_id:
        tool_ids = list(slug_to_id.values())
        # Single query using Supabase .in_() filter
        from app.database import _execute, get_service_client

        param_rows = (
            (
                await _execute(
                    get_service_client()
                    .schema("ai")
                    .table("tool_parameters")
                    .select("tool_id,parameter_name,description,data_type,required,position")
                    .in_("tool_id", tool_ids)
                    .limit(500)
                )
            ).data
            or []
        )
        for row in param_rows:
            tool_params.setdefault(row["tool_id"], []).append(row)

    tools: list[Dict[str, Any]] = []
    for slug in list_tools():
        entry = get_handler(slug)
        if not entry:
            continue

        tool_id = slug_to_id.get(slug)
        params = tool_params.get(tool_id, []) if tool_id else []

        # Sort by position (NULLs last)
        params.sort(key=lambda p: p.get("position") or 999)

        properties: Dict[str, Any] = {}
        required_fields: list[str] = []

        for p in params:
            pname = p["parameter_name"]
            # organization_id is auto-injected by the executor — never
            # expose it to Gemini.
            if pname == "organization_id":
                continue
            properties[pname] = {
                "type": p.get("data_type", "string"),
                "description": p.get("description", ""),
            }
            if p.get("required"):
                required_fields.append(pname)

        declaration: Dict[str, Any] = {
            "name": slug,
            "description": entry.get("description", slug),
        }
        # Only include a parameters schema when the tool actually has
        # parameters — Gemini rejects an OBJECT schema with empty properties.
        if properties:
            declaration["parameters"] = {
                "type": "object",
                "properties": properties,
                "required": required_fields,
            }
        tools.append(declaration)

    _TOOL_DEFS_CACHE = (time.monotonic(), tools)
    return tools
