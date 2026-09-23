"""
Journal Repository — Supabase data access for journal entries and lines.

This is the core accounting data-access layer.  All financial truth lives in
``journal_entries`` + ``journal_lines``.  Database triggers enforce:
  * Balanced entries (total_debit = total_credit)
  * Open accounting periods for posting
  * Immutability of POSTED / REVERSED / VOIDED entries
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.database import (
    call_rpc,
    fetch_many,
    fetch_one,
    insert_many,
    insert_one,
    update_one,
)


async def create_journal_entry(
    *,
    organization_id: uuid.UUID,
    transaction_date: str,
    description: str,
    source_type: Optional[str] = None,
    source_id: Optional[uuid.UUID] = None,
    currency_code: str = "PKR",
    accounting_period_id: Optional[uuid.UUID] = None,
    created_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Create a DRAFT journal entry. journal_number auto-assigned by trigger."""
    # Resolve accounting period if not provided
    if not accounting_period_id:
        period = await _resolve_period(organization_id, transaction_date)
        if period:
            accounting_period_id = uuid.UUID(period["id"])

    data: Dict[str, Any] = {
        "organization_id": str(organization_id),
        "transaction_date": transaction_date,
        "description": description,
        "source_type": source_type,
        "source_id": str(source_id) if source_id else None,
        "currency_code": currency_code,
        "accounting_period_id": str(accounting_period_id) if accounting_period_id else None,
        "status": "DRAFT",
        "created_by": str(created_by) if created_by else None,
    }
    return await insert_one("journal_entries", data=data)


async def add_journal_lines(
    *, entry_id: uuid.UUID, organization_id: uuid.UUID, lines: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Add lines to a journal entry.

    Each line dict must contain: account_id, description, debit, credit.
    Optional: customer_id, supplier_id, project_id, tax_rate_id.
    line_number is set sequentially.
    """
    prepared = []
    for i, line in enumerate(lines, start=1):
        prepared.append({
            "entry_id": str(entry_id),
            "organization_id": str(organization_id),
            "line_number": i,
            "account_id": str(line["account_id"]),
            "description": line.get("description", ""),
            "debit": float(line.get("debit", 0)),
            "credit": float(line.get("credit", 0)),
            "customer_id": str(line["customer_id"]) if line.get("customer_id") else None,
            "supplier_id": str(line["supplier_id"]) if line.get("supplier_id") else None,
            "project_id": str(line["project_id"]) if line.get("project_id") else None,
            "tax_rate_id": str(line["tax_rate_id"]) if line.get("tax_rate_id") else None,
        })
    return await insert_many("journal_lines", data=prepared)


async def get_journal_entry(
    organization_id: uuid.UUID, *, entry_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "journal_entries",
        filters={
            "id": str(entry_id),
            "organization_id": str(organization_id),
        },
    )


async def get_journal_lines(
    *, entry_id: uuid.UUID, organization_id: Optional[uuid.UUID] = None
) -> List[Dict[str, Any]]:
    """Return the lines of a journal entry, oldest first.

    DEFENCE IN DEPTH: ``journal_lines`` carries ``organization_id``, so pass it
    to add a second filter.  The entry id is normally already org-verified by
    :func:`get_journal_entry`, but without this a foreign entry id would return
    another tenant's lines.
    """
    filters: Dict[str, Any] = {"entry_id": str(entry_id)}
    if organization_id is not None:
        filters["organization_id"] = str(organization_id)
    return await fetch_many(
        "journal_lines",
        filters=filters,
        order="line_number.asc",
    )


async def validate_journal_entry(*, entry_id: uuid.UUID) -> Dict[str, Any]:
    """Call the database function validate_journal_entry(p_entry_id uuid).

    NOTE: the PostgreSQL parameter is ``p_entry_id`` (migration 016) — the
    RPC body keys MUST match the database argument names exactly.
    """
    result = await call_rpc(
        "validate_journal_entry", params={"p_entry_id": str(entry_id)}
    )
    return {"success": True, "result": result}


async def post_journal_entry(
    *, entry_id: uuid.UUID, posted_by: Optional[uuid.UUID] = None
) -> Dict[str, Any]:
    """Call the database function post_journal_entry(p_entry_id, p_posted_by)."""
    result = await call_rpc(
        "post_journal_entry",
        params={
            "p_entry_id": str(entry_id),
            "p_posted_by": str(posted_by) if posted_by else None,
        },
    )
    return {"success": True, "result": result}


async def reverse_journal_entry(
    *,
    entry_id: uuid.UUID,
    reason: Optional[str] = None,
    reversal_date: Optional[str] = None,
    reversed_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Call the database function reverse_journal_entry(p_entry_id,
    p_reversal_date, p_reason, p_reversed_by)."""
    result = await call_rpc(
        "reverse_journal_entry",
        params={
            "p_entry_id": str(entry_id),
            "p_reversal_date": reversal_date,
            "p_reason": reason or "Reversal",
            "p_reversed_by": str(reversed_by) if reversed_by else None,
        },
    )
    return {"success": True, "result": result}


# ---- Private helpers ------------------------------------------------------

async def _resolve_period(
    organization_id: uuid.UUID, transaction_date: str
) -> Optional[Dict[str, Any]]:
    """Resolve the accounting period for a given date."""
    try:
        result = await call_rpc(
            "get_period_for_date",
            params={"p_date": transaction_date, "p_org_id": str(organization_id)},
        )
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
    except Exception:
        pass
    return None
