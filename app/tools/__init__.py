"""
ERP AI Agent — Tool Registry
===============================
Maps the 36 registered ``ai.tools`` slugs to actual Python implementations.

Each tool handler receives ``(organization_id, **kwargs)`` and returns a
``ToolResult``.  Tools that are read-only are marked ``read_only=True``.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Coroutine, Dict, Optional

import structlog

from app.repositories import account_repository as account_repo
from app.models.schemas import ToolResult
from app.services import (
    customer_service,
    supplier_service,
    invoice_service,
    purchase_service,
    expense_service,
    payment_service,
    project_service,
    accounting_service,
    reporting_service,
    quotation_service,
    credit_note_service,
    purchase_return_service,
    bank_service,
    product_service,
    fixed_asset_service,
    service_service,
)
from datetime import date

from app.accounting_engine import auto_journal, record_cash_sale

log = structlog.get_logger(__name__)

# Type alias for a tool handler
ToolHandler = Callable[..., Coroutine[Any, Any, ToolResult]]

# Registry: slug -> (handler, read_only, description)
_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register(slug: str, *, handler: ToolHandler, read_only: bool = False, description: str = ""):
    """Register a tool handler."""
    _TOOL_REGISTRY[slug] = {
        "handler": handler,
        "read_only": read_only,
        "description": description,
    }


def get_handler(slug: str) -> Dict[str, Any] | None:
    """Return the registered entry for *slug*, or None."""
    return _TOOL_REGISTRY.get(slug)


def list_tools() -> list[str]:
    """Return all registered tool slugs."""
    return list(_TOOL_REGISTRY.keys())


# ===================================================================
# CUSTOMER TOOLS
# ===================================================================

async def _search_customer(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await customer_service.search(organization_id, query=kw.get("query", ""), limit=kw.get("limit", 25))
    return ToolResult(tool_name="search_customer", success=True, data=data)

async def _get_customer(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await customer_service.get(organization_id, customer_id=uuid.UUID(kw["customer_id"]))
    return ToolResult(tool_name="get_customer", success=True, data=data)

async def _create_customer(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await customer_service.create(organization_id, **kw)
    return ToolResult(tool_name="create_customer", success=True, data=data)

async def _get_customer_ledger(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await customer_service.get_ledger(organization_id, customer_id=uuid.UUID(kw["customer_id"]))
    return ToolResult(tool_name="get_customer_ledger", success=True, data=data)

# ===================================================================
# SUPPLIER TOOLS
# ===================================================================

async def _search_supplier(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await supplier_service.search(organization_id, query=kw.get("query", ""), limit=kw.get("limit", 25))
    return ToolResult(tool_name="search_supplier", success=True, data=data)

async def _get_supplier(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await supplier_service.get(organization_id, supplier_id=uuid.UUID(kw["supplier_id"]))
    return ToolResult(tool_name="get_supplier", success=True, data=data)

async def _create_supplier(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await supplier_service.create(organization_id, **kw)
    return ToolResult(tool_name="create_supplier", success=True, data=data)

async def _get_supplier_ledger(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await supplier_service.get_ledger(organization_id, supplier_id=uuid.UUID(kw["supplier_id"]))
    return ToolResult(tool_name="get_supplier_ledger", success=True, data=data)

# ===================================================================
# ACCOUNT TOOLS
# ===================================================================

async def _search_account(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await account_repo.search_accounts(organization_id, query=kw.get("query", ""))
    return ToolResult(tool_name="search_account", success=True, data=data)

async def _get_chart_of_accounts(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await account_repo.get_chart_of_accounts(organization_id, account_type=kw.get("account_type"))
    return ToolResult(tool_name="get_chart_of_accounts", success=True, data=data)

async def _create_account(organization_id: uuid.UUID, **kw) -> ToolResult:
    # Explicit account-creation requests MUST NOT fail on a colliding code
    # (live defect: the model suggested 6150 which already exists, the
    # unique constraint fired, and the operation silently degraded to the
    # default expense account).  Resolve a free code in the same numbering
    # series BEFORE the insert; the DB unique constraint remains the
    # last line of defence.
    requested_code = str(kw.get("code") or "")
    requested_name = str(kw.get("name") or "").strip()

    # Search-before-create (P2): an exact (case/trim-insensitive) name match
    # means the requested ledger already exists — REUSE it.  The unique
    # constraint only guards (organization_id, code); without this check an
    # explicit "create Rent Expense - Building A" request could create a
    # second ledger with the same name under a different code.
    if requested_name:
        try:
            candidates = await account_repo.search_accounts(
                organization_id, query=requested_name, limit=25
            )
        except Exception as exc:  # noqa: BLE001 — search is best-effort
            log.warning("tools.create_account.search_failed", error=str(exc)[:200])
            candidates = []
        exact = [
            c for c in candidates
            if (c.get("name") or "").strip().lower() == requested_name.lower()
        ]
        if exact:
            existing = exact[0]
            log.info(
                "tools.create_account.reused_existing",
                name=requested_name,
                code=existing.get("code"),
            )
            return ToolResult(
                tool_name="create_account",
                success=True,
                data={
                    **existing,
                    "reused_existing": True,
                    "note": (
                        f"An account named '{requested_name}' already exists "
                        f"(code {existing.get('code')}) — it was reused; no "
                        "duplicate ledger was created."
                    ),
                },
            )

    try:
        resolved_code = await account_repo.next_available_code(
            organization_id, requested_code
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the agent
        return ToolResult(
            tool_name="create_account",
            success=False,
            error=(
                f"Could not resolve a free account code near '{requested_code}': {exc}. "
                "Ask the user which account code series to use."
            ),
        )
    if resolved_code != requested_code:
        log.info(
            "tools.create_account.code_resolved",
            requested=requested_code,
            resolved=resolved_code,
        )
        kw = {**kw, "code": resolved_code}
    data = await account_repo.create_account(organization_id=organization_id, **kw)
    if resolved_code != requested_code:
        data["code_resolved_from"] = requested_code
    return ToolResult(tool_name="create_account", success=True, data=data)


async def _record_cash_sale(organization_id: uuid.UUID, **kw) -> ToolResult:
    """Trusted cash-sale domain operation.

    The model NEVER assembles this journal manually: the tool resolves the
    Cash and Revenue accounts deterministically from the organisation's
    chart of accounts, delegates journal construction to the accounting
    engine, and auto-validates + auto-posts (same convention as
    create_expense / create_purchase_bill).  Session-level duplicate
    protection lives in the tool router.
    """
    amount = float(kw["amount"])
    transaction_date = kw.get("transaction_date") or date.today().isoformat()
    description = kw.get("description") or f"Cash sale {amount:,.2f}"
    log.info(
        "sale_tool_started",
        organization_id=str(organization_id),
        tool="record_cash_sale",
        amount=amount,
    )

    cash = await accounting_service.resolve_account(
        organization_id, account_name="Cash"
    )
    if not cash:
        cash = await accounting_service.resolve_account(
            organization_id, account_name="Cash Account"
        )
    if not cash:
        assets = await account_repo.get_chart_of_accounts(
            organization_id, account_type="ASSET", limit=5
        )
        cash = assets[0] if assets else None
    # ---- REVENUE ACCOUNT -------------------------------------------------
    # DIRECTIVE: never "the first REVENUE account in the chart".  See
    # app/services/revenue_ledger_service.py — an unresolved revenue stream is
    # a QUESTION for the user, not a coin toss.  The previous fuzzy fallback
    # credited a MOBILE PHONE sale to *Software Development Revenue* purely
    # because that account sorted first in a software house's chart.
    from app.services import revenue_ledger_service

    # A REVIEWED account is authoritative: the user approved a dedicated stream
    # ledger (or named an existing one), so use exactly that account.  Only when
    # nothing was reviewed does the service resolve one deliberately.
    revenue = None
    revenue_source = "reviewed"
    stream = revenue_ledger_service.stream_label(description)
    override_id = kw.get("revenue_account_id")
    if override_id:
        rows = await account_repo.get_chart_of_accounts(
            organization_id, account_type="REVENUE", limit=200
        )
        revenue = next(
            (r for r in (rows or []) if str(r.get("id")) == str(override_id)),
            None,
        )
        if revenue is None:
            return ToolResult(
                tool_name="record_cash_sale",
                success=False,
                error=(
                    "The reviewed revenue account is not present in this "
                    "organisation's chart of accounts. Ask the user which "
                    "revenue account to use."
                ),
            )
    else:
        revenue, stream, revenue_source = (
            await revenue_ledger_service.resolve_revenue_account(
                organization_id, description
            )
        )
    if revenue is None:
        if stream:
            ask = (
                "This sale has no dedicated revenue ledger. Ask the user "
                "whether to create a dedicated revenue ledger named "
                f"'{revenue_ledger_service.suggest_ledger_name(stream)}' for "
                f"'{stream}', and which existing revenue account it should "
                "belong under — or which existing revenue account to use "
                "instead. Never choose a revenue account silently."
            )
        else:
            ask = (
                "The item sold was not specific enough to name a revenue "
                "ledger, and the chart has more than one revenue account. "
                "Ask the user which revenue account this sale belongs to."
            )
        log.warning(
            "sale_tool_failed",
            organization_id=str(organization_id),
            tool="record_cash_sale",
            stage="revenue_resolution",
        )
        return ToolResult(
            tool_name="record_cash_sale", success=False, error=ask
        )
    if not cash:
        log.warning(
            "sale_tool_failed",
            organization_id=str(organization_id),
            tool="record_cash_sale",
            stage="cash_resolution",
        )
        return ToolResult(
            tool_name="record_cash_sale",
            success=False,
            error=(
                "Cannot resolve the Cash account in the chart of accounts. "
                "Ask the user which asset account to use."
            ),
        )

    customer_id = kw.get("customer_id")
    result = await record_cash_sale(
        organization_id=organization_id,
        cash_account_id=uuid.UUID(str(cash["id"])),
        revenue_account_id=uuid.UUID(str(revenue["id"])),
        amount=amount,
        transaction_date=transaction_date,
        description=description,
        customer_id=(
            uuid.UUID(str(customer_id)) if customer_id else None
        ),
    )
    # Auto-validate + auto-post — same convention as the other trusted
    # document tools (deterministic backend calls, never an LLM decision).
    # NOTE: record_cash_sale returns prepare_journal's dict directly:
    # {"entry": <row>, "lines": [...], "total_debit", "total_credit"} —
    # the row is at result["entry"], NOT nested one level deeper.
    entry = result.get("entry") or {}
    if entry.get("id"):
        try:
            entry_id = uuid.UUID(entry["id"])
            await accounting_service.validate_journal(entry_id=entry_id)
            await accounting_service.post_journal(entry_id=entry_id)
            result["journal_posted"] = True
        except Exception as exc:  # noqa: BLE001 — never fake success
            log.warning(
                "tools.record_cash_sale.post_journal_failed",
                entry_id=str(entry.get("id")),
                error=str(exc)[:300],
            )
            result["journal_posted"] = False
            result["journal_warning"] = f"Journal prepared but posting failed: {exc}"
    result["cash_account"] = cash.get("name")
    result["revenue_account"] = revenue.get("name")
    # Audit trail: HOW the revenue account was chosen (stream / only-one), so a
    # reviewer can see it was resolved deliberately rather than by sort order.
    result["revenue_account_source"] = revenue_source
    if stream:
        result["revenue_stream"] = stream
    log.info(
        "sale_tool_succeeded",
        organization_id=str(organization_id),
        tool="record_cash_sale",
        journal_posted=bool(result.get("journal_posted")),
        revenue_account_id=str(revenue.get("id")),
        reviewed=bool(kw.get("revenue_account_id")),
    )
    return ToolResult(tool_name="record_cash_sale", success=True, data=result)

# ===================================================================
# INVOICE TOOLS
# ===================================================================

async def _create_invoice(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await invoice_service.create_invoice(organization_id=organization_id, **kw)
    # C3+C4: Atomic document + journal creation
    amount = float(data.get("total", 0))
    if amount > 0:
        journal_result = await auto_journal(
            organization_id=organization_id,
            document_type="invoice",
            document=data,
            amount=amount,
            transaction_date=data.get("invoice_date", ""),
            description=f"Invoice {data.get('invoice_number', data.get('id', ''))}",
            customer_id=kw.get("customer_id"),
            project_id=kw.get("project_id"),
        )
        data.update(journal_result)
        # Auto-validate + auto-post the invoice journal — same convention as
        # create_purchase_bill / payment_service / create_expense: a recorded
        # invoice is a real receivable event; the ledger must reflect it
        # immediately instead of leaving a DRAFT journal. Live-verified
        # defect (S5/S6 suite): invoice journals stayed DRAFT. Deterministic
        # backend calls — never an LLM decision.
        prepared = journal_result.get("journal_entry") or {}
        entry = prepared.get("entry") or {}
        if entry.get("id"):
            try:
                entry_id = uuid.UUID(entry["id"])
                await accounting_service.validate_journal(entry_id=entry_id)
                await accounting_service.post_journal(entry_id=entry_id)
                # Source-tie the journal back to the invoice row and leave
                # DRAFT — mirrors quotation_service.convert_quotation. A
                # posted invoice journal is a real receivable event: the
                # invoice must carry journal_entry_id and sit in ISSUED so
                # v_open_receivables / v_customer_aging reflect it (they
                # filter out DRAFT). Live-verified defect (INV-000001):
                # journal POSTED but invoice stayed DRAFT with a NULL
                # journal_entry_id, so the ledger showed a receivable the
                # aging views hid.
                from app.repositories import invoice_repository as inv_repo
                await inv_repo.link_journal_to_invoice(
                    invoice_id=uuid.UUID(str(data["id"])),
                    journal_entry_id=entry_id,
                )
                await inv_repo.mark_issued(
                    invoice_id=uuid.UUID(str(data["id"]))
                )
                data["journal_entry_id"] = str(entry_id)
                data["status"] = "ISSUED"
                data["journal_posted"] = True
            except Exception as exc:  # noqa: BLE001 — never fake success
                log.warning(
                    "tools.create_invoice.post_journal_failed",
                    entry_id=str(entry.get("id")),
                    error=str(exc)[:300],
                )
                data["journal_posted"] = False
                data["journal_warning"] = (
                    f"Journal prepared but posting failed: {exc}"
                )
    return ToolResult(tool_name="create_invoice", success=True, data=data)

async def _get_invoice(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await invoice_service.get_invoice(organization_id, invoice_id=uuid.UUID(kw["invoice_id"]))
    return ToolResult(tool_name="get_invoice", success=True, data=data)

# ===================================================================
# PURCHASE TOOLS
# ===================================================================

def _safe_account_hint(value: Any) -> Optional[uuid.UUID]:
    """Parse an optional account hint (account_id) from tool arguments."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def _create_purchase_bill(organization_id: uuid.UUID, **kw) -> ToolResult:
    # account_id is a JOURNAL hint (validated by the accounting engine),
    # not a bill column — pop it before the service call.
    account_hint = _safe_account_hint(kw.pop("account_id", None))
    data = await purchase_service.create_purchase_bill(organization_id=organization_id, **kw)
    # C3+C4: Atomic document + journal creation
    amount = float(data.get("total", 0))
    if amount > 0:
        journal_result = await auto_journal(
            organization_id=organization_id,
            document_type="purchase_bill",
            document=data,
            amount=amount,
            transaction_date=data.get("bill_date", ""),
            description=f"Purchase bill {data.get('bill_number', data.get('id', ''))}",
            supplier_id=kw.get("supplier_id"),
            account_hint_id=account_hint,
        )
        data.update(journal_result)
        # Auto-validate + auto-post the bill journal — same convention as
        # payment_service (receipts/payments) and create_expense: a recorded
        # bill is a real payable event; the ledger must reflect it
        # immediately instead of leaving a DRAFT journal that verification
        # would still call "balanced → VERIFIED". Deterministic backend
        # calls — never an LLM decision.
        # prepare_journal returns {"entry": <row>, ...} — DB contract first.
        prepared = journal_result.get("journal_entry") or {}
        entry = prepared.get("entry") or {}
        if entry.get("id"):
            try:
                entry_id = uuid.UUID(entry["id"])
                await accounting_service.validate_journal(entry_id=entry_id)
                await accounting_service.post_journal(entry_id=entry_id)
                data["journal_posted"] = True
            except Exception as exc:  # noqa: BLE001 — never fake success
                log.warning(
                    "tools.create_purchase_bill.post_journal_failed",
                    entry_id=str(entry.get("id")),
                    error=str(exc)[:300],
                )
                data["journal_posted"] = False
                data["journal_warning"] = (
                    f"Journal prepared but posting failed: {exc}"
                )
    return ToolResult(tool_name="create_purchase_bill", success=True, data=data)

