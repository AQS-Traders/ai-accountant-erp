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

    rows = await list_revenue_accounts(organization_id)

    if label:
        ledger = await find_stream_ledger(organization_id, label)
        if ledger is not None:
            return ledger, label, "stream"
        if len(rows) == 1:
            # Only ONE revenue account exists: crediting it is unambiguous, so
            # there is nothing to ask about.  (needs_review() agrees — the two
            # must not disagree, or a sale would be asked a pointless question.)
            return rows[0], label, "only-one"
        # A stream is named but has no dedicated ledger and the chart offers
        # several candidates.  Whether the stream DESERVES its own ledger is the
        # user's call (reporting granularity), so this is deliberately
        # ambiguous — the caller asks.
        return None, label, "ambiguous"

    if len(rows) == 1:
        return rows[0], label, "only-one"

    return None, label, "ambiguous"


# The heading stream ledgers group under when the chart has no general revenue
# account of its own (see ensure_revenue_parent).
REVENUE_PARENT_NAME = "Revenue"


async def ensure_revenue_parent(
    organization_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """The account that dedicated stream ledgers hang under — created if needed.

    Prefers an existing GENERAL revenue account ("Operating Revenue", "Revenue",
    ...).  When the chart contains only SPECIFIC stream accounts (for example a
    software house whose revenue accounts are "Software Development Revenue",
    "Consulting Revenue", ...), it creates a root "Revenue" heading instead.

    Without this, a goods sale's ledger would be parented to whichever specific
    service stream happened to sort first — reporting "Mobile Phone Sales" as a
    child of "Software Development Revenue", which is exactly the kind of
    nonsense this feature exists to prevent.
    """
    general = await find_general_revenue(organization_id)
    if general is not None:
        return general

    rows = await list_revenue_accounts(organization_id)
    for row in rows:
        if _norm(row.get("name")) == _norm(REVENUE_PARENT_NAME):
            return row

    code = await a_repo.next_available_code(organization_id, "4000")
    created = await a_repo.create_account(
        organization_id=organization_id,
        code=code,
        name=REVENUE_PARENT_NAME,
        account_type="REVENUE",
        normal_balance="CREDIT",
        parent_account_id=None,
        description=(
            "Revenue heading — dedicated revenue-stream ledgers group under this."
        ),
    )
    log.info("revenue_ledger.parent_created", code=code, name=REVENUE_PARENT_NAME)
    return created


async def find_parent_revenue(
    organization_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """The revenue account a NEW dedicated ledger should hang under.

    Prefers the organisation's general revenue account (exact name match), then
    any root-level revenue account, then the first revenue account.  This makes
    the child a CHILD of Revenue rather than a new root — the hierarchy the
    reporting views already roll up.
    """
    rows = await list_revenue_accounts(organization_id)
    if not rows:
        return None
    by_name = {_norm(r.get("name")): r for r in rows}
    for name in _GENERAL_REVENUE_NAMES:
        if name in by_name:
            return by_name[name]
    roots = [r for r in rows if not r.get("parent_account_id")]
    return roots[0] if roots else rows[0]


async def _next_child_code(
    organization_id: uuid.UUID, parent: Optional[Dict[str, Any]]
) -> str:
    """An unused code one above the parent's (4000 -> 4001, then 4002, ...).

    ``next_available_code`` probes forward, so a collision can never overwrite
    or steal an existing account's code.
    """
    base = 4001
    if parent is not None:
        try:
            base = int(str(parent.get("code") or "").strip()) + 1
        except (TypeError, ValueError):
            base = 4001
    return await a_repo.next_available_code(organization_id, str(base))


async def create_stream_ledger(
    organization_id: uuid.UUID,
    label: str,
    *,
    parent: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[Dict[str, Any]], bool]:
    """Create (or return) the dedicated ledger for *label*.

    Returns ``(account, created)``.  Idempotent: if the ledger already exists it
    is returned with ``created=False``.  The account is created as a CHILD of the
    revenue parent, never as a new root, so the chart hierarchy and the
    reporting roll-up stay intact.
    """
    existing = await find_stream_ledger(organization_id, label)
    if existing is not None:
        return existing, False

    parent = parent or await ensure_revenue_parent(organization_id)
    code = await _next_child_code(organization_id, parent)
    created = await a_repo.create_account(
        organization_id=organization_id,
        code=code,
        name=suggest_ledger_name(label),
        account_type="REVENUE",
        normal_balance="CREDIT",
        parent_account_id=(
            uuid.UUID(str(parent["id"])) if parent is not None else None
        ),
        description=f"Dedicated revenue ledger for {label}",
    )
    log.info(
        "revenue_ledger.created",
        code=code,
        name=suggest_ledger_name(label),
        parent=parent.get("name") if parent else None,
    )
    return created, True


async def apply_decision(
    organization_id: uuid.UUID,
    label: Optional[str],
    decision: Optional[str],
) -> tuple[Optional[Dict[str, Any]], bool]:
    """Turn the user's review answer into a concrete revenue account.

    ``CREATE``       -> create/find the dedicated ledger for the stream.
    ``USE_EXISTING`` -> the organisation's general revenue account.
    anything else    -> (None, False): the caller must keep asking, never guess.
    """
    choice = str(decision or "").strip().upper()
    if choice == "CREATE" and label:
        return await create_stream_ledger(organization_id, label)
    if choice in ("USE_EXISTING", "SKIP", "EXISTING"):
        general = await find_general_revenue(organization_id)
        if general is not None:
            return general, False
        rows = await list_revenue_accounts(organization_id)
        if len(rows) == 1:
            return rows[0], False
    return None, False


async def needs_review(
    organization_id: uuid.UUID,
    item_description: Optional[str],
    decision: Optional[str],
) -> bool:
    """Whether a sale must ASK before its revenue account is chosen.

    True only when a stream can be named, no dedicated ledger exists for it, the
    user has not already answered, AND the choice is genuinely non-obvious (more
    than one revenue account exists).  A single-revenue-account chart is
    unambiguous, so it never nags.
    """
    if decision:
        return False
    label = stream_label(item_description)
    if not label:
        return False
    if await find_stream_ledger(organization_id, label) is not None:
        return False
    return len(await list_revenue_accounts(organization_id)) > 1


__all__ = [
    "stream_label",
    "suggest_ledger_name",
    "list_revenue_accounts",
    "find_stream_ledger",
    "find_general_revenue",
    "find_parent_revenue",
    "resolve_revenue_account",
    "create_stream_ledger",
    "apply_decision",
    "needs_review",
]
