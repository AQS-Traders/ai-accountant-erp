"""
Dedicated revenue ledgers — per revenue stream.

WHY THIS EXISTS
---------------
A trusted sale tool must decide WHICH revenue account to credit.  It used to
fall back to "the first REVENUE account in the chart", which meant a sale of a
MOBILE PHONE was credited to *Software Development Revenue* purely because that
account sorted first in a SOFTWARE_HOUSE organisation's chart:

    Dr  1020  Cash                          25,000.00
    Cr  4010  Software Development Revenue  25,000.00   <-- wrong stream

That is a revenue-mix misstatement, and it happened silently.  The correct
behaviour, per the enhancement brief's "Ask -> Understand -> Classify ->
Validate -> Execute" principle, is:

  1. derive the STREAM from what was actually sold (the item description);
  2. use a dedicated ledger for that stream when one exists;
  3. otherwise fall back only to an UNAMBIGUOUS general revenue account;
  4. otherwise REFUSE and ask — never pick one of several at random.

Step 4 is the important one: an unresolved revenue account is a question for
the user, not a coin toss.  Nothing in this module invents or renames accounts.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import account_repository as a_repo

log = structlog.get_logger(__name__)


# Descriptions too generic to name a revenue stream.
_GENERIC_ITEMS = frozenset({
    "", "it", "items", "item", "goods", "good", "product", "products",
    "stuff", "something", "things", "thing", "sale", "sales", "cash sale",
    "service", "services", "order", "orders", "stock",
})

# General, non-stream revenue accounts, most specific first.  An EXACT
# (case-insensitive) name match on one of these is unambiguous and safe.
_GENERAL_REVENUE_NAMES = (
    "operating revenue",
    "sales revenue",
    "service revenue",
    "services revenue",
    "revenue",
)

_LEDGER_SUFFIX = " Sales"

# Boilerplate the agent puts in front of what was sold ("Sale of Mobile PHONE",
# "Invoice for Chairs").  Stripped so the ledger name is about the ITEM, not the
# document: "Sale of Mobile PHONE" -> "Mobile PHONE" -> "Mobile PHONE Sales".
_LEADING_NOISE = (
    "cash sale of", "cash sale", "sale of", "sales of", "sold",
    "invoice for", "invoice", "receipt for", "receipt",
    "sale", "sales",
)


def stream_label(item_description: Optional[str]) -> Optional[str]:
    """Normalise what was sold into a revenue-stream label, or None.

    Returns None when the description is missing or too generic to name a
    ledger — "something", "goods", "item" and friends never become accounts.
    Only the first line is used so an attached document's item list cannot
    produce a nonsense account name.
    """
    if not item_description:
        return None
    text = str(item_description).strip()
    if not text:
        return None
    text = text.splitlines()[0]
    text = " ".join(text.split())
    if len(text) > 60:
        text = text[:60].rstrip()

    # Strip document boilerplate ("Sale of ...", "Invoice for ...").
    while True:
        lowered = text.lower()
        stripped = None
        for prefix in _LEADING_NOISE:
            if lowered.startswith(prefix + " "):
                stripped = text[len(prefix):].strip()
                break
        if stripped is None or not stripped:
            break
        text = stripped

    if text.lower().strip(" .:-") in _GENERIC_ITEMS:
        return None
    # Strip leading quantity/"x" noise: "2 tables" -> "tables".
    parts = text.split()
    while parts and (parts[0].isdigit() or parts[0].lower() in ("x", "no", "nos")):
        parts.pop(0)
    text = " ".join(parts).strip()
    if not text or text.lower().strip(" .:-") in _GENERIC_ITEMS:
        return None
    return text


def suggest_ledger_name(label: str) -> str:
    """Name proposed for a NEW dedicated ledger: 'Chairs' -> 'Chairs Sales'."""
    return f"{label.strip()}{_LEDGER_SUFFIX}"


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


async def list_revenue_accounts(organization_id: uuid.UUID) -> List[Dict[str, Any]]:
    """All active REVENUE accounts in the organisation's chart."""
    return await a_repo.get_chart_of_accounts(
        organization_id, account_type="REVENUE", limit=100
    ) or []


async def find_stream_ledger(
    organization_id: uuid.UUID, label: str
) -> Optional[Dict[str, Any]]:
    """An EXISTING dedicated ledger for this stream, or None.

    Only exact, deliberate matches count:
      * the account name equals the proposed ledger name ("Chairs Sales"), or
      * the account name equals the stream itself ("Chairs").

    A fuzzy hit is never accepted — guessing here is the very defect this
    module exists to remove.
    """
    wanted = {_norm(label), _norm(suggest_ledger_name(label))}
    for row in await list_revenue_accounts(organization_id):
        if _norm(row.get("name")) in wanted:
            return row
    return None


async def find_general_revenue(
    organization_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """The organisation's general revenue account — exact name match only."""
    by_name = {_norm(r.get("name")): r for r in await list_revenue_accounts(organization_id)}
    for name in _GENERAL_REVENUE_NAMES:
        if name in by_name:
            return by_name[name]
    return None


async def resolve_revenue_account(
    organization_id: uuid.UUID,
    item_description: Optional[str],
) -> tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """Choose the revenue account for a sale.

    Returns ``(account, stream_label, reason)``.

    ``account`` is **None** when the choice is ambiguous — the caller must then
    ask the user rather than pick one.  ``reason`` is a short machine-readable
    tag for the audit trail: ``stream``, ``only-one`` or ``ambiguous``.
    """
    label = stream_label(item_description)

    if label:
        ledger = await find_stream_ledger(organization_id, label)
        if ledger is not None:
            return ledger, label, "stream"
        # A stream is named but has no dedicated ledger yet.  Whether it
        # DESERVES one is the user's call (reporting granularity), so this is
        # deliberately ambiguous — the caller asks.
        return None, label, "ambiguous"

    rows = await list_revenue_accounts(organization_id)
    if len(rows) == 1:
        # Exactly one revenue account: crediting it is unambiguous, not a guess.
        return rows[0], label, "only-one"

    return None, label, "ambiguous"


__all__ = [
    "stream_label",
    "suggest_ledger_name",
    "list_revenue_accounts",
    "find_stream_ledger",
    "find_general_revenue",
    "resolve_revenue_account",
]
