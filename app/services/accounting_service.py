"""
Accounting Service — orchestration of the accounting engine and journal repository.

This is the authoritative service for all accounting operations.
The accounting engine is deterministic — it does NOT depend on Gemini
to calculate debit/credit.  Gemini may *propose* accounts; this service
resolves and validates them.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import journal_repository as j_repo
from app.repositories import account_repository as a_repo
from app.repositories import organization_repository as org_repo

log = structlog.get_logger(__name__)


async def prepare_journal(
    *,
    organization_id: uuid.UUID,
    transaction_date: str,
    description: str,
    lines: List[Dict[str, Any]],
    source_type: Optional[str] = None,
    source_id: Optional[uuid.UUID] = None,
    currency_code: str = "PKR",
) -> Dict[str, Any]:
    """Prepare a DRAFT journal entry with lines.

    Each line must have: account_id, description, debit, credit.
    Optional: customer_id, supplier_id, project_id.

    Validates balance (total debit == total credit) before returning.
    """
    # Validate lines
    total_debit = sum(float(l.get("debit", 0)) for l in lines)
    total_credit = sum(float(l.get("credit", 0)) for l in lines)

    if len(lines) < 2:
        raise ValueError("Journal entry requires at least 2 lines")
    if abs(total_debit - total_credit) > 0.01:
        raise ValueError(
            f"Journal is not balanced: debit={total_debit}, credit={total_credit}"
        )
    if total_debit <= 0:
        raise ValueError("Journal amounts must be positive")

    # Create journal entry
    entry = await j_repo.create_journal_entry(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        source_type=source_type,
        source_id=source_id,
        currency_code=currency_code,
    )

    # Add lines
    created_lines = await j_repo.add_journal_lines(
        entry_id=uuid.UUID(entry["id"]),
        organization_id=organization_id,
        lines=lines,
    )

    log.info(
        "accounting.journal_prepared",
        entry_id=entry["id"],
        total_debit=total_debit,
        line_count=len(lines),
    )

    return {
        "entry": entry,
        "lines": created_lines,
        "total_debit": total_debit,
        "total_credit": total_credit,
    }


async def validate_journal(*, entry_id: uuid.UUID) -> Dict[str, Any]:
    """Validate a DRAFT journal entry (transitions to VALIDATED)."""
    return await j_repo.validate_journal_entry(entry_id=entry_id)


async def post_journal(*, entry_id: uuid.UUID) -> Dict[str, Any]:
    """Post a VALIDATED journal entry (transitions to POSTED)."""
    return await j_repo.post_journal_entry(entry_id=entry_id)


async def reverse_journal(
    *, entry_id: uuid.UUID, reason: Optional[str] = None
) -> Dict[str, Any]:
    """Reverse a POSTED journal entry."""
    return await j_repo.reverse_journal_entry(entry_id=entry_id, reason=reason)


async def get_entry(
    organization_id: uuid.UUID, *, entry_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await j_repo.get_journal_entry(organization_id, entry_id=entry_id)


async def get_lines(
    *, entry_id: uuid.UUID, organization_id: Optional[uuid.UUID] = None
) -> List[Dict[str, Any]]:
    return await j_repo.get_journal_lines(
        entry_id=entry_id, organization_id=organization_id
    )


async def resolve_account(
    organization_id: uuid.UUID,
    *,
    account_type: Optional[str] = None,
    account_code: Optional[str] = None,
    account_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve an account by code or name within the organisation."""
    if account_code:
        return await a_repo.get_account_by_code(organization_id, code=account_code)
    if account_name:
        results = await a_repo.search_accounts(organization_id, query=account_name, limit=5)
        # Return exact match if found
        for r in results:
            if r.get("name", "").lower().strip() == account_name.lower().strip():
                return r
        return results[0] if results else None
    return None
