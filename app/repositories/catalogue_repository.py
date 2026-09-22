"""Catalogue data access shared by the product and service repositories.

Both catalogues answer the same questions — list with a status filter, patch
one row, flip ``is_active`` — so the queries live here instead of being written
twice.  Every call carries ``organization_id`` (the second guard on updates and
deletes), so a foreign row id can never be touched.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence

from app.database import delete_one, fetch_many, search_ilike, update_one
from app.services.catalogue_rules import normalise_status


async def list_rows(
    table: str,
    organization_id: uuid.UUID,
    *,
    columns: Sequence[str],
    query: str = "",
    status: str = "ALL",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """List the catalogue: an ilike search when a term is given, else everything.

    A blank term lists the whole catalogue (the page's default view); a term
    reuses the repository's trigram-backed ``search_ilike`` so the behaviour
    matches the agent's ``search_product`` / ``search_service`` tools.
    """
    select = ",".join(columns)
    term = (query or "").strip()
    if term:
        rows = await search_ilike(
            table,
            column="name",
            value=term,
            organization_id=organization_id,
            select=select,
            limit=limit,
        )
    else:
        rows = await fetch_many(
            table,
            filters={"organization_id": str(organization_id)},
            select=select,
            order="name.asc",
            limit=limit,
        )

    wanted = normalise_status(status)
    if wanted == "ACTIVE":
        return [r for r in rows if r.get("is_active", True)]
    if wanted == "INACTIVE":
        return [r for r in rows if not r.get("is_active", True)]
    return list(rows)


async def patch_row(
    table: str,
    organization_id: uuid.UUID,
    *,
    row_id: uuid.UUID,
    changes: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Update the given columns only. ``{}`` is refused by the caller."""
    return await update_one(
        table, row_id=row_id, data=changes, organization_id=organization_id
    )


async def remove_row(
    table: str,
    organization_id: uuid.UUID,
    *,
    row_id: uuid.UUID,
) -> bool:
    """Hard delete, org-scoped. The caller has already proven it is unreferenced."""
    return await delete_one(table, row_id=row_id, organization_id=organization_id)
