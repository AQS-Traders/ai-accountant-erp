"""
Account Repository — Supabase data access for the chart of accounts.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from app.database import fetch_many, fetch_one, insert_one, search_ilike


async def search_accounts(
    organization_id: uuid.UUID, *, query: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """Search accounts by name or code."""
    # Try both name and code — merge results
    by_name = await search_ilike(
        "accounts",
        column="name",
        value=query,
        organization_id=organization_id,
        select="id,code,name,account_type,normal_balance,is_active,is_control_account",
        limit=limit,
    )
    by_code = await search_ilike(
        "accounts",
        column="code",
        value=query,
        organization_id=organization_id,
        select="id,code,name,account_type,normal_balance,is_active,is_control_account",
        limit=limit,
    )
    # De-duplicate by id
    seen = set()
    merged: List[Dict[str, Any]] = []
    for row in by_name + by_code:
        if row["id"] not in seen:
            seen.add(row["id"])
            merged.append(row)
    return merged[:limit]


async def get_account(
    organization_id: uuid.UUID, *, account_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "accounts",
        filters={
            "id": str(account_id),
            "organization_id": str(organization_id),
        },
    )


async def get_account_by_code(
    organization_id: uuid.UUID, *, code: str
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "accounts",
        filters={
            "code": code,
            "organization_id": str(organization_id),
        },
    )


async def get_chart_of_accounts(
    organization_id: uuid.UUID,
    *,
    account_type: Optional[str] = None,
    is_active: bool = True,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch the full (or filtered) chart of accounts."""
    filters: Dict[str, Any] = {
        "organization_id": str(organization_id),
        "is_active": is_active,
    }
    if account_type:
        filters["account_type"] = account_type
    return await fetch_many(
        "accounts",
        filters=filters,
        select="id,code,name,account_type,normal_balance,parent_account_id,is_control_account,is_active",
        order="code.asc",
        limit=limit,
    )


async def create_account(
    *,
    organization_id: uuid.UUID,
    code: str,
    name: str,
    account_type: str,
    normal_balance: str,
    parent_account_id: Optional[uuid.UUID] = None,
    is_control_account: bool = False,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    return await insert_one(
        "accounts",
        data={
            "organization_id": str(organization_id),
            "code": code,
            "name": name,
            "account_type": account_type,
            "normal_balance": normal_balance,
            "parent_account_id": str(parent_account_id) if parent_account_id else None,
            "is_control_account": is_control_account,
            "is_active": True,
            "is_system": False,
            "description": description,
        },
    )


async def get_grouping_account_ids(organization_id: uuid.UUID) -> set:
    """Ids of accounts that are the PARENT of another active account.

    A hierarchical chart of accounts uses such accounts as headings
    ("Property, Plant & Equipment", "Cost of Sales").  They must never
    carry a posting, so they must never be resolved as a default account.
    """
    rows = await fetch_many(
        "accounts",
        filters={"organization_id": str(organization_id), "is_active": True},
        select="parent_account_id",
        limit=1000,
    )
    return {
        str(row["parent_account_id"])
        for row in rows
        if row.get("parent_account_id")
    }


async def next_available_code(
    organization_id: uuid.UUID, requested_code: str, *, max_probes: int = 200
) -> str:
    """Return `requested_code` if free, else the next free numeric code.

    Respects the organisation's actual code structure: the requested code's
    digit-length is preserved while probing forward (6150 -> 6151 -> ...),
    so a suggested account lands in the same numbering series instead of
    colliding with the unique (organization_id, code) constraint.  When the
    whole series is exhausted the numeric part grows by one digit.

    Non-numeric codes are returned unchanged when free; when taken, a
    numeric suffix is appended.
    """
    code = (requested_code or "").strip()
    if not code:
        return code

    if not await get_account_by_code(organization_id, code=code):
        return code  # requested code is free — use it verbatim

    if code.isdigit():
        base = int(code)
        length = len(code)
        for delta in range(1, max_probes + 1):
            candidate = str(base + delta)
            if len(candidate) > length:
                # keep the same width while possible; allow growth once the
                # series is exhausted (e.g. 9999 -> 10000)
                pass
            if not await get_account_by_code(organization_id, code=candidate):
                return candidate
        raise ValueError(
            f"No free account code found near '{code}' after {max_probes} probes"
        )

    # Non-numeric code: append/increment a numeric suffix.
    match = re.match(r"^(.*?)(\d+)$", code)
    prefix, suffix = (match.group(1), match.group(2)) if match else (code, "0")
    base = int(suffix)
    for delta in range(1, max_probes + 1):
        candidate = f"{prefix}{base + delta}"
        if not await get_account_by_code(organization_id, code=candidate):
            return candidate
    raise ValueError(
        f"No free account code found near '{code}' after {max_probes} probes"
    )
