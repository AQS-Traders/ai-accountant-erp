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

# Tools that write financial state.  Read-only tools never claim a key.
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