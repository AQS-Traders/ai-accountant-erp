"""
Payment Service — business logic for customer receipts, supplier payments,
allocation against invoices/bills, and bank-to-bank transfers.

All financial mutations flow through the deterministic Accounting Engine
so that journal entries, ledger balances, and financial statements stay
consistent regardless of whether the transaction originated from AI or
the manual frontend.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.repositories import payment_repository as repo
from app.repositories import bank_repository as bank_repo
from app.repositories import invoice_repository as inv_repo
from app.repositories import purchase_repository as pur_repo
from app.database import fetch_many, update_one, call_rpc
from app.services import accounting_service
from app import accounting_engine as engine

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_bank_gl_account(
    organization_id: uuid.UUID,
    bank_account_id: Optional[uuid.UUID],
) -> uuid.UUID:
    """Resolve the GL account id for a bank_accounts row."""
    if not bank_account_id:
        # Fall back to default bank account
        default = await bank_repo.get_default_bank_account(organization_id)
        if not default:
            raise ValueError("No bank account specified and no default bank account configured")
        return uuid.UUID(default["gl_account_id"])
    ba = await bank_repo.get_bank_account(organization_id, bank_account_id=bank_account_id)
    if not ba:
        raise ValueError(f"Bank account {bank_account_id} not found")
    return uuid.UUID(ba["gl_account_id"])


async def _resolve_cash_gl_account(
    organization_id: uuid.UUID,
    cash_account_id: Optional[uuid.UUID],
) -> uuid.UUID:
    """Resolve the GL account id for a cash_accounts row (Work Stream R4.1).

    The CASH ledger is separate from the bank ledger: an explicit
    cash_account_id wins, else the org's default (first active) cash
    drawer.  A missing cash account is a CONFIGURATION GAP — raised,
    never silently replaced by the bank GL.
    """
    if not cash_account_id:
        default = await bank_repo.get_default_cash_account(organization_id)
        if not default:
            raise ValueError(
                "No cash account specified and no cash account configured "
                "(a cash transaction must hit the cash ledger, never the "
                "bank ledger)"
            )
        return uuid.UUID(default["gl_account_id"])
    ca = await bank_repo.get_cash_account(organization_id, cash_account_id=cash_account_id)
    if not ca:
        raise ValueError(f"Cash account {cash_account_id} not found")
    return uuid.UUID(ca["gl_account_id"])


async def _resolve_settlement_gl(
    organization_id: uuid.UUID,
    *,
    payment_method: str,
    bank_account_id: Optional[uuid.UUID] = None,
    cash_account_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """The MONEY side of a settlement journal (Work Stream R4.1).

    CASH  -> the cash ledger (cash_accounts GL).
    BANK* -> the bank ledger (bank_accounts GL).
    Never the wrong ledger, never a silent cross-default.
    """
    if str(payment_method or "").upper() == "CASH":
        return await _resolve_cash_gl_account(organization_id, cash_account_id)
    return await _resolve_bank_gl_account(organization_id, bank_account_id)


async def _resolve_account_by_keywords(
    organization_id: uuid.UUID,
    *,
    account_type: str,
    keywords: str,
) -> Optional[Dict[str, Any]]:
    """First active account of *account_type* whose name matches any
    *keywords* regex — used for nature-aware advance/loan accounts."""
    rows = await fetch_many(
        "accounts",
        filters={
            "organization_id": str(organization_id),
            "account_type": account_type,
            "is_active": True,
        },
        order="code.asc",
        limit=200,
    )
    for acc in rows or []:
        if re.search(keywords, str(acc.get("name") or ""), re.IGNORECASE):
            return acc
    return None


async def _resolve_receipt_credit_account(
    organization_id: uuid.UUID,
    customer_id: uuid.UUID,
    transaction_nature: Optional[str],
) -> tuple:
    """The CREDIT side of a customer receipt (Work Stream R4.3).

    * ALLOCATION (or unknown)  -> the customer's receivable (settlement).
    * ADVANCE                  -> a customer-advances LIABILITY account
      (money received before any invoice) — falling back to the
      receivable with an explicit warning, never a silent mis-post.
    * LOAN_OR_SETTLEMENT       -> a loan / other-liability account, same
      fallback rule.
    """
    nature = str(transaction_nature or "").upper()
    if nature == "ADVANCE":
        acc = await _resolve_account_by_keywords(
            organization_id,
            account_type="LIABILITY",
            keywords=r"advance|unearned|deferred",
        )
        if acc:
            return uuid.UUID(acc["id"]), None
        warning = (
            "ADVANCE received but no customer-advances liability account is "
            "configured — credited to Accounts Receivable instead"
        )
        return (
            await _resolve_customer_receivable(organization_id, customer_id),
            warning,
        )
    if nature == "LOAN_OR_SETTLEMENT":
        acc = await _resolve_account_by_keywords(
            organization_id,
            account_type="LIABILITY",
            keywords=r"loan|borrowing|sundry creditor|other payabl",
        )
        if acc:
            return uuid.UUID(acc["id"]), None
        warning = (
            "Loan/other settlement received but no matching liability "
            "account is configured — credited to Accounts Receivable instead"
        )
        return (
            await _resolve_customer_receivable(organization_id, customer_id),
            warning,
        )
    return await _resolve_customer_receivable(organization_id, customer_id), None


async def _resolve_payment_debit_account(
    organization_id: uuid.UUID,
    supplier_id: uuid.UUID,
    transaction_nature: Optional[str],
) -> tuple:
    """The DEBIT side of a supplier payment (Work Stream R4.3).

    * ALLOCATION (or unknown)  -> the supplier's payable (settlement).
    * ADVANCE                  -> a supplier-advances ASSET account
      (money paid before any bill) — payable fallback + warning.
    * LOAN_OR_SETTLEMENT       -> a loan liability account, same rule.
    """
    nature = str(transaction_nature or "").upper()
    if nature == "ADVANCE":
        acc = await _resolve_account_by_keywords(
            organization_id,
            account_type="ASSET",
            keywords=r"advance",
        )
        if acc:
            return uuid.UUID(acc["id"]), None
        warning = (
            "Advance paid but no supplier-advances asset account is "
            "configured — debited to Accounts Payable instead"
        )
        return (
            await _resolve_supplier_payable(organization_id, supplier_id),
            warning,
        )
    if nature == "LOAN_OR_SETTLEMENT":
        acc = await _resolve_account_by_keywords(
            organization_id,
            account_type="LIABILITY",
            keywords=r"loan|borrowing",
        )
        if acc:
            return uuid.UUID(acc["id"]), None
        warning = (
            "Loan/other settlement paid but no matching liability account "
            "is configured — debited to Accounts Payable instead"
        )
        return (
            await _resolve_supplier_payable(organization_id, supplier_id),
            warning,
        )
    return await _resolve_supplier_payable(organization_id, supplier_id), None


async def _resolve_customer_receivable(
    organization_id: uuid.UUID, customer_id: uuid.UUID
) -> uuid.UUID:
    """Resolve customer's receivable account (from customer row or default)."""
    from app.database import fetch_one as _fetch
    row = await _fetch("customers", filters={
        "id": str(customer_id), "organization_id": str(organization_id),
    })
    if row and row.get("receivable_account_id"):
        return uuid.UUID(row["receivable_account_id"])
    # Fallback: first ASSET account with RECEIVABLE category
    from app.database import fetch_many as _fm
    accts = await _fm(
        "accounts",
        filters={"organization_id": str(organization_id), "is_active": True},
        select="id,account_category_id",
        limit=200,
    )
    # Find receivable category
    cat = await _fetch("account_categories", filters={"code": "RECEIVABLE"})
    if cat:
        for a in accts:
            if a.get("account_category_id") == cat["id"]:
                return uuid.UUID(a["id"])
    raise ValueError("Cannot resolve receivable account for customer")


