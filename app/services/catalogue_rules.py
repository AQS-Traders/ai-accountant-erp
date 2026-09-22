"""Catalogue usage + status rules shared by products and services.

The database contract is authoritative (live FK introspection,
2026-09-23):

    products.id  <- invoice_items, credit_note_items, purchase_bill_items,
                    purchase_return_items, quotation_items   (all NO ACTION)
    services.id  <- invoice_items, credit_note_items, quotation_items
                                                             (all NO ACTION)

So a hard DELETE fails with a foreign-key violation for any item that has
ever been used on a document.  The ERP-correct behaviour is:

* ``deactivate`` (soft) — always allowed; the item leaves the pickers but every
  historical document keeps resolving it;
* ``delete`` (hard) — only while NOTHING references it; otherwise refuse with
  the list of documents that use it, so the user can deactivate instead.

Keeping this in one place means the page, the agent and any future client
cannot disagree about what "delete" means.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Sequence, Tuple

from app.database import fetch_many


async def find_usage(
    organization_id: uuid.UUID,
    *,
    item_id: uuid.UUID,
    column: str,
    references: Sequence[str],
    limit: int = 1,
) -> List[Tuple[str, int]]:
    """Return ``[(table, rows_seen)]`` for documents referencing this item.

    ``column`` is the referencing column (``product_id`` / ``service_id``) and is
    passed explicitly — ``invoice_items`` references BOTH, so deriving it from
    the table name would query the wrong column for services.

    Only the FIRST row per table is fetched (``limit``): enough to answer "is
    this item in use?", which is all the delete rule needs.
    """
    used: List[Tuple[str, int]] = []
    for table in references:
        rows = await fetch_many(
            table,
            filters={
                column: str(item_id),
                "organization_id": str(organization_id),
            },
            select="id",
            limit=limit,
        )
        if rows:
            used.append((table, len(rows)))
    return used


def usage_message(used: Sequence[Tuple[str, int]], name: str, kind: str) -> str:
    """The refusal text: says WHERE it is used and WHAT to do instead."""
    labels = sorted({_REFERENCE_LABELS.get(table, table) for table, _ in used})
    listed = ", ".join(labels)
    return (
        f"'{name}' is already used by {listed}, so deleting it would break "
        f"those documents' history. Deactivate it instead — it then stops "
        f"appearing in new {kind} pickers while every past document keeps "
        f"resolving it correctly."
    )


def normalise_status(status: Any) -> str:
    """'ALL' | 'ACTIVE' | 'INACTIVE' — anything unknown means ALL (no filter)."""
    value = str(status or "ALL").strip().upper()
    return value if value in ("ALL", "ACTIVE", "INACTIVE") else "ALL"


def status_counts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Live counters the page shows in its status strip."""
    active = sum(1 for row in rows if row.get("is_active", True))
    return {
        "total": len(rows),
        "active": active,
        "inactive": len(rows) - active,
    }

PRODUCT_REFERENCES: Tuple[str, ...] = (
    "invoice_items",
    "credit_note_items",
    "purchase_bill_items",
    "purchase_return_items",
    "quotation_items",
)
SERVICE_REFERENCES: Tuple[str, ...] = (
    "invoice_items",
    "credit_note_items",
    "quotation_items",
)

# Human labels for the refusal message (never a bare table name to the user).
_REFERENCE_LABELS = {
    "invoice_items": "sales invoices",
    "credit_note_items": "credit notes",
    "purchase_bill_items": "purchase bills",
    "purchase_return_items": "purchase returns",
    "quotation_items": "quotations",
}
