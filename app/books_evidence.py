"""
AI-Native ERP — LIVE BOOKS EVIDENCE layer (Work Stream S3)
==========================================================

Python's job here is *retrieval and enforcement*, never interpretation.

The LLM accounting-reasoning loop (``app/accounting_reasoning.py``) decides
which evidence it needs — it names an evidence KIND in business terms
("fixed_assets", "open_payables", "journal_entries"), not a SQL query and not
a final accounting treatment.  This module:

* exposes the CLOSED set of evidence kinds the model may ask for, with the
  argument schema each kind accepts;
* validates every request (known kind, whitelisted argument names, argument
  types, bounded cardinality) — an unknown kind or argument is REJECTED;
* enforces permissions through the existing authorization layer
  (``app.permissions.authorize_tool``) on the read-only tool slug that gates
  the kind;
* reads EXCLUSIVELY through organization-scoped repositories/views — the
  organization id is injected here and can never come from the model;
* bounds every result set so the prompt stays small and predictable;
* never raises: a failing source degrades to an empty result carrying the
  reason, and that failure is INFORMATION for the model (``empty`` is not an
  error, ``error`` is).

Nothing in this module decides what a request *means*, which account to use,
which party is required, whether an asset exists, or which workflow to run.
It returns what the books actually contain, labelled for the model as::

    LIVE BOOKS EVIDENCE — use this to reassess the request

Architecture: docs/LLM_PRIMARY_REASONING_ARCHITECTURE.md
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import structlog

log = structlog.get_logger(__name__)

#: The label every evidence block carries into the prompt.  Deterministic
#: values and live evidence must never be confusable in the model's input.
EVIDENCE_LABEL = "LIVE BOOKS EVIDENCE — use this to reassess the request"

#: Hard bounds — the evidence block is context, not a data dump.
MAX_RECORDS_PER_KIND = 12
MAX_TERMS = 5
MAX_REQUESTS_PER_ROUND = 6
MAX_RECORD_CHARS = 1200

#: Argument schema value types accepted from the model.
_ARG_TYPES = ("string", "list", "number")


@dataclass(frozen=True)
class EvidenceKind:
    """One closed evidence area the reasoning model may inspect."""

    kind: str
    title: str
    description: str
    args: Dict[str, str]
    permission_slug: str
    loader: Callable[..., Awaitable[List[Dict[str, Any]]]]


@dataclass
class EvidenceResult:
    """What the books actually contained for one evidence request."""

    kind: str
    title: str
    why: str = ""
    records: List[Dict[str, Any]] = field(default_factory=list)
    source: str = ""
    error: Optional[str] = None
    truncated: bool = False
    rejected: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def empty(self) -> bool:
        """A genuinely empty result — valid accounting information."""
        return self.ok and not self.records

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "why": self.why,
            "ok": self.ok,
            "empty": self.empty,
            "records": self.records,
            "source": self.source,
            "error": self.error,
            "truncated": self.truncated,
            "rejected": self.rejected,
        }


# ---------------------------------------------------------------------------
# Financial-statement mapping (deterministic accounting structure)
# ---------------------------------------------------------------------------
# The chart of accounts carries account_type / normal_balance / parent
# hierarchy; the financial-statement LINE an account rolls into is derived
# from that structure deterministically.  This is accounting structure, not a
# phrase guess: the model is told which statement line each ledger feeds.

_CONTRA_ASSET_HINTS = ("accumulated depreciation", "allowance", "provision for doubtful")


def financial_statement_line(account_type: Optional[str], name: Optional[str] = None) -> str:
    """Deterministic statement line for an account (structure, not wording)."""
    atype = (account_type or "").upper()
    lname = (name or "").lower()
    if atype == "ASSET":
        if any(h in lname for h in _CONTRA_ASSET_HINTS):
            return "Balance sheet — Non-current assets (contra)"
        return "Balance sheet — Assets"
    if atype == "LIABILITY":
        return "Balance sheet — Liabilities"
    if atype == "EQUITY":
        return "Balance sheet — Equity"
    if atype == "REVENUE":
        if "cost of" in lname or lname.startswith("cogs"):
            return "Income statement — Cost of sales"
        if "other income" in lname or "gain" in lname:
            return "Income statement — Other income"
        return "Income statement — Revenue"
    if atype == "EXPENSE":
        if "depreciation" in lname:
            return "Income statement — Depreciation"
        if "interest" in lname or "finance" in lname or "bank charge" in lname:
            return "Income statement — Finance costs"
        if "tax" in lname:
            return "Income statement — Tax expense"
        return "Income statement — Expenses"
    return "Unmapped"


# ---------------------------------------------------------------------------
# Bounded, organization-scoped readers
# ---------------------------------------------------------------------------
# Every reader receives organization_id from THIS module (never from the
# model) and returns a list of compact dicts.  Repository failures are caught
# by gather_evidence; readers only translate rows into prompt-sized facts.


def _terms(args: Dict[str, Any]) -> List[str]:
    raw = args.get("terms")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text.lower() not in {t.lower() for t in out}:
            out.append(text[:80])
    return out[:MAX_TERMS]


def _limit(args: Dict[str, Any]) -> int:
    try:
        requested = int(args.get("limit"))
    except (TypeError, ValueError):
        return MAX_RECORDS_PER_KIND
    return max(1, min(requested, MAX_RECORDS_PER_KIND))


def _matches(record: Dict[str, Any], terms: Sequence[str]) -> bool:
    """True when any term appears in any string field of *record*."""
    if not terms:
        return True
    haystack = " ".join(
        str(v).lower() for v in record.values() if isinstance(v, (str, int, float))
    )
    return any(term.lower() in haystack for term in terms)


def _trim(record: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None/empty fields and cap value length for prompt safety."""
    out: Dict[str, Any] = {}
    for key, value in record.items():
        if value is None or value == "":
            continue
        text = str(value)
        out[key] = text[:200] if len(text) > 200 else value
    return out
