"""Dedicated receivable/payable ledgers - one per party, under the control account.

WHY THIS EXISTS
---------------
Every sales invoice debited the SAME root account, so the chart itself could not
answer "how much does ABC Furnitures owe?" (live: two posted invoices both at
``1100 Accounts Receivable``).  The revenue side already had a dedicated child
ledger ("4001 chairs Sales"); the party side had nothing.

The schema was built for this and never wired up:
``customers.receivable_account_id`` / ``suppliers.payable_account_id`` exist and
the PAYMENT flows already honour them
(``payment_service._resolve_customer_receivable``).  Nothing ever created or set
them, so every party stayed NULL and every posting hit the shared control
account.

WHAT IT DOES
------------
On party creation - and in the explicit backfill - it ensures ONE dedicated
account per party as a CHILD of the control account, and links it on the row:

    1100            Accounts Receivable        (control account, unchanged)
      1100-0001     ABC Furnitures             (posts on credit sales)
      1100-0002     FDS Labs Pvt

Code ladder, deterministic and in this order:

    (a) a sub-code in the control account's own series -> 1100-0001, 1100-0002...
    (b) when that series is exhausted, the next free code in the numeric series
        -> 1101, 1102 ...
    (c) otherwise a generated code -> 1100-0001-2, RECV-0002 ...

Every account is a CHILD of the control account in all three cases, so
``v_account_subtree_balance`` rolls the parties up into "Accounts Receivable"
and ``v_account_hierarchy`` shows the segregation.

WHAT IT NEVER DOES
------------------
* never invents a party: the account name is the party's own name;
* never merges two parties into one account - an existing account is reused only
  on an EXACT normalised name match under the SAME control account, and a party
  that already carries an account id is left untouched;
* never mutates journals or posts anything - this module only maintains the
  chart and the party link;
* never reads or writes another organisation's data (``organization_id`` is on
  every query);
* resolution (:func:`resolve_customer_receivable_account`) is READ-ONLY, so a
  posting path never grows the chart as a side effect.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.database import fetch_many, fetch_one
from app.repositories import account_repository as a_repo

log = structlog.get_logger(__name__)

RECEIVABLE = "RECEIVABLE"
PAYABLE = "PAYABLE"

# Accepted names for the control account, most specific first.  Matched on the
# NORMALISED name only - a fuzzy match would attach parties to a wrong heading.
_KIND_RULES: Dict[str, Dict[str, Any]] = {
    RECEIVABLE: {
        "control_names": (
            "accounts receivable",
            "trade receivables",
            "trade debtors",
            "sundry debtors",
            "debtors",
            "receivables",
        ),
        "account_type": "ASSET",
        "normal_balance": "DEBIT",
        "explicit_code": "1100",
        "base_code": "1100-0001",
        "label": "receivable",
    },
    PAYABLE: {
        "control_names": (
            "accounts payable",
            "trade payables",
            "trade creditors",
            "sundry creditors",
            "creditors",
            "payables",
        ),
        "account_type": "LIABILITY",
        "normal_balance": "CREDIT",
        "explicit_code": "2010",
        "base_code": "2010-0001",
        "label": "payable",
    },
}

# Upper bound on the (a) sub-code probe before falling through to (b).  Keeps a
# pathological chart from turning one party creation into an unbounded scan.
_MAX_SERIES = 500


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())



async def control_account(
    organization_id: uuid.UUID, *, kind: str
) -> Optional[Dict[str, Any]]:
    """The heading party ledgers hang under, or None.

    Prefers a ROOT account with an accepted name (a control account is a
    heading), then any account with that name, then the explicit code
    (1100 / 2010).  Returns None rather than guessing: without a control account
    the caller leaves the party unlinked and postings keep using the engine's
    own resolution - today's behaviour, no worse.
    """
    rules = _rules(kind)
    rows = await _accounts(organization_id, account_type=rules["account_type"])
    for wanted in rules["control_names"]:
        for row in rows:
            if _norm(row.get("name")) == wanted and not row.get("parent_account_id"):
                return row
    for wanted in rules["control_names"]:
        for row in rows:
            if _norm(row.get("name")) == wanted:
                return row
    return await a_repo.get_account_by_code(
        organization_id, code=rules["explicit_code"]
    )


async def find_party_account(
    organization_id: uuid.UUID, *, kind: str, parent_id: Any, party_name: str
) -> Optional[Dict[str, Any]]:
    """An EXISTING dedicated ledger for this party under this control account.

    Exact normalised name match under the SAME parent only: a near-match, or an
    account hanging under a different heading, is never treated as the party's -
    sharing an account between two parties is a reporting lie.
    """
    if not parent_id or not _norm(party_name):
        return None
    rules = _rules(kind)
    parent = str(parent_id)
    for row in await _accounts(organization_id, account_type=rules["account_type"]):
        if str(row.get("parent_account_id") or "") != parent:
            continue
        if _norm(row.get("name")) == _norm(party_name):
            return row
    return None


async def allocate_code(
    organization_id: uuid.UUID,
    *,
    kind: str,
    parent_code: Optional[str] = None,
    reserved: Any = (),
) -> str:
    """The code a NEW party ledger gets, by the documented ladder a -> b -> c.

    ``reserved`` lets the DRY RUN show a realistic plan: codes already claimed
    earlier in the same report are skipped, so the operator sees 1100-0001,
    1100-0002, ... instead of one repeated code.  Live callers pass nothing and
    behave exactly as before.
    """
    rules = _rules(kind)
    parent_code = str(parent_code or "").strip()
    taken = {str(code).strip() for code in reserved}

    # (a) sub-code in the control account's own series: 1100 -> 1100-0001
    if parent_code:
        for index in range(1, _MAX_SERIES + 1):
            candidate = "%s-%04d" % (parent_code, index)
            if candidate in taken:
                continue
            if not await a_repo.get_account_by_code(organization_id, code=candidate):
                return candidate
        # (b) numeric series above the control code: 1100 -> 1101, 1102 ...
        if parent_code.isdigit():
            base = int(parent_code)
            for delta in range(1, _MAX_SERIES + 1):
                candidate = await a_repo.next_available_code(
                    organization_id, str(base + delta)
                )
                if candidate not in taken:
                    return candidate

    # (c) generated fallback (non-numeric control code, or no control account)
    candidate = await a_repo.next_available_code(
        organization_id, rules["base_code"]
    )
    for _ in range(_MAX_SERIES):
        if candidate not in taken:
            return candidate
        candidate = await a_repo.next_available_code(organization_id, candidate)
    return candidate


def _rules(kind: str) -> Dict[str, Any]:
    try:
        return _KIND_RULES[kind]
    except KeyError:  # pragma: no cover - guarded by the public entry points
        raise ValueError("unknown party ledger kind: %r" % (kind,))


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def ensure_party_account(
    organization_id: uuid.UUID,
    *,
    kind: str,
    party_name: Any,
    existing_account_id: Any = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Ensure the party's dedicated ledger exists.  Returns ``(account, created)``.

    Idempotent and concurrency-safe: a pre-linked account wins, then an existing
    account with the party's exact name under the control account, and only then
    is a new account created (unique ``(organization_id, code)`` makes exactly
    one concurrent insert win; the loser re-reads the winner).
    """
    rules = _rules(kind)
    name = " ".join(str(party_name or "").split())
    if not name:
        return None, False

    linked = _as_uuid(existing_account_id)
    if linked is not None:
        account = await a_repo.get_account(organization_id, account_id=linked)
        if account and account.get("is_active", True):
            return account, False

    control = await control_account(organization_id, kind=kind)
    parent_id = (control or {}).get("id")
    if not parent_id:
        # No heading to segregate under: leave the party unlinked rather than
        # creating a root account that would break the reporting hierarchy.
        log.warning(
            "party_ledger.no_control_account", kind=kind, party=name,
            organization_id=str(organization_id),
        )
        return None, False

    existing = await find_party_account(
        organization_id, kind=kind, parent_id=parent_id, party_name=name
    )
    if existing is not None:
        return existing, False

    code = await allocate_code(
        organization_id, kind=kind, parent_code=(control or {}).get("code")
    )
    log.info(
        "party_ledger.creation_started", kind=kind, party=name, code=code,
        parent=(control or {}).get("name"), organization_id=str(organization_id),
    )
    try:
        created = await a_repo.create_account(
            organization_id=organization_id,
            code=code,
            name=name,
            account_type=rules["account_type"],
            normal_balance=rules["normal_balance"],
            parent_account_id=uuid.UUID(str(parent_id)),
            is_control_account=False,
            description=(
                "Dedicated %s ledger for %s - keeps party balances segregated "
                "under %s." % (rules["label"], name, (control or {}).get("name"))
            ),
        )
    except Exception:
        recovered = await find_party_account(
            organization_id, kind=kind, parent_id=parent_id, party_name=name
        )
        if recovered is not None:
            log.info(
                "party_ledger.reused", kind=kind, party=name,
                via="constraint_recovery",
            )
            return recovered, False
        raise
    log.info(
        "party_ledger.created", kind=kind, party=name, code=code,
        account_id=str(created.get("id")),
    )
    return created, True


