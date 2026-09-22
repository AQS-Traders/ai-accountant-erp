"""
Customer Repository — Supabase data access for customer records.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.database import fetch_many, fetch_one, insert_one, search_ilike, update_one


async def search_customers(
    organization_id: uuid.UUID, *, query: str, limit: int = 25
) -> List[Dict[str, Any]]:
    """Search customers by name or code using trigram similarity."""
    return await search_ilike(
        "customers",
        column="name",
        value=query,
        organization_id=organization_id,
        select="id,name,customer_code,email,phone,is_active",
        limit=limit,
    )


async def get_customer(
    organization_id: uuid.UUID, *, customer_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """Fetch a single customer by ID within organisation scope."""
    return await fetch_one(
        "customers",
        filters={
            "id": str(customer_id),
            "organization_id": str(organization_id),
        },
    )


async def get_customer_by_code(
    organization_id: uuid.UUID, *, code: str
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "customers",
        filters={
            "customer_code": code,
            "organization_id": str(organization_id),
        },
    )


async def create_customer(
    *,
    organization_id: uuid.UUID,
    name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    legal_name: Optional[str] = None,
    tax_number: Optional[str] = None,
    currency_code: str = "PKR",
    payment_terms_days: int = 30,
    credit_limit: Optional[float] = None,
    receivable_account_id: Optional[uuid.UUID] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new customer. customer_code is auto-assigned by trigger."""
    data: Dict[str, Any] = {
        "organization_id": str(organization_id),
        "name": name,
        "email": email,
        "phone": phone,
        "legal_name": legal_name,
        "tax_number": tax_number,
        "currency_code": currency_code,
        "payment_terms_days": payment_terms_days,
        # credit_limit is NOT NULL in the schema (default 0) — an explicit
        # None would violate the constraint, so default it here.
        "credit_limit": credit_limit if credit_limit is not None else 0,
        "receivable_account_id": str(receivable_account_id) if receivable_account_id else None,
        "notes": notes,
        "is_active": True,
    }
    return await insert_one("customers", data=data)


async def set_receivable_account(
    organization_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
    account_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """Link the customer's DEDICATED receivable ledger.

    Party segregation: the account is a child of the AR control account, so an
    invoice debits the customer's own receivable instead of the shared control
    account.  ``organization_id`` is passed as the second guard so a foreign row
    id can never be updated (see database.update_one).
    """
    return await update_one(
        "customers",
        row_id=customer_id,
        data={"receivable_account_id": str(account_id)},
        organization_id=organization_id,
    )


async def get_customer_ledger(
    organization_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Retrieve customer ledger entries from the v_customer_ledger view."""
    return await fetch_many(
        "v_customer_ledger",
        filters={
            "customer_id": str(customer_id),
            "organization_id": str(organization_id),
        },
        order="transaction_date.desc",
        limit=limit,
    )


async def get_open_receivables(
    organization_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
) -> List[Dict[str, Any]]:
    """Fetch open invoices for a customer."""
    return await fetch_many(
        "v_open_receivables",
        filters={
            "customer_id": str(customer_id),
            "organization_id": str(organization_id),
        },
    )
