"""
ERP AI Agent — Deterministic Accounting Engine
==================================================
Provides authoritative accounting logic.  **Never** depends on Gemini
to calculate debit/credit or verify balance.

Responsibilities:
* Resolve accounts
* Construct journal lines from transaction data
* Validate balance (total_debit == total_credit)
* Link source documents
* Link customer/supplier/project dimensions
* Respect accounting periods
* Handle reversals

Mandatory invariant:  TOTAL DEBIT == TOTAL CREDIT
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Dict, List, Optional

import structlog

from app.services import accounting_service
from app.services import party_ledger_service
from app.repositories import account_repository as a_repo

log = structlog.get_logger(__name__)


# ===================================================================
# High-level transaction builders
# ===================================================================


async def record_credit_purchase(
    *,
    organization_id: uuid.UUID,
    supplier_id: uuid.UUID,
    asset_account_id: uuid.UUID,
    payable_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record a credit purchase journal.

    Debit:  Asset / Expense account
    Credit: Accounts Payable (supplier)
    """
    lines = [
        {
            "account_id": str(asset_account_id),
            "description": description,
            "debit": amount,
            "credit": 0,
            "supplier_id": str(supplier_id),
            "project_id": str(project_id) if project_id else None,
        },
        {
            "account_id": str(payable_account_id),
            "description": f"Payable — {description}",
            "debit": 0,
            "credit": amount,
            "supplier_id": str(supplier_id),
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="purchase_bill",
        source_id=source_id,
    )


async def record_cash_purchase(
    *,
    organization_id: uuid.UUID,
    asset_account_id: uuid.UUID,
    cash_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record a cash purchase journal.

    Debit:  Asset / Expense account
    Credit: Cash / Bank account
    """
    lines = [
        {
            "account_id": str(asset_account_id),
            "description": description,
            "debit": amount,
            "credit": 0,
        },
        {
            "account_id": str(cash_account_id),
            "description": f"Cash payment — {description}",
            "debit": 0,
            "credit": amount,
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="purchase",
        source_id=source_id,
    )


async def record_credit_sale(
    *,
    organization_id: uuid.UUID,
    customer_id: uuid.UUID,
    receivable_account_id: uuid.UUID,
    revenue_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record a credit sale journal.

    Debit:  Accounts Receivable (customer)
    Credit: Revenue account
    """
    lines = [
        {
            "account_id": str(receivable_account_id),
            "description": f"Receivable — {description}",
            "debit": amount,
            "credit": 0,
            "customer_id": str(customer_id),
        },
        {
            "account_id": str(revenue_account_id),
            "description": description,
            "debit": 0,
            "credit": amount,
            "customer_id": str(customer_id),
            "project_id": str(project_id) if project_id else None,
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="invoice",
        source_id=source_id,
    )


async def record_cash_sale(
    *,
    organization_id: uuid.UUID,
    cash_account_id: uuid.UUID,
    revenue_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
    customer_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record a cash sale journal.

    Debit:  Cash / Bank account
    Credit: Revenue account
    """
    lines = [
        {
            "account_id": str(cash_account_id),
            "description": f"Cash received — {description}",
            "debit": amount,
            "credit": 0,
            "customer_id": str(customer_id) if customer_id else None,
        },
        {
            "account_id": str(revenue_account_id),
            "description": description,
            "debit": 0,
            "credit": amount,
            "customer_id": str(customer_id) if customer_id else None,
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="cash_sale",
        source_id=source_id,
    )


async def record_expense(
    *,
    organization_id: uuid.UUID,
    expense_account_id: uuid.UUID,
    payment_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
    supplier_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record an expense journal.

    Debit:  Expense account
    Credit: Cash/Bank/Payable account
    """
    lines = [
        {
            "account_id": str(expense_account_id),
            "description": description,
            "debit": amount,
            "credit": 0,
            "supplier_id": str(supplier_id) if supplier_id else None,
        },
        {
            "account_id": str(payment_account_id),
            "description": f"Payment — {description}",
            "debit": 0,
            "credit": amount,
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="expense",
        source_id=source_id,
    )


async def record_customer_receipt(
    *,
    organization_id: uuid.UUID,
    customer_id: uuid.UUID,
    receivable_account_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record customer payment receipt.

    Debit:  Bank / Cash account
    Credit: Accounts Receivable
    """
    lines = [
        {
            "account_id": str(bank_account_id),
            "description": f"Receipt — {description}",
            "debit": amount,
            "credit": 0,
            "customer_id": str(customer_id),
        },
        {
            "account_id": str(receivable_account_id),
            "description": f"Receivable settled — {description}",
            "debit": 0,
            "credit": amount,
            "customer_id": str(customer_id),
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="receipt",
        source_id=source_id,
    )


async def record_supplier_payment(
    *,
    organization_id: uuid.UUID,
    supplier_id: uuid.UUID,
    payable_account_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record supplier payment.

    Debit:  Accounts Payable
    Credit: Bank / Cash account
    """
    lines = [
        {
            "account_id": str(payable_account_id),
            "description": f"Payable settled — {description}",
            "debit": amount,
            "credit": 0,
            "supplier_id": str(supplier_id),
        },
        {
            "account_id": str(bank_account_id),
            "description": f"Payment — {description}",
            "debit": 0,
            "credit": amount,
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="payment",
        source_id=source_id,
    )


async def record_bank_transfer(
    *,
    organization_id: uuid.UUID,
    source_bank_account_id: uuid.UUID,
    destination_bank_account_id: uuid.UUID,
    amount: float,
    transaction_date: str,
    description: str,
    source_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record an internal bank-to-bank transfer journal.

    Debit:  Destination bank GL account
    Credit: Source bank GL account
    """
    lines = [
        {
            "account_id": str(destination_bank_account_id),
            "description": f"Transfer in — {description}",
            "debit": amount,
            "credit": 0,
        },
        {
            "account_id": str(source_bank_account_id),
            "description": f"Transfer out — {description}",
            "debit": 0,
            "credit": amount,
        },
    ]
    return await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=transaction_date,
        description=description,
        lines=lines,
        source_type="bank_transfer",
        source_id=source_id,
    )


# ===================================================================
# Verification
# ===================================================================


async def verify_journal(
    organization_id: uuid.UUID, *, entry_id: uuid.UUID
) -> Dict[str, Any]:
    """Verify a journal entry is balanced and exists."""
    entry = await accounting_service.get_entry(organization_id, entry_id=entry_id)
    if not entry:
        return {"verified": False, "reason": "Journal entry not found"}

    lines = await accounting_service.get_lines(entry_id=entry_id)
    total_debit = sum(float(l.get("debit", 0)) for l in lines)
    total_credit = sum(float(l.get("credit", 0)) for l in lines)

    balanced = abs(total_debit - total_credit) < 0.01
    return {
        "verified": balanced,
        "journal_number": entry.get("journal_number"),
        "status": entry.get("status"),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "line_count": len(lines),
    }


# ===================================================================
# Auto-journal — atomic document + journal creation (C3 + C4)
# ===================================================================


async def _grouping_account_ids(organization_id: uuid.UUID) -> set:
    """Parent (grouping) account ids for an organization.

    Used so a hierarchy heading can never be resolved as a posting default.
    A lookup failure degrades to "no grouping accounts", which keeps the
    previous behaviour rather than blocking the posting.
    """
    try:
        return await a_repo.get_grouping_account_ids(organization_id)
    except Exception as exc:  # noqa: BLE001 — never block a posting on this
        log.warning("accounting_engine.grouping_lookup_failed", error=str(exc)[:200])
        return set()


async def _resolve_default_account(
    organization_id: uuid.UUID,
    account_type: str,
    *,
    fallback_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a default account of the given type — deterministically.

    EXPENSE accounts are special: an arbitrary "first expense account"
    default debited a chair purchase to 6010 Salaries (live defect).  For
    EXPENSE the default MUST be a genuinely GENERIC account (general /
    other / miscellaneous / sundry / operating in its name); when none
    exists we return None so the caller surfaces an honest warning instead
    of silently posting to a wrong expense category.  Other types (cash,
    payable, revenue) keep the first-of-type convention — conventional
    control accounts, low mis-classification risk.

    GROUPING accounts are never postable: since the chart of accounts is
    hierarchical, a parent (e.g. "Property, Plant & Equipment") is a
    heading, not an account that can carry a posting.  Any account that is
    the parent of another active account is skipped, so introducing
    hierarchy can never hijack a default.
    """
    accounts = await a_repo.get_chart_of_accounts(
        organization_id, account_type=account_type, limit=100,
    )
    if accounts:
        group_ids = await _grouping_account_ids(organization_id)
        accounts = [a for a in accounts if a.get("id") not in group_ids]

    if account_type == "EXPENSE":
        generic_terms = ("general", "other", "miscellaneous", "sundry", "operating")
        for acc in accounts:
            name = (acc.get("name") or "").lower()
            if "accumulated" in name or "depreciation" in name:
                continue
            if any(t in name for t in generic_terms):
                return acc
        log.warning(
            "accounting_engine.no_generic_expense_default",
            hint="post with an explicit account hint instead",
        )
        return None
    if accounts:
        return accounts[0]
    if fallback_type:
        fallback = await a_repo.get_chart_of_accounts(
            organization_id, account_type=fallback_type, limit=5,
        )
        if fallback:
            group_ids = await _grouping_account_ids(organization_id)
            for acc in fallback:
                if acc.get("id") not in group_ids:
                    return acc
    return None


async def auto_journal(
    *,
    organization_id: uuid.UUID,
    document_type: str,
    document: Dict[str, Any],
    amount: float,
    transaction_date: str,
    description: str,
    customer_id: Optional[uuid.UUID] = None,
    supplier_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
    account_hint_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Automatically create a journal entry for a document.

    Resolves default accounts from the chart of accounts and calls
    the appropriate accounting-engine builder.  On success the return
    dict includes ``journal_entry``; on failure it includes
    ``journal_warning`` but the document itself is still returned
    (C4: document creation is not rolled back).
    """
    source_id = uuid.UUID(document["id"]) if document.get("id") else None

    # --- Account hint validation (classification → engine hand-off) -------
    # The hint comes from the deterministic classifier (or the model) and is
    # VALIDATED here: it must exist, belong to this organisation, be active,
    # and be of an acceptable type for the debit side.  An invalid hint is
    # ignored (falls back to the engine default) — it can never bypass the
    # engine's authority.
    hint_account: Optional[Dict[str, Any]] = None
    if account_hint_id:
        try:
            hint_account = await a_repo.get_account(
                organization_id, account_id=account_hint_id
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("accounting_engine.hint_lookup_failed", error=str(exc))
        if not hint_account:
            log.warning(
                "accounting_engine.hint_rejected",
                reason="account not found in this organisation",
                account_hint_id=str(account_hint_id),
            )
            hint_account = None
        elif not hint_account.get("is_active", True):
            log.warning(
                "accounting_engine.hint_rejected",
                reason="account is inactive",
                account_hint_id=str(account_hint_id),
            )
            hint_account = None

    try:
        if document_type == "invoice":
            # PARTY SEGREGATION: post to the CUSTOMER'S OWN receivable ledger
            # when it exists - a child of the AR control account, created when
            # the customer is added (app/services/party_ledger_service.py) - so
            # the chart itself shows what each party owes.  The control account
            # stays the fallback for parties that have no ledger yet.
            #
            # The resolution is READ-ONLY: a posting never grows the chart, and
            # a party link pointing at a deactivated/foreign account is ignored.
            receivable = None
            if customer_id:
                receivable = await party_ledger_service.resolve_customer_receivable_account(
                    organization_id, customer_id
                )
            if not receivable:
                # Resolve the RECEIVABLE account precisely — the first ASSET
                # account is usually Cash (1000), which would debit Cash for a
                # CREDIT sale. Live-verified defect (S5/S6 suite): JE-000041
                # debited 1000 Cash instead of 1100 Accounts Receivable.
                receivable = await accounting_service.resolve_account(
                    organization_id, account_name="Accounts Receivable"
                )
            if not receivable:
                receivable = await _resolve_default_account(
                    organization_id, "ASSET", fallback_type="CURRENT_ASSET",
                )
            revenue = await _resolve_default_account(
                organization_id, "REVENUE", fallback_type="INCOME",
            )
            if not receivable or not revenue:
                return {"journal_warning": "Cannot resolve Receivable or Revenue accounts"}
            entry = await record_credit_sale(
                organization_id=organization_id,
                customer_id=customer_id or uuid.UUID(int=0),
                receivable_account_id=uuid.UUID(receivable["id"]),
                revenue_account_id=uuid.UUID(revenue["id"]),
                amount=amount,
                transaction_date=transaction_date,
                description=description,
                source_id=source_id,
                project_id=project_id,
            )
            return {"journal_entry": entry}

        elif document_type == "purchase_bill":
            # A validated hint (from the classifier/user) may route the debit
            # to a fixed-asset account instead of the generic expense default.
            expense = hint_account if (
                hint_account and hint_account.get("account_type") in ("EXPENSE", "ASSET")
            ) else await _resolve_default_account(
                organization_id, "EXPENSE",
            )
            # PARTY SEGREGATION (payable side): the SUPPLIER'S OWN payable
            # ledger when it exists; the AP control account otherwise.
            payable = None
            if supplier_id:
                payable = await party_ledger_service.resolve_supplier_payable_account(
                    organization_id, supplier_id
                )
            if not payable:
                # Resolve the PAYABLE control account by NAME, not as a generic
                # liability default: once parties hang under 2010 it becomes a
                # HEADING, and the default resolver deliberately refuses to
                # return a heading (a heading must never carry a posting).
                payable = await accounting_service.resolve_account(
                    organization_id, account_name="Accounts Payable"
                )
            if not payable:
                payable = await _resolve_default_account(
                    organization_id, "LIABILITY", fallback_type="CURRENT_LIABILITY",
                )
            if not expense or not payable:
                return {"journal_warning": "Cannot resolve Expense or Payable accounts"}
            entry = await record_credit_purchase(
                organization_id=organization_id,
                supplier_id=supplier_id or uuid.UUID(int=0),
                asset_account_id=uuid.UUID(expense["id"]),
                payable_account_id=uuid.UUID(payable["id"]),
                amount=amount,
                transaction_date=transaction_date,
                description=description,
                source_id=source_id,
                project_id=project_id,
            )
            return {"journal_entry": entry}

        elif document_type == "expense":
            expense_acct = hint_account if (
                hint_account and hint_account.get("account_type") in ("EXPENSE", "ASSET")
            ) else await _resolve_default_account(
                organization_id, "EXPENSE",
            )
            cash_acct = await _resolve_default_account(
                organization_id, "ASSET", fallback_type="CURRENT_ASSET",
            )
            if not expense_acct or not cash_acct:
                return {"journal_warning": "Cannot resolve Expense or Cash accounts"}
            entry = await record_expense(
                organization_id=organization_id,
                expense_account_id=uuid.UUID(expense_acct["id"]),
                payment_account_id=uuid.UUID(cash_acct["id"]),
                amount=amount,
                transaction_date=transaction_date,
                description=description,
                source_id=source_id,
                supplier_id=supplier_id,
            )
            return {"journal_entry": entry}

        else:
            return {"journal_warning": f"Auto-journal not supported for {document_type}"}

    except Exception as exc:
        log.warning(
            "accounting_engine.auto_journal_failed",
            document_type=document_type,
            error=str(exc),
        )
        return {"journal_warning": f"Journal creation failed: {exc}"}