async def _get_purchase_bill(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await purchase_service.get_purchase_bill(organization_id, bill_id=uuid.UUID(kw["bill_id"]))
    return ToolResult(tool_name="get_purchase_bill", success=True, data=data)

# ===================================================================
# EXPENSE TOOLS
# ===================================================================

async def _create_expense(organization_id: uuid.UUID, **kw) -> ToolResult:
    # account_id is a JOURNAL hint (validated by the accounting engine),
    # not an expense column — pop it before the service call.
    account_hint = _safe_account_hint(kw.pop("account_id", None))
    data = await expense_service.create_expense(organization_id=organization_id, **kw)
    # C3+C4: Atomic document + journal creation
    amount = float(data.get("total", 0) or data.get("subtotal", 0))
    if amount > 0:
        journal_result = await auto_journal(
            organization_id=organization_id,
            document_type="expense",
            document=data,
            amount=amount,
            transaction_date=data.get("expense_date", ""),
            description=data.get("description", f"Expense {data.get('id', '')}"),
            supplier_id=kw.get("supplier_id"),
            account_hint_id=account_hint,
        )
        data.update(journal_result)
        # Auto-validate + auto-post the expense journal — the SAME
        # convention payment_service already implements for receipts and
        # payments: a confirmed expense is a final financial event, so
        # the ledger must reflect it immediately.  Without this the
        # journal stayed DRAFT while the session claimed VERIFIED.
        # Deterministic backend calls — never an LLM decision.
        # prepare_journal returns {"entry": <row>, "lines": [...]} —
        # the entry row is nested under "entry" (mirrors payment_service).
        prepared = journal_result.get("journal_entry") or {}
        entry = prepared.get("entry") or {}
        if entry.get("id"):
            try:
                entry_id = uuid.UUID(entry["id"])
                await accounting_service.validate_journal(entry_id=entry_id)
                await accounting_service.post_journal(entry_id=entry_id)
                data["journal_posted"] = True
            except Exception as exc:  # noqa: BLE001 — posting failure must
                # not fake success: surface it as a warning; verification
                # will re-read the actual status from the database.
                log.warning(
                    "tools.create_expense.post_journal_failed",
                    entry_id=str(entry.get("id")),
                    error=str(exc)[:300],
                )
                data["journal_posted"] = False
                data["journal_warning"] = (
                    f"Journal prepared but posting failed: {exc}"
                )
        else:
            log.warning(
                "tools.create_expense.journal_entry_missing",
                keys=list(journal_result.keys()),
            )
    return ToolResult(tool_name="create_expense", success=True, data=data)

async def _classify_expense(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await expense_service.classify_expense(description=kw.get("description", ""), amount=kw.get("amount", 0))
    return ToolResult(tool_name="classify_expense", success=True, data=data)

# ===================================================================
# PAYMENT TOOLS
# ===================================================================

async def _record_customer_receipt(organization_id: uuid.UUID, **kw) -> ToolResult:
    # Wire invoice_id / bill_id through if present
    invoice_id = kw.pop("invoice_id", None)
    if invoice_id:
        kw["invoice_id"] = uuid.UUID(invoice_id)
    data = await payment_service.record_customer_receipt(organization_id=organization_id, **kw)
    return ToolResult(tool_name="record_customer_receipt", success=True, data=data)

async def _record_supplier_payment(organization_id: uuid.UUID, **kw) -> ToolResult:
    bill_id = kw.pop("bill_id", None)
    if bill_id:
        kw["bill_id"] = uuid.UUID(bill_id)
    data = await payment_service.record_supplier_payment(organization_id=organization_id, **kw)
    return ToolResult(tool_name="record_supplier_payment", success=True, data=data)

# ===================================================================
# BANK TOOLS
# ===================================================================

async def _list_bank_accounts(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await bank_service.list_bank_accounts(organization_id, is_active=kw.get("is_active"))
    return ToolResult(tool_name="list_bank_accounts", success=True, data=data)

async def _create_bank_account(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await bank_service.create_bank_account(organization_id=organization_id, **kw)
    return ToolResult(tool_name="create_bank_account", success=True, data=data)

async def _record_bank_transfer(organization_id: uuid.UUID, **kw) -> ToolResult:
    # Resolve bank account UUIDs from names if needed
    src = kw.get("source_bank_account_id")
    dst = kw.get("destination_bank_account_id")
    if src and not _is_uuid(src):
        resolved = await bank_service.resolve_bank_account(organization_id, name=src)
        if not resolved:
            return ToolResult(tool_name="record_bank_transfer", success=False, error=f"Cannot resolve source bank account: {src}")
        kw["source_bank_account_id"] = resolved["id"]
    if dst and not _is_uuid(dst):
        resolved = await bank_service.resolve_bank_account(organization_id, name=dst)
        if not resolved:
            return ToolResult(tool_name="record_bank_transfer", success=False, error=f"Cannot resolve destination bank account: {dst}")
        kw["destination_bank_account_id"] = resolved["id"]
    data = await payment_service.record_bank_transfer(organization_id=organization_id, **kw)
    return ToolResult(tool_name="record_bank_transfer", success=True, data=data)


def _is_uuid(val) -> bool:
    """Check if a string is a valid UUID."""
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError):
        return False

# ===================================================================
# JOURNAL TOOLS
# ===================================================================

async def _prepare_journal(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await accounting_service.prepare_journal(organization_id=organization_id, **kw)
    return ToolResult(tool_name="prepare_journal", success=True, data=data)

async def _validate_journal(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await accounting_service.validate_journal(entry_id=uuid.UUID(kw["entry_id"]))
    return ToolResult(tool_name="validate_journal", success=True, data=data)

async def _post_journal(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await accounting_service.post_journal(entry_id=uuid.UUID(kw["entry_id"]))
    return ToolResult(tool_name="post_journal", success=True, data=data)

async def _reverse_journal(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await accounting_service.reverse_journal(entry_id=uuid.UUID(kw["entry_id"]), reason=kw.get("reason"))
    return ToolResult(tool_name="reverse_journal", success=True, data=data)

# ===================================================================
# REPORTING TOOLS
# ===================================================================

async def _get_general_ledger(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await reporting_service.get_general_ledger(organization_id, **kw)
    return ToolResult(tool_name="get_general_ledger", success=True, data=data)

async def _get_trial_balance(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await reporting_service.get_trial_balance(organization_id)
    return ToolResult(tool_name="get_trial_balance", success=True, data=data)

async def _get_profit_loss(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await reporting_service.get_income_statement(organization_id)
    return ToolResult(tool_name="get_profit_loss", success=True, data=data)

async def _get_balance_sheet(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await reporting_service.get_balance_sheet(organization_id)
    return ToolResult(tool_name="get_balance_sheet", success=True, data=data)

async def _get_cash_flow(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await reporting_service.get_cash_flow(organization_id)
    return ToolResult(tool_name="get_cash_flow", success=True, data=data)

async def _list_expenses(organization_id: uuid.UUID, **kw) -> ToolResult:
    """List recorded expenses (read-only).  Supports optional
    ``from_date``/``to_date`` (ISO) — used by the deterministic
    expense-details fast path ("details of this month's expenses")."""
    from app.repositories import expense_repository

    rows = await expense_repository.list_expenses(organization_id, limit=100)
    from_date = kw.get("from_date")
    to_date = kw.get("to_date")
    if from_date:
        rows = [r for r in rows if (r.get("expense_date") or "") >= str(from_date)]
    if to_date:
        rows = [r for r in rows if (r.get("expense_date") or "") <= str(to_date)]
    return ToolResult(tool_name="list_expenses", success=True, data=rows)

async def _generate_report(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await reporting_service.generate_report(organization_id, **kw)
    return ToolResult(tool_name="generate_report", success=True, data=data)

# ===================================================================
# PROJECT TOOLS
# ===================================================================

async def _create_project(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await project_service.create(organization_id, **kw)
    return ToolResult(tool_name="create_project", success=True, data=data)

async def _get_project(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await project_service.get(organization_id, project_id=uuid.UUID(kw["project_id"]))
    return ToolResult(tool_name="get_project", success=True, data=data)

async def _get_project_profitability(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await project_service.get_profitability(organization_id, project_id=uuid.UUID(kw["project_id"]))
    return ToolResult(tool_name="get_project_profitability", success=True, data=data)

# ===================================================================
# QUOTATION TOOLS
# ===================================================================

async def _create_quotation(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await quotation_service.create_quotation(organization_id=organization_id, **kw)
    return ToolResult(tool_name="create_quotation", success=True, data=data)

async def _convert_quotation(organization_id: uuid.UUID, **kw) -> ToolResult:
    # Quotation → Invoice cross-module lifecycle: creates the invoice with
    # quotation source linkage, posts the receivable journal via the
    # deterministic accounting engine, then marks the quotation CONVERTED.
    if kw.get("quotation_id") and not isinstance(kw["quotation_id"], uuid.UUID):
        kw["quotation_id"] = uuid.UUID(str(kw["quotation_id"]))
    data = await quotation_service.convert_quotation(organization_id=organization_id, **kw)
    return ToolResult(tool_name="convert_quotation", success=True, data=data)

# ===================================================================
# CREDIT NOTE TOOLS
# ===================================================================

async def _create_credit_note(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await credit_note_service.create_credit_note(organization_id=organization_id, **kw)
    return ToolResult(tool_name="create_credit_note", success=True, data=data)

# ===================================================================
# PURCHASE RETURN TOOLS
# ===================================================================

async def _create_purchase_return(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await purchase_return_service.create_purchase_return(organization_id=organization_id, **kw)
    return ToolResult(tool_name="create_purchase_return", success=True, data=data)

# ===================================================================
# EXPENSE PAYMENT TOOL
# ===================================================================

async def _record_expense_payment(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await payment_service.record_supplier_payment(organization_id=organization_id, **kw)
    return ToolResult(tool_name="record_expense_payment", success=True, data=data)

# ===================================================================
# REGISTER ALL TOOLS
# ===================================================================

# Customer
register("search_customer", handler=_search_customer, read_only=True, description="Search customers by name or code")
register("get_customer", handler=_get_customer, read_only=True, description="Get customer details")
register("create_customer", handler=_create_customer, read_only=False, description="Create a new customer")
register("get_customer_ledger", handler=_get_customer_ledger, read_only=True, description="Get customer ledger")

# Supplier
register("search_supplier", handler=_search_supplier, read_only=True, description="Search suppliers by name or code")
register("get_supplier", handler=_get_supplier, read_only=True, description="Get supplier details")
register("create_supplier", handler=_create_supplier, read_only=False, description="Create a new supplier")
register("get_supplier_ledger", handler=_get_supplier_ledger, read_only=True, description="Get supplier ledger")

# Account
register("search_account", handler=_search_account, read_only=True, description="Search chart of accounts")
register("get_chart_of_accounts", handler=_get_chart_of_accounts, read_only=True, description="Get full chart of accounts")
register("create_account", handler=_create_account, read_only=False, description="Create a new account")

# Invoice
register("create_invoice", handler=_create_invoice, read_only=False, description="Create a sales invoice with line items: pass items=[{description, quantity, unit_price, ...}] — lines are validated and their totals computed by the service (never invent amounts)")
register("get_invoice", handler=_get_invoice, read_only=True, description="Get invoice details")

# Purchase
register("create_purchase_bill", handler=_create_purchase_bill, read_only=False, description="Create a purchase bill")
register("get_purchase_bill", handler=_get_purchase_bill, read_only=True, description="Get purchase bill details")

# Expense
register("create_expense", handler=_create_expense, read_only=False, description="Record an expense")
register("classify_expense", handler=_classify_expense, read_only=True, description="Classify expense category")

# Payment
register("record_customer_receipt", handler=_record_customer_receipt, read_only=False, description="Record customer receipt with journal and allocation")
register("record_supplier_payment", handler=_record_supplier_payment, read_only=False, description="Record supplier payment with journal and allocation")

# Bank
register("list_bank_accounts", handler=_list_bank_accounts, read_only=True, description="List bank accounts")
register("create_bank_account", handler=_create_bank_account, read_only=False, description="Create a new bank account with linked GL account")
register("record_bank_transfer", handler=_record_bank_transfer, read_only=False, description="Record bank-to-bank transfer with journal")

# Journal
register("prepare_journal", handler=_prepare_journal, read_only=False, description="Prepare a draft journal entry")
register("validate_journal", handler=_validate_journal, read_only=False, description="Validate a journal entry")
register("post_journal", handler=_post_journal, read_only=False, description="Post a validated journal entry")
register("reverse_journal", handler=_reverse_journal, read_only=False, description="Reverse a posted journal entry")

# Sales — trusted cash-sale domain operation (deterministic journal)
register("record_cash_sale", handler=_record_cash_sale, read_only=False, description="Record an immediate cash sale: resolves the Cash and Revenue accounts, prepares, validates and posts the journal via the accounting engine")

# Reporting
register("get_general_ledger", handler=_get_general_ledger, read_only=True, description="Get general ledger")
register("get_trial_balance", handler=_get_trial_balance, read_only=True, description="Get trial balance")
register("get_profit_loss", handler=_get_profit_loss, read_only=True, description="Get profit & loss statement")
register("get_balance_sheet", handler=_get_balance_sheet, read_only=True, description="Get balance sheet")
register("get_cash_flow", handler=_get_cash_flow, read_only=True, description="Get cash flow data")
register("list_expenses", handler=_list_expenses, read_only=True, description="List recorded expenses with dates, payees and totals (supports from_date/to_date)")
register("generate_report", handler=_generate_report, read_only=True, description="Generate a custom report")

# Project
register("create_project", handler=_create_project, read_only=False, description="Create a project")
register("get_project", handler=_get_project, read_only=True, description="Get project details")
register("get_project_profitability", handler=_get_project_profitability, read_only=True, description="Get project profitability")

# Quotation
register("create_quotation", handler=_create_quotation, read_only=False, description="Create a sales quotation")
register("convert_quotation", handler=_convert_quotation, read_only=False, description="Convert an accepted quotation into a sales invoice: creates the invoice linked to the source quotation, posts the receivable journal, and marks the quotation CONVERTED (double conversion is refused)")

# Credit Note
register("create_credit_note", handler=_create_credit_note, read_only=False, description="Create a credit note")

# Purchase Return
register("create_purchase_return", handler=_create_purchase_return, read_only=False, description="Create a purchase return")

# Expense Payment
register("record_expense_payment", handler=_record_expense_payment, read_only=False, description="Record payment for an expense or bill")

# ===================================================================
# PRODUCT CATALOG (item resolution — NO stock ledger exists in the DB)
# ===================================================================

async def _search_product(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await product_service.search(organization_id, query=kw.get("query", ""), limit=kw.get("limit", 25))
    return ToolResult(tool_name="search_product", success=True, data=data)

async def _create_product(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await product_service.create(organization_id, **kw)
    return ToolResult(tool_name="create_product", success=True, data=data)

async def _search_service(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await service_service.search(organization_id, query=kw.get("query", ""), limit=kw.get("limit", 25))
    return ToolResult(tool_name="search_service", success=True, data=data)

async def _create_service(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await service_service.create(organization_id, **kw)
    return ToolResult(tool_name="create_service", success=True, data=data)

# ===================================================================
# FIXED ASSET LIFECYCLE (acquisition → depreciation → disposal/sale)
# ===================================================================

async def _search_fixed_asset(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await fixed_asset_service.search(organization_id, query=kw.get("query", ""), limit=kw.get("limit", 25))
    return ToolResult(tool_name="search_fixed_asset", success=True, data=data)

async def _get_fixed_asset(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await fixed_asset_service.get(organization_id, asset_id=uuid.UUID(kw["asset_id"]))
    return ToolResult(tool_name="get_fixed_asset", success=True, data=data)

async def _register_fixed_asset(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await fixed_asset_service.register_asset(organization_id, **kw)
    return ToolResult(tool_name="register_fixed_asset", success=True, data=data)

async def _dispose_fixed_asset(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await fixed_asset_service.dispose_asset(organization_id, **kw)
    return ToolResult(tool_name="dispose_fixed_asset", success=True, data=data)

async def _record_asset_depreciation(organization_id: uuid.UUID, **kw) -> ToolResult:
    data = await fixed_asset_service.record_depreciation(organization_id, **kw)
    return ToolResult(tool_name="record_asset_depreciation", success=True, data=data)

# Product catalog
register("search_product", handler=_search_product, read_only=True, description="Search the product catalog by name (empty result = no product exists yet)")
register("create_product", handler=_create_product, read_only=False, description="Create a catalog product (catalog only — the ERP has no stock-quantity ledger). Set is_stock_tracked=true ONLY when the user explicitly answered the inventory question confirming resale stock — never infer it")

# Service catalog
register("search_service", handler=_search_service, read_only=True, description="Search the service catalog by name (empty result = no service exists yet)")
register("create_service", handler=_create_service, read_only=False, description="Create a catalog service (billing unit HOUR/DAY/MONTH/FIXED/ITEM — services never involve stock or assets)")

# Fixed asset lifecycle
register("search_fixed_asset", handler=_search_fixed_asset, read_only=True, description="Search registered fixed assets by name or code (empty result = asset not registered)")
register("get_fixed_asset", handler=_get_fixed_asset, read_only=True, description="Get fixed asset details by id")
register("register_fixed_asset", handler=_register_fixed_asset, read_only=False, description="Acquire and capitalise a fixed asset: creates the asset record and posts the acquisition journal (Dr asset / Cr cash-bank or payable)")
register("dispose_fixed_asset", handler=_dispose_fixed_asset, read_only=False, description="Dispose of / sell / write off a fixed asset: removes cost and accumulated depreciation, records proceeds and any gain or loss via a posted journal")
register("record_asset_depreciation", handler=_record_asset_depreciation, read_only=False, description="Record asset depreciation: posts the depreciation journal and updates accumulated depreciation and book value")



# ===================================================================
# FINANCIAL HEALTH SCORING
# ===================================================================

async def _calculate_health(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.health_service import calculate_health
    data = await calculate_health(organization_id)
    return ToolResult(tool_name="calculate_health", success=True, data=data)

async def _get_health(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.health_service import get_latest_snapshot, get_history
    latest = await get_latest_snapshot(organization_id)
    history = await get_history(organization_id)
    return ToolResult(tool_name="get_health", success=True, data={"latest": latest, "history": history})

register("calculate_health", handler=_calculate_health, read_only=False, description="Calculate financial health score")
register("get_health", handler=_get_health, read_only=True, description="Get financial health history")


# ===================================================================
# RECURRING TRANSACTIONS
# ===================================================================

async def _create_recurring_template(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.recurring_service import create_template
    data = await create_template(organization_id=organization_id, **kw)
    return ToolResult(tool_name="create_recurring_template", success=True, data=data)

async def _get_recurring_templates(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.recurring_service import list_templates
    data = await list_templates(
        organization_id, status=kw.get("status"), limit=kw.get("limit", 100)
    )
    return ToolResult(tool_name="get_recurring_templates", success=True, data=data)

async def _get_recurring_template(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.recurring_service import get_template
    data = await get_template(
        organization_id, template_id=uuid.UUID(str(kw["template_id"]))
    )
    return ToolResult(tool_name="get_recurring_template", success=True, data=data)

async def _update_recurring_template(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.recurring_service import set_status
    data = await set_status(
        organization_id=organization_id,
        template_id=uuid.UUID(str(kw["template_id"])),
        status=str(kw["status"]),
    )
    return ToolResult(tool_name="update_recurring_template", success=True, data=data)

async def _generate_recurring(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.recurring_service import generate_due
    data = await generate_due(organization_id, as_of=kw.get("as_of"))
    return ToolResult(tool_name="generate_recurring", success=True, data=data)

async def _get_recurring_executions(organization_id: uuid.UUID, **kw) -> ToolResult:
    from app.services.recurring_service import list_executions
    data = await list_executions(
        organization_id,
        template_id=uuid.UUID(str(kw["template_id"])) if kw.get("template_id") else None,
        limit=kw.get("limit", 100),
    )
    return ToolResult(tool_name="get_recurring_executions", success=True, data=data)

register("create_recurring_template", handler=_create_recurring_template, read_only=False, description="Create a recurring transaction template (balanced journal lines + frequency + next due date)")
register("get_recurring_templates", handler=_get_recurring_templates, read_only=True, description="List recurring transaction templates (optionally filter by status)")
register("get_recurring_template", handler=_get_recurring_template, read_only=True, description="Get a single recurring transaction template")
register("update_recurring_template", handler=_update_recurring_template, read_only=False, description="Pause / resume / complete / cancel a recurring template")
register("generate_recurring", handler=_generate_recurring, read_only=False, description="Generate journal entries for every due recurring template (draft unless auto_post); advances next due dates")
register("get_recurring_executions", handler=_get_recurring_executions, read_only=True, description="List recurring executions (generated/failed journal runs)")