async def _load_chart_of_accounts(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Chart of accounts + classification + financial-statement mapping."""
    from app.repositories import account_repository

    rows = await account_repository.get_chart_of_accounts(
        organization_id, limit=500
    )
    terms = _terms(args)
    parent_ids = {
        str(r.get("parent_account_id")) for r in rows if r.get("parent_account_id")
    }
    selected = [r for r in rows if _matches(r, terms)] if terms else rows
    out = []
    for row in selected[: _limit(args) * 4 if terms else 60]:
        out.append(
            _trim(
                {
                    "id": row.get("id"),
                    "code": row.get("code"),
                    "name": row.get("name"),
                    "account_type": row.get("account_type"),
                    "normal_balance": row.get("normal_balance"),
                    "is_control_account": row.get("is_control_account"),
                    "is_posting_account": str(row.get("id")) not in parent_ids,
                    "statement_line": financial_statement_line(
                        row.get("account_type"), row.get("name")
                    ),
                }
            )
        )
    return out


async def _load_account_search(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    from app.repositories import account_repository

    out: List[Dict[str, Any]] = []
    for term in _terms(args) or [""]:
        rows = await account_repository.search_accounts(
            organization_id, query=term, limit=_limit(args)
        )
        for row in rows:
            out.append(
                _trim(
                    {
                        "id": row.get("id"),
                        "code": row.get("code"),
                        "name": row.get("name"),
                        "account_type": row.get("account_type"),
                        "normal_balance": row.get("normal_balance"),
                        "statement_line": financial_statement_line(
                            row.get("account_type"), row.get("name")
                        ),
                    }
                )
            )
    return out


async def _load_parties(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Customers and suppliers matching the request's own words."""
    from app.repositories import customer_repository, supplier_repository

    terms = _terms(args)
    role = str(args.get("party_role") or "").strip().lower()
    out: List[Dict[str, Any]] = []

    async def _role_rows(loader, label: str, query: str) -> None:
        rows = await loader(organization_id, query=query, limit=_limit(args))
        for row in rows:
            out.append(
                _trim(
                    {
                        "role": label,
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "code": row.get("customer_code") or row.get("supplier_code"),
                        "is_active": row.get("is_active"),
                    }
                )
            )

    for term in terms or [""]:
        if role in ("", "customer"):
            await _role_rows(customer_repository.search_customers, "customer", term)
        if role in ("", "supplier"):
            await _role_rows(supplier_repository.search_suppliers, "supplier", term)
    return out


async def _load_open_receivables(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Unsettled customer invoices — scope may be one party or all."""
    from app.repositories import customer_repository, invoice_repository

    party_names = [str(args.get("party_name") or "").strip()] if args.get("party_name") else _terms(args)
    party_names = [p for p in party_names if p]
    out: List[Dict[str, Any]] = []

    if party_names:
        for name in party_names[:3]:
            customers = await customer_repository.search_customers(
                organization_id, query=name, limit=5
            )
            for customer in customers:
                rows = await customer_repository.get_open_receivables(
                    organization_id, customer_id=uuid.UUID(str(customer["id"]))
                )
                for row in rows:
                    out.append(_trim({**row, "customer_name": customer.get("name")}))
    else:
        for status in ("ISSUED", "PARTIALLY_PAID", "OVERDUE"):
            rows = await invoice_repository.list_invoices(
                organization_id, status=status, limit=_limit(args)
            )
            out.extend(_trim(row) for row in rows)
    return out


async def _load_open_payables(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Unsettled supplier bills / expenses — scope may be one party or all."""
    from app.repositories import purchase_repository, supplier_repository

    party_names = [str(args.get("party_name") or "").strip()] if args.get("party_name") else _terms(args)
    party_names = [p for p in party_names if p]
    out: List[Dict[str, Any]] = []

    if party_names:
        for name in party_names[:3]:
            suppliers = await supplier_repository.search_suppliers(
                organization_id, query=name, limit=5
            )
            for supplier in suppliers:
                rows = await supplier_repository.get_open_payables(
                    organization_id, supplier_id=uuid.UUID(str(supplier["id"]))
                )
                for row in rows:
                    out.append(_trim({**row, "supplier_name": supplier.get("name")}))
    else:
        for status in ("OPEN", "PARTIALLY_PAID"):
            rows = await purchase_repository.list_purchase_bills(
                organization_id, status=status, limit=_limit(args)
            )
            out.extend(_trim(row) for row in rows)
    return out


async def _load_ledgers(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Customer/supplier subledger movement for a named party."""
    from app.repositories import customer_repository, supplier_repository

    names = [str(args.get("party_name") or "").strip()] if args.get("party_name") else _terms(args)
    names = [n for n in names if n]
    out: List[Dict[str, Any]] = []
    for name in names[:3]:
        customers = await customer_repository.search_customers(
            organization_id, query=name, limit=3
        )
        for customer in customers:
            rows = await customer_repository.get_customer_ledger(
                organization_id, customer_id=uuid.UUID(str(customer["id"])), limit=_limit(args)
            )
            for row in rows:
                out.append(_trim({**row, "party_name": customer.get("name"), "role": "customer"}))
        suppliers = await supplier_repository.search_suppliers(
            organization_id, query=name, limit=3
        )
        for supplier in suppliers:
            rows = await supplier_repository.get_supplier_ledger(
                organization_id, supplier_id=uuid.UUID(str(supplier["id"])), limit=_limit(args)
            )
            for row in rows:
                out.append(_trim({**row, "party_name": supplier.get("name"), "role": "supplier"}))
    return out


async def _load_documents(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Invoices, purchase bills and expenses — the document layer."""
    from app.repositories import (
        expense_repository,
        invoice_repository,
        purchase_repository,
    )

    terms = _terms(args)
    out: List[Dict[str, Any]] = []
    invoices = await invoice_repository.list_invoices(
        organization_id, limit=_limit(args)
    )
    out.extend(_trim({"kind": "invoice", **row}) for row in invoices if _matches(row, terms))
    bills = await purchase_repository.list_purchase_bills(
        organization_id, limit=_limit(args)
    )
    out.extend(_trim({"kind": "purchase_bill", **row}) for row in bills if _matches(row, terms))
    expenses = await expense_repository.list_expenses(
        organization_id, limit=_limit(args)
    )
    out.extend(_trim({"kind": "expense", **row}) for row in expenses if _matches(row, terms))
    return out


async def _load_journal_entries(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Posted/draft journal entries with their lines (the GL evidence)."""
    from app.database import fetch_many
    from app.repositories import journal_repository

    filters: Dict[str, Any] = {"organization_id": str(organization_id)}
    if args.get("from_date"):
        filters["transaction_date"] = f"gte.{args['from_date']}"
    entries = await fetch_many(
        "journal_entries",
        filters=filters,
        select=(
            "id,journal_number,transaction_date,description,status,"
            "source_type,source_id,total_debit,total_credit,reversed_by_entry_id"
        ),
        order="transaction_date.desc",
        limit=_limit(args),
    )
    terms = _terms(args)
    selected = [e for e in entries if _matches(e, terms)] if terms else entries
    out: List[Dict[str, Any]] = []
    for entry in selected:
        lines = await journal_repository.get_journal_lines(
            entry_id=uuid.UUID(str(entry["id"])), organization_id=organization_id
        )
        out.append(
            _trim(
                {
                    **entry,
                    "lines": [
                        _trim(
                            {
                                "account_id": line.get("account_id"),
                                "debit": line.get("debit"),
                                "credit": line.get("credit"),
                                "description": line.get("description"),
                            }
                        )
                        for line in lines
                    ],
                }
            )
        )
    return out
async def _load_fixed_assets(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Fixed-asset register: cost, accumulated depreciation, carrying value,
    status, GL links, lifecycle transactions and disposal/depreciation trail."""
    from app.database import fetch_many
    from app.repositories import fixed_asset_repository

    terms = _terms(args)
    rows: List[Dict[str, Any]] = []
    if terms:
        for term in terms:
            rows.extend(
                await fixed_asset_repository.search_assets(
                    organization_id, query=term, limit=_limit(args)
                )
            )
    else:
        rows = await fetch_many(
            "fixed_assets",
            filters={"organization_id": str(organization_id)},
            select=(
                "id,asset_code,name,purchase_date,purchase_cost,salvage_value,"
                "useful_life_years,depreciation_method,accumulated_depreciation,"
                "book_value,status,gl_asset_account_id"
            ),
            order="purchase_date.desc",
            limit=_limit(args),
        )
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        asset_id = str(row.get("id"))
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        transactions = await fixed_asset_repository.get_asset_transactions(
            organization_id, asset_id=uuid.UUID(asset_id), limit=10
        )
        out.append(
            _trim(
                {
                    **row,
                    "already_disposed": str(row.get("status") or "").upper()
                    in ("DISPOSED", "SOLD", "WRITTEN_OFF"),
                    "lifecycle": [
                        _trim(
                            {
                                "type": t.get("transaction_type"),
                                "date": t.get("transaction_date"),
                                "amount": t.get("amount"),
                                "journal_entry_id": t.get("journal_entry_id"),
                            }
                        )
                        for t in transactions
                    ],
                }
            )
        )
    return out
async def _load_catalog(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Product and service catalog entries (catalog only — no stock ledger)."""
    from app.repositories import product_repository, service_repository

    terms = _terms(args)
    out: List[Dict[str, Any]] = []
    for term in terms or [""]:
        for row in await product_repository.search_products(
            organization_id, query=term, limit=_limit(args)
        ):
            out.append(_trim({"kind": "product", **row}))
        for row in await service_repository.search_services(
            organization_id, query=term, limit=_limit(args)
        ):
            out.append(_trim({"kind": "service", **row}))
    return out


async def _load_bank_accounts(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Bank and cash accounts (settlement accounts available for money)."""
    from app.repositories import bank_repository, organization_repository

    out: List[Dict[str, Any]] = []
    for row in await organization_repository.get_bank_accounts(organization_id) or []:
        out.append(_trim({"kind": "bank_account", **row}))
    for row in await bank_repository.list_cash_accounts(organization_id) or []:
        out.append(_trim({"kind": "cash_account", **row}))
    return out[:_limit(args)]


async def _load_periods(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Financial years and accounting periods, with the OPEN period flagged."""
    from app.repositories import organization_repository

    out: List[Dict[str, Any]] = []
    years = await organization_repository.get_financial_years(organization_id)
    for row in years or []:
        out.append(_trim({"kind": "financial_year", **row}))
    open_period = await organization_repository.get_open_accounting_period(
        organization_id
    )
    open_id = str((open_period or {}).get("id") or "")
    periods = await organization_repository.get_accounting_periods(organization_id)
    for row in periods or []:
        out.append(
            _trim(
                {
                    "kind": "accounting_period",
                    **row,
                    "is_open": str(row.get("id")) == open_id if open_id else None,
                }
            )
        )
    return out[: _limit(args) + 12]


async def _load_policies(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Organization profile and learned accounting policies/preferences."""
    from app.repositories import organization_repository
    from app.services import preference_service

    out: List[Dict[str, Any]] = []
    org = await organization_repository.get_organization(organization_id=organization_id)
    if isinstance(org, dict):
        out.append(
            _trim(
                {
                    "kind": "organization",
                    "id": org.get("id"),
                    "name": org.get("name"),
                    "business_type": org.get("business_type") or org.get("business_type_code"),
                    "base_currency": org.get("base_currency_code"),
                    "country": org.get("country_code") or org.get("country"),
                }
            )
        )
    prefs = await preference_service.get_all_preferences(organization_id)
    for key, value in (prefs or {}).items():
        out.append(_trim({"kind": "policy_preference", "key": key, "value": value}))
    return out


async def _load_prior_transactions(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Receipts, supplier payments and expenses — records of money already
    moved (evidence for settlement vs recognition vs advance)."""
    from app.repositories import expense_repository, payment_repository

    terms = _terms(args)
    party = str(args.get("party_name") or "").strip()
    out: List[Dict[str, Any]] = []

    receipts = await payment_repository.list_receipts(
        organization_id, limit=_limit(args)
    )
    out.extend(
        _trim({"kind": "receipt", **row})
        for row in receipts
        if _matches(row, terms) or (party and party.lower() in str(row).lower())
    )
    payments = await payment_repository.list_payments(
        organization_id, limit=_limit(args)
    )
    out.extend(
        _trim({"kind": "supplier_payment", **row})
        for row in payments
        if _matches(row, terms) or (party and party.lower() in str(row).lower())
    )
    expenses = await expense_repository.list_expenses(
        organization_id, limit=_limit(args)
    )
    out.extend(
        _trim({"kind": "expense", **row})
        for row in expenses
        if _matches(row, terms) or (party and party.lower() in str(row).lower())
    )
    return out


async def _load_reports(
    organization_id: uuid.UUID, **args: Any
) -> List[Dict[str, Any]]:
    """Trial balance / income statement / balance sheet summary lines."""
    from app.repositories import report_repository

    out: List[Dict[str, Any]] = []
    trial = await report_repository.get_trial_balance(organization_id, limit=200)
    out.extend(_trim({"kind": "trial_balance", **row}) for row in trial or [])
    income = await report_repository.get_income_statement(organization_id, limit=200)
    out.extend(_trim({"kind": "income_statement", **row}) for row in income or [])
    balance = await report_repository.get_balance_sheet(organization_id, limit=200)
    out.extend(_trim({"kind": "balance_sheet", **row}) for row in balance or [])
    return out
# ---------------------------------------------------------------------------
# The closed evidence registry
# ---------------------------------------------------------------------------
# ``args`` is the whitelist of argument names (and their types) each kind
# accepts.  Anything else in a model request is REJECTED — Python decides what
# a lookup may read, the model decides which lookup it needs.

EVIDENCE_KINDS: Dict[str, EvidenceKind] = {
    "chart_of_accounts": EvidenceKind(
        kind="chart_of_accounts",
        title="Chart of accounts with classifications and statement mapping",
        description=(
            "Every ledger with its type, normal balance, whether it is a "
            "posting account or a heading, and which financial-statement "
            "line it feeds. Pass 'terms' to filter to a subject area."
        ),
        args={"terms": "list", "limit": "number"},
        permission_slug="get_chart_of_accounts",
        loader=_load_chart_of_accounts,
    ),
    "account_search": EvidenceKind(
        kind="account_search",
        title="Ledger search by name or code",
        description="Search the chart of accounts by the words in the request.",
        args={"terms": "list", "limit": "number"},
        permission_slug="search_account",
        loader=_load_account_search,
    ),
    "parties": EvidenceKind(
        kind="parties",
        title="Customers and suppliers matching the request's own words",
        description=(
            "Search both party ledgers. An empty result means no such party "
            "record exists — never assume a party exists."
        ),
        args={"terms": "list", "party_role": "string", "limit": "number"},
        permission_slug="search_customer",
        loader=_load_parties,
    ),
    "ledgers": EvidenceKind(
        kind="ledgers",
        title="Customer/supplier subledger movement",
        description=(
            "Every posted movement for a named party (invoices, receipts, "
            "bills, payments) with the running balance."
        ),
        args={"party_name": "string", "terms": "list", "limit": "number"},
        permission_slug="get_customer_ledger",
        loader=_load_ledgers,
    ),
    "open_receivables": EvidenceKind(
        kind="open_receivables",
        title="Unsettled sales invoices (trade receivables)",
        description=(
            "Open customer invoices, optionally scoped to one party. Tells "
            "you whether money received from a customer settles an existing "
            "receivable or is something else (advance, refund, capital)."
        ),
        args={"party_name": "string", "terms": "list", "limit": "number"},
        permission_slug="get_customer_ledger",
        loader=_load_open_receivables,
    ),
    "open_payables": EvidenceKind(
        kind="open_payables",
        title="Unsettled purchase bills (trade payables)",
        description=(
            "Open supplier bills, optionally scoped to one party. Tells you "
            "whether a payment settles an existing payable or is something "
            "else (advance, loan repayment, owner withdrawal, new purchase)."
        ),
        args={"party_name": "string", "terms": "list", "limit": "number"},
        permission_slug="get_supplier_ledger",
        loader=_load_open_payables,
    ),
    "documents": EvidenceKind(
        kind="documents",
        title="Existing documents (invoices, purchase bills, expenses)",
        description=(
            "Full document list with status and amounts. Use it to detect "
            "that an event was already recorded before proposing a new one."
        ),
        args={"terms": "list", "limit": "number"},
        permission_slug="get_invoice",
        loader=_load_documents,
    ),
    "journal_entries": EvidenceKind(
        kind="journal_entries",
        title="Journal entries (general ledger) with their lines",
        description=(
            "Recent journal entries including line-level debits/credits. Use "
            "it to see how an event was booked, whether it was already "
            "booked, and which accounts were used."
        ),
        args={"terms": "list", "from_date": "string", "limit": "number"},
        permission_slug="get_general_ledger",
        loader=_load_journal_entries,
    ),
}


EVIDENCE_KINDS.update(
    {
        "fixed_assets": EvidenceKind(
            kind="fixed_assets",
            title="Fixed-asset register (cost, accumulated depreciation, book value)",
            description=(
                "Registered assets with acquisition cost, accumulated "
                "depreciation, carrying/book value, status and lifecycle "
                "rows. An empty result means the asset is NOT registered."
            ),
            args={"terms": "list", "limit": "number"},
            permission_slug="search_fixed_asset",
            loader=_load_fixed_assets,
        ),
        "catalog": EvidenceKind(
            kind="catalog",
            title="Product and service catalog",
            description=(
                "Catalog items matching the request's words (this ERP has no "
                "stock-quantity ledger)."
            ),
            args={"terms": "list", "limit": "number"},
            permission_slug="search_product",
            loader=_load_catalog,
        ),
        "bank_accounts": EvidenceKind(
            kind="bank_accounts",
            title="Bank and cash accounts available for settlement",
            description="Existing bank/cash accounts with their linked GL accounts.",
            args={"limit": "number"},
            permission_slug="list_bank_accounts",
            loader=_load_bank_accounts,
        ),
        "periods": EvidenceKind(
            kind="periods",
            title="Financial years and accounting periods",
            description=(
                "The accounting calendar with the OPEN period flagged. A "
                "closed period changes what may be posted and when."
            ),
            args={"limit": "number"},
            permission_slug="search_account",
            loader=_load_periods,
        ),
        "policies": EvidenceKind(
            kind="policies",
            title="Organization profile and accounting policies",
            description="Organization identity plus learned accounting policies.",
            args={},
            permission_slug="search_account",
            loader=_load_policies,
        ),
        "prior_transactions": EvidenceKind(
            kind="prior_transactions",
            title="Receipts, supplier payments and expenses already recorded",
            description=(
                "Money-movement records. Use it to distinguish settlement "
                "from recognition and to detect an already-recorded event."
            ),
            args={"party_name": "string", "terms": "list", "limit": "number"},
            permission_slug="get_customer_ledger",
            loader=_load_prior_transactions,
        ),
        "reports": EvidenceKind(
            kind="reports",
            title="Trial balance, income statement and balance sheet lines",
            description="Statement-level summary of the books.",
            args={"limit": "number"},
            permission_slug="get_trial_balance",
            loader=_load_reports,
        ),
    }
)


def evidence_kind_names() -> List[str]:
    return sorted(EVIDENCE_KINDS)


def evidence_catalog_text(description_limit: int = 96) -> str:
    """The catalog shown to the model: which lookups exist and their args.

    Descriptions are capped: the catalog is contract text the model must read on
    every round, and the reasoning call is the slowest call in the pipeline — so
    it stays informative without inflating the prompt.
    """
    lines = [
        "EVIDENCE CATALOG (read-only lookups you may request; each is "
        "organization-scoped and permission-checked):"
    ]
    for kind in evidence_kind_names():
        spec = EVIDENCE_KINDS[kind]
        args = ", ".join(f"{name}:{typ}" for name, typ in spec.args.items()) or "no arguments"
        description = (spec.description or "").strip()
        if len(description) > description_limit:
            description = description[: description_limit - 1].rstrip() + "…"
        lines.append(f"  - {spec.kind} — {description} (args: {args})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Request parsing + Python-side validation (the enforcement half)
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRequest:
    kind: str
    why: str = ""
    args: Dict[str, Any] = field(default_factory=dict)


def parse_evidence_requests(raw: Any) -> List[EvidenceRequest]:
    """Read the model's ``evidence_requests`` array (defensive, bounded)."""
    if not isinstance(raw, list):
        return []
    out: List[EvidenceRequest] = []
    for item in raw[:MAX_REQUESTS_PER_ROUND]:
        if isinstance(item, str):
            out.append(EvidenceRequest(kind=item.strip().lower()))
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("name") or "").strip().lower()
        out.append(
            EvidenceRequest(
                kind=kind,
                why=str(item.get("why") or item.get("reason") or "")[:300],
                args=item.get("args") if isinstance(item.get("args"), dict) else {},
            )
        )
    return out


def validate_evidence_request(request: EvidenceRequest) -> Optional[str]:
    """Return a rejection message, or None when the request is allowed.

    Rejections are deterministic and are fed BACK to the model (never silently
    repaired into a different lookup).
    """
    spec = EVIDENCE_KINDS.get((request.kind or "").strip().lower())
    if spec is None:
        return (
            f"Unknown evidence kind '{request.kind}'. Allowed kinds: "
            f"{', '.join(evidence_kind_names())}."
        )
    for name, value in (request.args or {}).items():
        if name not in spec.args:
            return (
                f"Argument '{name}' is not accepted by evidence kind "
                f"'{spec.kind}'. Allowed args: "
                f"{', '.join(spec.args) or 'none'}."
            )
        if value in (None, ""):
            continue
        expected = spec.args[name]
        if expected == "list" and isinstance(value, str):
            continue
        if expected == "list" and not isinstance(value, (list, tuple)):
            return f"Argument '{name}' must be a list of strings."
        if expected == "number":
            try:
                float(value)
            except (TypeError, ValueError):
                return f"Argument '{name}' must be a number."
        if expected == "string" and not isinstance(value, (str, int, float)):
            return f"Argument '{name}' must be a string."
    return None


# ---------------------------------------------------------------------------
# Execution — the model SELECTS the lookup; Python ENFORCES what it may read
# ---------------------------------------------------------------------------


async def gather_evidence(
    requests: Sequence[EvidenceRequest],
    *,
    organization_id: uuid.UUID,
    auth: Any = None,
    session_id: Optional[uuid.UUID] = None,
) -> List[EvidenceResult]:
    """Execute validated, permission-checked, read-only evidence lookups.

    * the organization id is injected HERE and is never taken from the model;
    * a kind whose gating read-only tool the role may not use is DENIED (the
      denial is returned as evidence, so the model knows it cannot inspect
      that area of the books);
    * loader failures degrade to an ``error`` on that result only;
    * every result set is bounded (``MAX_RECORDS_PER_KIND``).
    """
    from app.permissions import authorize_tool

    results: List[EvidenceResult] = []

    async def _one(request: EvidenceRequest) -> EvidenceResult:
        spec = EVIDENCE_KINDS.get((request.kind or "").strip().lower())
        if spec is None:
            message = (
                validate_evidence_request(request) or "Unknown evidence kind."
            )
            return EvidenceResult(
                kind=request.kind,
                title="rejected",
                why=request.why,
                error=message,
                rejected=True,
                source="policy",
            )
        if auth is not None:
            permitted = await authorize_tool(spec.permission_slug, auth=auth)
            if not permitted:
                return EvidenceResult(
                    kind=spec.kind,
                    title=spec.title,
                    why=request.why,
                    error=(
                        f"Permission denied for '{spec.permission_slug}' — this "
                        "role may not inspect this area of the books."
                    ),
                    rejected=True,
                    source=spec.permission_slug,
                )
        try:
            records = await spec.loader(organization_id, **(request.args or {}))
        except Exception as exc:  # noqa: BLE001 — evidence must never raise
            log.warning(
                "books_evidence.reader_failed",
                kind=spec.kind,
                error=str(exc)[:200],
                session_id=str(session_id) if session_id else None,
            )
            return EvidenceResult(
                kind=spec.kind,
                title=spec.title,
                why=request.why,
                error=f"Lookup failed: {str(exc)[:200]}",
                source=spec.kind,
            )
        records = [r for r in (records or []) if isinstance(r, dict)]
        truncated = len(records) > MAX_RECORDS_PER_KIND
        return EvidenceResult(
            kind=spec.kind,
            title=spec.title,
            why=request.why,
            records=records[:MAX_RECORDS_PER_KIND],
            source=spec.kind,
            truncated=truncated,
        )

    gathered = await asyncio.gather(*(_one(r) for r in requests))
    results.extend(gathered)
    log.info(
        "books_evidence.gathered",
        session_id=str(session_id) if session_id else None,
        requested=len(requests),
        returned=len(results),
        empty=sum(1 for r in results if r.empty),
        errors=sum(1 for r in results if not r.ok),
    )
    return results


# ---------------------------------------------------------------------------
# Rendering — deterministic facts, clearly labelled as LIVE BOOKS EVIDENCE
# ---------------------------------------------------------------------------


def _render_records(records: Sequence[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for record in records:
        rendered = json.dumps(record, default=str, ensure_ascii=False)
        if len(rendered) > MAX_RECORD_CHARS:
            rendered = rendered[:MAX_RECORD_CHARS] + "… (truncated)"
        lines.append(f"      · {rendered}")
    return lines


def render_evidence(results: Sequence[EvidenceResult]) -> str:
    """Render gathered evidence for the prompt, with its provenance label."""
    if not results:
        return ""
    blocks: List[str] = []
    for result in results:
        header = f"  [{result.kind}] {result.title}"
        if result.why:
            header += f" — requested because: {result.why}"
        if result.rejected:
            blocks.append(f"{header}\n    REFUSED: {result.error}")
            continue
        if result.error:
            blocks.append(f"{header}\n    LOOKUP FAILED: {result.error}")
            continue
        if result.empty:
            blocks.append(
                f"{header}\n    EMPTY — no matching records exist in these books "
                "(this is information, not an error: do not assume the record exists)."
            )
            continue
        blocks.append(header)
        blocks.extend(_render_records(result.records))
        if result.truncated:
            blocks.append(
                f"      · … more rows exist than shown ({MAX_RECORDS_PER_KIND} max)"
            )
    return "\n".join([f"{EVIDENCE_LABEL}:", *blocks])


def render_evidence_compact(results: Sequence[EvidenceResult]) -> str:
    """One-line-per-kind summary for audit steps and logs."""
    parts: List[str] = []
    for result in results:
        if result.rejected:
            state = "refused"
        elif result.error:
            state = "failed"
        elif result.empty:
            state = "empty"
        else:
            state = f"{len(result.records)} row(s)"
        parts.append(f"{result.kind}={state}")
    return "; ".join(parts)