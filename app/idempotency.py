"""Idempotency for financial mutations (migration 075).

An explicit, persistent, organization-scoped request key replaces the previous
amount-based duplicate guard, which covered ONE tool slug, ONE execution
session, nothing across restarts — and treated two legitimately identical
transactions as one, i.e. it could suppress a real financial record.

Semantics:
  * the first attempt CLAIMS the key and performs the mutation;
  * an identical retry (same key, same request hash) REPLAYS the stored result
    instead of writing again;
  * the same key with a DIFFERENT payload is refused (that is not a retry);
  * a FAILED attempt may be retried; an abandoned IN_PROGRESS claim is
    reclaimable by the database after a stale window, so a crash cannot wedge
    a key forever.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Optional

import structlog

from app.database import call_rpc

log = structlog.get_logger(__name__)

# The EXACTLY-ONCE CLAIM set (app/tool_router.py): a call to one of these
# slugs claims a key before it runs, so a retry replays instead of writing
# twice.  Read-only tools never claim.
#
# NOTE — this set carries TOOL SLUGS.  For the wider question "which TOOLS
# write the books?" use FINANCIAL_WRITE_TOOLS below: an INTENT is not a tool
# slug, and conflating the two made a predicate answer the wrong question
# (see INTENT_ONLY_NAMES).
FINANCIAL_MUTATION_TOOLS = frozenset({
    "create_invoice",
    "create_credit_note",
    "create_purchase_bill",
    "create_purchase_return",
    "create_quotation",
    "convert_quotation",
    "record_cash_sale",
    "record_credit_sale",
    "record_sale",
    "record_expense",
    "record_customer_receipt",
    "record_supplier_payment",
    "record_expense_payment",
    "record_bank_transfer",
    "prepare_journal",
    "post_journal",
    "register_fixed_asset",
    "dispose_fixed_asset",
    "record_asset_depreciation",
})

# ---------------------------------------------------------------------------
# Intent vocabulary vs tool vocabulary — never conflated
# ---------------------------------------------------------------------------
# An INTENT (app/planner.py) is what the USER asked for; a TOOL SLUG
# (app/tools/__init__.py) is the callable that performs it.  They are usually
# spelled the same, and exactly where they are not, a predicate built on one
# vocabulary silently answers the wrong question for the other:
#
#   intent  record_expense          -> tool create_expense
#   intent  record_sale             -> tool create_invoice
#   intent  record_credit_sale      -> tool create_invoice
#   (the mapping lives in app/tool_selector.py: _INTENT_TOOLS)
#
# These three names are INTENTS and no registered tool carries them, so they
# can never appear as a tool result name.
INTENT_ONLY_NAMES = frozenset({
    "record_expense",
    "record_sale",
    "record_credit_sale",
})

# TOOL SLUGS that write the books: the predicate vocabulary for
# "did this plan perform a financial mutation?" and "did the primary operation
# run and succeed?" (app/agent.py).
#
# Derived from the claim set above, with the two vocabulary errors corrected:
#   * the intent-only names are removed (no tool result can ever match them —
#     `record_expense` in the claim set made every RECORDED expense look like a
#     missing primary mutation, which PHASE 7/8 turned into FAILED);
#   * `create_expense` is ADDED (it writes the expense AND posts its journal —
#     it was absent, so an expense could never satisfy a financial predicate).
#
# This is NOT the claim set: `create_expense` deliberately stays out of
# FINANCIAL_MUTATION_TOOLS, because a derived claim key is
# ``session:<id>:<slug>`` — putting it in that set would make a SECOND,
# genuinely different expense in the same run collide on the key and be refused
# as "key reused with different parameters".  Widening exactly-once protection
# to expenses therefore needs its own change (key discrimination), tracked
# separately as it can suppress real records if done naively.
FINANCIAL_WRITE_TOOLS = frozenset(
    (FINANCIAL_MUTATION_TOOLS - INTENT_ONLY_NAMES) | {"create_expense"}
)


def request_hash(operation: str, arguments: Dict[str, Any]) -> str:
    """Stable hash of the request payload.

    Canonicalised (sorted keys, str fallback for UUID/Decimal) so an identical
    retry hashes identically while a changed amount, party or date does not.
    """
    canonical = json.dumps(
        {"operation": operation, "arguments": arguments},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_key(
    *,
    explicit: Optional[str],
    session_id: Optional[uuid.UUID],
    operation: str,
) -> Optional[str]:
    """Explicit key wins; otherwise derive a key STABLE across retries.

    The derived key is (run, tool) — deliberately NOT the payload — so a retry
    of the same operation in the same run collides and replays, while a
    genuinely different second operation in that run does not.  The payload is
    compared separately via ``request_hash``, which is what turns a
    same-key-different-payload call into a refusal rather than a replay.
    """
    if explicit:
        return str(explicit)[:200]
    if session_id is None:
        return None
    return f"session:{session_id}:{operation}"


async def claim(
    *,
    organization_id: uuid.UUID,
    operation: str,
    key: str,
    arguments: Dict[str, Any],
    user_id: Optional[uuid.UUID] = None,
    stale_after: str = "10 minutes",
) -> Dict[str, Any]:
    """Claim the key. Returns {'state': 'claimed'|'replay'|'in_progress', ...}."""
    return await call_rpc(
        "claim_financial_operation",
        params={
            "p_organization_id": str(organization_id),
            "p_operation": operation,
            "p_idempotency_key": key,
            "p_request_hash": request_hash(operation, arguments),
            "p_user_id": str(user_id) if user_id else None,
            "p_stale_after": stale_after,
        },
    )


async def complete(
    op_id: uuid.UUID,
    *,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Record the terminal outcome so a later retry can replay it."""
    await call_rpc(
        "complete_financial_operation",
        params={
            "p_id": str(op_id),
            "p_status": status,
            "p_result": result,
            "p_error": error,
        },
    )


def is_key_conflict(exc: Exception) -> bool:
    """True when the database refused a reused key with a different payload."""
    text = str(exc)
    return "40001" in text or "already used for" in text


async def safe_complete(
    op_id: Optional[uuid.UUID],
    *,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Record the outcome without ever failing the mutation itself.

    The financial write has ALREADY happened by the time this runs, so a
    failure to update the ledger must not surface as a tool error. It is
    logged, and the row stays IN_PROGRESS until the stale window lets a retry
    reclaim it.
    """
    if op_id is None:
        return
    payload: Optional[Dict[str, Any]] = None
    if result is not None:
        try:
            # jsonb cannot carry UUID/Decimal; round-trip through str.
            payload = json.loads(json.dumps(result, default=str))
        except (TypeError, ValueError):
            payload = None
    try:
        await complete(
            op_id,
            status="FAILED" if error else "COMPLETED",
            result=payload,
            error=(str(error)[:2000] if error else None),
        )
    except Exception as exc:  # noqa: BLE001 - never fail the mutation here
        log.warning("idempotency.complete_failed", error=str(exc)[:200])