async def _resolve_supplier_payable(
    organization_id: uuid.UUID, supplier_id: uuid.UUID
) -> uuid.UUID:
    """Resolve supplier's payable account (from supplier row or default)."""
    from app.database import fetch_one as _fetch
    row = await _fetch("suppliers", filters={
        "id": str(supplier_id), "organization_id": str(organization_id),
    })
    if row and row.get("payable_account_id"):
        return uuid.UUID(row["payable_account_id"])
    # Fallback: first LIABILITY account with PAYABLE category
    from app.database import fetch_many as _fm
    accts = await _fm(
        "accounts",
        filters={"organization_id": str(organization_id), "is_active": True},
        select="id,account_category_id",
        limit=200,
    )
    cat = await _fetch("account_categories", filters={"code": "PAYABLE"})
    if cat:
        for a in accts:
            if a.get("account_category_id") == cat["id"]:
                return uuid.UUID(a["id"])
    raise ValueError("Cannot resolve payable account for supplier")


async def _update_invoice_paid(
    organization_id: uuid.UUID, invoice_id: uuid.UUID, amount: float
) -> None:
    """Increment invoice.amount_paid and update status."""
    inv = await inv_repo.get_invoice(organization_id, invoice_id=invoice_id)
    if not inv:
        return
    new_paid = float(inv.get("amount_paid", 0)) + amount
    total = float(inv.get("total", 0))
    if new_paid >= total:
        status = "PAID"
    elif new_paid > 0:
        status = "PARTIALLY_PAID"
    else:
        status = inv.get("status", "ISSUED")
    await update_one("invoices", row_id=invoice_id, data={
        "amount_paid": new_paid, "status": status,
    }, organization_id=organization_id)


