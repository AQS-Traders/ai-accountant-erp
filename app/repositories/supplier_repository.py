"""
Supplier Repository — Supabase data access for supplier records.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.database import fetch_many, fetch_one, insert_one, search_ilike, update_one


async def search_suppliers(
    organization_id: uuid.UUID, *, query: str, limit: int = 25
) -> List[Dict[str, Any]]:
    """Search suppliers by name or code."""
    return await search_ilike(
        "suppliers",
        column="name",
        value=query,
        organization_id=organization_id,
        select="id,name,supplier_code,email,phone,is_active",
        limit=limit,
    )


async def get_supplier(
    organization_id: uuid.UUID, *, supplier_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "suppliers",
        filters={
            "id": str(supplier_id),
            "organization_id": str(organization_id),
        },
    )


async def get_supplier_by_code(
    organization_id: uuid.UUID, *, code: str
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "suppliers",
        filters={
            "supplier_code": code,
            "organization_id": str(organization_id),
        },
    )


async def create_supplier(
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
    payable_account_id: Optional[uuid.UUID] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new supplier. supplier_code auto-assigned by trigger."""
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
        "payable_account_id": str(payable_account_id) if payable_account_id else None,
        "notes": notes,
        "is_active": True,
    }
    return await insert_one("suppliers", data=data)


async def set_payable_account(
    organization_id: uuid.UUID,
    *,
    supplier_id: uuid.UUID,
    account_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """Link the supplier's DEDICATED payable ledger.

    Party segregation: the account is a child of the AP control account, so a
    purchase bill credits the supplier's own payable instead of the shared
    control account.  ``organization_id`` is the second guard on the update.
    """
    return await update_one(
        "suppliers",
        row_id=supplier_id,
        data={"payable_account_id": str(account_id)},
        organization_id=organization_id,
    )


async def get_supplier_ledger(
    organization_id: uuid.UUID,
    *,
    supplier_id: uuid.UUID,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Retrieve supplier ledger entries from the v_supplier_ledger view."""
    return await fetch_many(
        "v_supplier_ledger",
        filters={
            "supplier_id": str(supplier_id),
            "organization_id": str(organization_id),
        },
        order="transaction_date.desc",
        limit=limit,
    )


async def get_open_payables(
    organization_id: uuid.UUID,
    *,
    supplier_id: uuid.UUID,
) -> List[Dict[str, Any]]:
    """Fetch open bills for a supplier."""
    return await fetch_many(
        "v_open_payables",
        filters={
            "supplier_id": str(supplier_id),
            "organization_id": str(organization_id),
        },
    )