async def ensure_customer_receivable_account(
    organization_id: uuid.UUID, customer: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], bool]:
    customer = customer or {}
    return await ensure_party_account(
        organization_id,
        kind=RECEIVABLE,
        party_name=customer.get("name"),
        existing_account_id=customer.get("receivable_account_id"),
    )


async def ensure_supplier_payable_account(
    organization_id: uuid.UUID, supplier: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], bool]:
    supplier = supplier or {}
    return await ensure_party_account(
        organization_id,
        kind=PAYABLE,
        party_name=supplier.get("name"),
        existing_account_id=supplier.get("payable_account_id"),
    )


async def _linked_party_account(
    organization_id: uuid.UUID,
    *,
    table: str,
    column: str,
    party_id: Any,
) -> Optional[Dict[str, Any]]:
    """READ-ONLY lookup of the party's linked ledger.  Never creates anything."""
    linked_id = _as_uuid(party_id)
    if linked_id is None:
        return None
    row = await fetch_one(
        table,
        filters={"id": str(linked_id), "organization_id": str(organization_id)},
    )
    account_id = _as_uuid((row or {}).get(column))
    if account_id is None:
        return None
    account = await a_repo.get_account(organization_id, account_id=account_id)
    if not account or not account.get("is_active", True):
        return None
    return account