async def _update_bill_paid(
    organization_id: uuid.UUID, bill_id: uuid.UUID, amount: float
) -> None:
    """Increment bill.amount_paid and update status."""
    bill = await pur_repo.get_purchase_bill(organization_id, bill_id=bill_id)
    if not bill:
        return
    new_paid = float(bill.get("amount_paid", 0)) + amount
    total = float(bill.get("total", 0))
    if new_paid >= total:
        status = "PAID"
    elif new_paid > 0:
        status = "PARTIALLY_PAID"
    else:
        status = bill.get("status", "OPEN")
    await update_one("purchase_bills", row_id=bill_id, data={
        "amount_paid": new_paid, "status": status,
    }, organization_id=organization_id)


# ---------------------------------------------------------------------------
# Customer Receipts
# ---------------------------------------------------------------------------

async def record_customer_receipt(
    *,
    organization_id: uuid.UUID,
    customer_id: uuid.UUID,
    amount: float,
    receipt_date: Optional[str] = None,
    payment_method: str = "BANK_TRANSFER",
    currency_code: str = "PKR",
    bank_account_id: Optional[uuid.UUID] = None,
    cash_account_id: Optional[uuid.UUID] = None,
    reference: Optional[str] = None,
    notes: Optional[str] = None,
    invoice_id: Optional[uuid.UUID] = None,
    transaction_nature: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record a customer receipt with auto-journal and optional invoice allocation.

    Work Stream R4: the MONEY side follows the settlement channel — a CASH
    receipt debits the CASH ledger, a bank receipt debits the BANK ledger
    (never crossed).  The CREDIT side follows the business operation
    (``transaction_nature``): invoice settlement credits the receivable,
    an advance credits a customer-advances liability, a loan credits a
    loan/other-liability account (configuration fallbacks warn, never
    mis-post silently).
    """
    r_date = receipt_date or date.today().isoformat()

    # ATOMIC POSTING (migration 078): the receipt, its journal entry and its
    # lines commit in ONE database transaction — a receipt can never exist
    # without its journal (the partial state the previous create-then-journal
    # sequence allowed, where a journal failure was swallowed into a warning).
    # Arithmetic/validation stays here; only the writes are transactional.
    money_gl = await _resolve_settlement_gl(
        organization_id,
        payment_method=payment_method,
        bank_account_id=bank_account_id,
        cash_account_id=cash_account_id,
    )
    credit_gl, credit_warning = await _resolve_receipt_credit_account(
        organization_id, customer_id, transaction_nature
    )

    entry_desc = f"Receipt from customer — {reference or 'customer receipt'}"
    outcome = await call_rpc(
        "create_receipt_atomic",
        params={
            "p_organization_id": str(organization_id),
            "p_header": {
                "receipt_date": r_date,
                "customer_id": str(customer_id),
                "amount": amount,
                "payment_method": payment_method,
                "currency_code": currency_code,
                "bank_account_id": str(bank_account_id) if bank_account_id else None,
                "cash_account_id": str(cash_account_id) if cash_account_id else None,
                "reference": reference,
                "notes": notes,
                "created_by": str(created_by) if created_by else None,
            },
            "p_journal": {
                "transaction_date": r_date,
                "description": entry_desc,
                "reference": reference,
                "currency_code": currency_code,
                "lines": [
                    {
                        "account_id": str(money_gl),
                        "description": f"Receipt — {entry_desc}",
                        "debit": amount,
                        "credit": 0,
                        "customer_id": str(customer_id),
                    },
                    {
                        "account_id": str(credit_gl),
                        "description": f"Receivable settled — {entry_desc}",
                        "debit": 0,
                        "credit": amount,
                        "customer_id": str(customer_id),
                    },
                ],
            },
        },
    ) or {}
    receipt = outcome.get("receipt") or {}
    receipt_id = uuid.UUID(receipt["id"])

    # The RPC created the entry DRAFT and linked it; validate + post so
    # ledger and cash-flow stay current (same as the pre-atomic flow).
    entry_id = uuid.UUID(outcome["journal_entry_id"])
    await accounting_service.validate_journal(entry_id=entry_id)
    await accounting_service.post_journal(entry_id=entry_id)

    if credit_warning:
        # R4.3: a nature-driven fallback is DISCLOSED, never silent.
        receipt["journal_warning"] = credit_warning

    # Allocate against invoice if provided
    if invoice_id:
        await repo.create_receipt_allocation(
            organization_id=organization_id,
            receipt_id=receipt_id,
            invoice_id=invoice_id,
            amount_allocated=amount,
        )
        await _update_invoice_paid(organization_id, invoice_id, amount)

    log.info("payment.receipt_recorded", receipt_id=str(receipt_id), amount=amount)
    return receipt


# ---------------------------------------------------------------------------
# Supplier Payments
# ---------------------------------------------------------------------------

async def record_supplier_payment(
    *,
    organization_id: uuid.UUID,
    supplier_id: uuid.UUID,
    amount: float,
    payment_date: Optional[str] = None,
    payment_method: str = "BANK_TRANSFER",
    currency_code: str = "PKR",
    bank_account_id: Optional[uuid.UUID] = None,
    cash_account_id: Optional[uuid.UUID] = None,
    reference: Optional[str] = None,
    notes: Optional[str] = None,
    bill_id: Optional[uuid.UUID] = None,
    transaction_nature: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record a supplier payment with auto-journal and optional bill allocation.

    Work Stream R4: the MONEY side follows the settlement channel — a CASH
    payment credits the CASH ledger, a bank payment credits the BANK ledger.
    The DEBIT side follows the business operation (``transaction_nature``):
    bill settlement debits the payable, an advance debits a supplier-advances
    asset, a loan repayment debits the loan liability (fallbacks warn).
    """
    p_date = payment_date or date.today().isoformat()

    # ATOMIC POSTING (migration 077): the payment, its journal entry and its
    # lines commit in ONE database transaction — a payment can never exist
    # without its journal (the partial state the previous create-then-journal
    # sequence allowed, where a journal failure was swallowed into a warning).
    # NOTE: DB enum payment_direction = {INFLOW, OUTFLOW} (migration 034).
    # The database contract is authoritative — supplier payments are OUTFLOW.
    money_gl = await _resolve_settlement_gl(
        organization_id,
        payment_method=payment_method,
        bank_account_id=bank_account_id,
        cash_account_id=cash_account_id,
    )
    debit_gl, debit_warning = await _resolve_payment_debit_account(
        organization_id, supplier_id, transaction_nature
    )

    entry_desc = f"Payment to supplier — {reference or 'supplier payment'}"
    outcome = await call_rpc(
        "create_payment_atomic",
        params={
            "p_organization_id": str(organization_id),
            "p_payment": {
                "payment_date": p_date,
                "supplier_id": str(supplier_id),
                "direction": "OUTFLOW",
                "payment_method": payment_method,
                "currency_code": currency_code,
                "bank_account_id": str(bank_account_id) if bank_account_id else None,
                "cash_account_id": str(cash_account_id) if cash_account_id else None,
                "amount": amount,
                "reference": reference,
                "notes": notes,
                "created_by": str(created_by) if created_by else None,
            },
            "p_journal": {
                "transaction_date": p_date,
                "description": entry_desc,
                "reference": reference,
                "currency_code": currency_code,
                "lines": [
                    {
                        "account_id": str(debit_gl),
                        "description": f"Payable settled — {entry_desc}",
                        "debit": amount,
                        "credit": 0,
                        "supplier_id": str(supplier_id),
                    },
                    {
                        "account_id": str(money_gl),
                        "description": f"Payment — {entry_desc}",
                        "debit": 0,
                        "credit": amount,
                    },
                ],
            },
        },
    ) or {}
    payment = outcome.get("payment") or {}
    payment_id = uuid.UUID(payment["id"])

    # The RPC created the entry DRAFT and linked it; validate + post so
    # ledger and cash-flow stay current (same as the pre-atomic flow).
    entry_id = uuid.UUID(outcome["journal_entry_id"])
    await accounting_service.validate_journal(entry_id=entry_id)
    await accounting_service.post_journal(entry_id=entry_id)

    if debit_warning:
        # R4.3: a nature-driven fallback is DISCLOSED, never silent.
        payment["journal_warning"] = debit_warning

    # Allocate against bill if provided
    if bill_id:
        await repo.create_payment_allocation(
            organization_id=organization_id,
            payment_id=payment_id,
            bill_id=bill_id,
            amount_allocated=amount,
        )
        await _update_bill_paid(organization_id, bill_id, amount)

    log.info("payment.supplier_payment_recorded", payment_id=str(payment_id), amount=amount)
    return payment


# ---------------------------------------------------------------------------
# Allocation helpers (for adding allocation to existing payments/receipts)
# ---------------------------------------------------------------------------

async def allocate_receipt(
    *,
    organization_id: uuid.UUID,
    receipt_id: uuid.UUID,
    invoice_id: uuid.UUID,
    amount_allocated: float,
) -> Dict[str, Any]:
    """Allocate an existing receipt against an invoice."""
    allocation = await repo.create_receipt_allocation(
        organization_id=organization_id,
        receipt_id=receipt_id,
        invoice_id=invoice_id,
        amount_allocated=amount_allocated,
    )
    await _update_invoice_paid(organization_id, invoice_id, amount_allocated)
    return allocation


async def allocate_payment(
    *,
    organization_id: uuid.UUID,
    payment_id: uuid.UUID,
    bill_id: uuid.UUID,
    amount_allocated: float,
) -> Dict[str, Any]:
    """Allocate an existing payment against a bill."""
    allocation = await repo.create_payment_allocation(
        organization_id=organization_id,
        payment_id=payment_id,
        bill_id=bill_id,
        amount_allocated=amount_allocated,
    )
    await _update_bill_paid(organization_id, bill_id, amount_allocated)
    return allocation


# ---------------------------------------------------------------------------
# Bank-to-Bank Transfer
# ---------------------------------------------------------------------------

async def record_bank_transfer(
    *,
    organization_id: uuid.UUID,
    source_bank_account_id: uuid.UUID,
    destination_bank_account_id: uuid.UUID,
    amount: float,
    transfer_date: Optional[str] = None,
    reference: Optional[str] = None,
    notes: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Record an internal bank-to-bank transfer.

    Creates a journal entry (Dr destination, Cr source) and
    two bank_transactions (one per account).
    """
    t_date = transfer_date or date.today().isoformat()

    # 1. Resolve GL accounts for both bank accounts
    src_ba = await bank_repo.get_bank_account(organization_id, bank_account_id=source_bank_account_id)
    dst_ba = await bank_repo.get_bank_account(organization_id, bank_account_id=destination_bank_account_id)
    if not src_ba or not dst_ba:
        raise ValueError("Source or destination bank account not found")

    src_gl = uuid.UUID(src_ba["gl_account_id"])
    dst_gl = uuid.UUID(dst_ba["gl_account_id"])

    # 2. Create journal via accounting engine
    journal = await engine.record_bank_transfer(
        organization_id=organization_id,
        source_bank_account_id=src_gl,
        destination_bank_account_id=dst_gl,
        amount=amount,
        transaction_date=t_date,
        description=f"Bank transfer — {reference or notes or 'internal'}",
    )

    entry_id = None
    if journal.get("entry"):
        entry_id = uuid.UUID(journal["entry"]["id"])
        # Auto-validate and post
        await accounting_service.validate_journal(entry_id=entry_id)
        await accounting_service.post_journal(entry_id=entry_id)

    # 3. Create bank_transactions for both sides
    src_txn = await bank_repo.create_bank_transaction(
        organization_id=organization_id,
        bank_account_id=source_bank_account_id,
        transaction_date=t_date,
        direction="OUTFLOW",
        amount=amount,
        description=f"Transfer to {dst_ba['account_name']}",
        reference=reference,
        journal_entry_id=entry_id,
        is_transfer=True,
    )
    dst_txn = await bank_repo.create_bank_transaction(
        organization_id=organization_id,
        bank_account_id=destination_bank_account_id,
        transaction_date=t_date,
        direction="INFLOW",
        amount=amount,
        description=f"Transfer from {src_ba['account_name']}",
        reference=reference,
        journal_entry_id=entry_id,
        is_transfer=True,
    )

    log.info(
        "payment.bank_transfer_recorded",
        source=str(source_bank_account_id),
        destination=str(destination_bank_account_id),
        amount=amount,
    )
    return {
        "transfer": True,
        "journal_entry": journal.get("entry"),
        "source_transaction": src_txn,
        "destination_transaction": dst_txn,
    }