async def resolve_customer_receivable_account(
    organization_id: uuid.UUID, customer_id: Any
) -> Optional[Dict[str, Any]]:
    """The customer's own receivable account, or None (caller uses the control)."""
    return await _linked_party_account(
        organization_id,
        table="customers",
        column="receivable_account_id",
        party_id=customer_id,
    )


async def resolve_supplier_payable_account(
    organization_id: uuid.UUID, supplier_id: Any
) -> Optional[Dict[str, Any]]:
    """The supplier's own payable account, or None (caller uses the control)."""
    return await _linked_party_account(
        organization_id,
        table="suppliers",
        column="payable_account_id",
        party_id=supplier_id,
    )


# ---------------------------------------------------------------------------
# Backfill: give every EXISTING party the ledger it never got
# ---------------------------------------------------------------------------

# The party tables this feature maintains, with the column that links them.
_PARTY_TABLES: Tuple[Tuple[str, str, str], ...] = (
    ("customers", "receivable_account_id", RECEIVABLE),
    ("suppliers", "payable_account_id", PAYABLE),
)


async def backfill_missing_accounts(
    *,
    organization_id: Optional[uuid.UUID] = None,
    apply: bool = False,
    limit: int = 1000,
) -> Dict[str, Any]:
    """Ensure every customer/supplier has its dedicated ledger.

    DRY RUN by default (``apply=False``): it reports the accounts it WOULD
    create - party, code, control account - and writes nothing.  With
    ``apply=True`` it creates the missing accounts and links them on the party
    rows; re-running is a no-op (an existing account is reused, a linked party
    is skipped).

    It creates no journal entries and touches no balances: the chart gains
    headings for parties that already exist, nothing more.  Postings that
    already happened keep the control account they were posted to - those are
    accepted accounting records and are never rewritten by a maintenance task.
    """
    report: Dict[str, Any] = {
        "apply": apply,
        "scanned": 0,
        "already_linked": 0,
        "created": [],
        "reused": [],
        "planned": [],
        "errors": [],
    }

    # Codes claimed earlier in this run, per (org, kind): only the DRY RUN needs
    # it, so its plan reads 1100-0001, 1100-0002, ... instead of repeating one.
    reserved: Dict[Any, set] = {}

    for table, column, kind in _PARTY_TABLES:
        filters = (
            {"organization_id": str(organization_id)} if organization_id else {}
        )
        rows = await fetch_many(
            table,
            filters=filters,
            select="id,organization_id,name,%s" % column,
            limit=limit,
        ) or []

        for row in rows:
            report["scanned"] += 1
            org = row.get("organization_id")
            name = row.get("name")
            if not org or not name:
                continue
            if row.get(column):
                report["already_linked"] += 1
                continue
            try:
                if not apply:
                    control = await control_account(org, kind=kind)
                    claimed = reserved.setdefault((str(org), kind), set())
                    code = await allocate_code(
                        org, kind=kind, parent_code=(control or {}).get("code"),
                        reserved=claimed,
                    )
                    claimed.add(code)
                    report["planned"].append({
                        "party_type": table,
                        "organization_id": str(org),
                        "party": name,
                        "code": code,
                        "parent": (control or {}).get("name"),
                    })
                    continue

                account, created = await ensure_party_account(
                    org, kind=kind, party_name=name
                )
                if account is None:
                    report["errors"].append({
                        "party_type": table, "party": name,
                        "error": "no control account in this organisation",
                    })
                    continue
                await _link_party(
                    org,
                    table=table,
                    party_id=row.get("id"),
                    account_id=account.get("id"),
                )
                entry = {
                    "party_type": table,
                    "organization_id": str(org),
                    "party": name,
                    "code": account.get("code"),
                    "account_id": str(account.get("id")),
                }
                report["created" if created else "reused"].append(entry)
            except Exception as exc:  # noqa: BLE001 - one party never stops the run
                log.warning(
                    "party_ledger.backfill_failed", party=name, kind=kind,
                    error=str(exc)[:200],
                )
                report["errors"].append({
                    "party_type": table, "party": name, "error": str(exc)[:200],
                })

    log.info(
        "party_ledger.backfill_done", apply=apply,
        scanned=report["scanned"], already_linked=report["already_linked"],
        created=len(report["created"]), reused=len(report["reused"]),
        planned=len(report["planned"]), errors=len(report["errors"]),
    )
    return report


async def _link_party(
    organization_id: uuid.UUID,
    *,
    table: str,
    party_id: Any,
    account_id: Any,
) -> None:
    """Store the ledger on the party row (the ONLY write this module performs)."""
    from app.repositories import customer_repository, supplier_repository

    pid = _as_uuid(party_id)
    aid = _as_uuid(account_id)
    if pid is None or aid is None:
        return
    if table == "customers":
        await customer_repository.set_receivable_account(
            organization_id, customer_id=pid, account_id=aid
        )
    elif table == "suppliers":
        await supplier_repository.set_payable_account(
            organization_id, supplier_id=pid, account_id=aid
        )


async def _accounts(
    organization_id: uuid.UUID, *, account_type: str
) -> List[Dict[str, Any]]:
    return await a_repo.get_chart_of_accounts(
        organization_id, account_type=account_type, limit=500
    ) or []
