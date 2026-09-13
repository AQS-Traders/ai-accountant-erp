"""
Expense Service — business logic for expense recording and classification.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import expense_repository as repo

log = structlog.get_logger(__name__)


async def create_expense(
    *,
    organization_id: uuid.UUID,
    expense_date: Optional[str] = None,
    payee_name: Optional[str] = None,
    description: str,
    subtotal: float,
    currency_code: str = "PKR",
    tax_total: float = 0.0,
    total: Optional[float] = None,
    supplier_id: Optional[uuid.UUID] = None,
    category_id: Optional[uuid.UUID] = None,
    payment_mode: str = "CASH",
    created_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    exp_date = expense_date or date.today().isoformat()
    expense = await repo.create_expense(
        organization_id=organization_id,
        expense_date=exp_date,
        payee_name=payee_name or "",
        description=description,
        subtotal=subtotal,
        currency_code=currency_code,
        tax_total=tax_total,
        total=total,
        supplier_id=supplier_id,
        category_id=category_id,
        payment_mode=payment_mode,
        created_by=created_by,
    )
    log.info("expense.created", expense_id=expense["id"], total=expense.get("total"))
    return expense


async def get_expense(
    organization_id: uuid.UUID, *, expense_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await repo.get_expense(organization_id, expense_id=expense_id)


async def classify_expense(
    *, description: str, amount: float
) -> Dict[str, Any]:
    """Classify an expense by description — returns suggested account type.

    This is a simple heuristic; the AI may refine it.
    """
    desc_lower = description.lower()
    if any(w in desc_lower for w in ["salary", "wage", "payroll"]):
        return {"category": "Salaries & Wages", "account_type": "EXPENSE"}
    if any(w in desc_lower for w in ["rent", "lease"]):
        return {"category": "Rent", "account_type": "EXPENSE"}
    if any(w in desc_lower for w in ["internet", "phone", "telecom"]):
        return {"category": "Communication", "account_type": "EXPENSE"}
    if any(w in desc_lower for w in ["electricity", "gas", "water", "utility"]):
        return {"category": "Utilities", "account_type": "EXPENSE"}
    if any(w in desc_lower for w in ["travel", "flight", "hotel", "transport"]):
        return {"category": "Travel", "account_type": "EXPENSE"}
    if any(w in desc_lower for w in ["computer", "laptop", "equipment", "hardware"]):
        return {"category": "Computer Equipment", "account_type": "ASSET"}
    if any(w in desc_lower for w in ["software", "subscription", "saas", "license"]):
        return {"category": "Software & Subscriptions", "account_type": "EXPENSE"}
    if any(w in desc_lower for w in ["office", "stationery", "supply"]):
        return {"category": "Office Supplies", "account_type": "EXPENSE"}
    return {"category": "General Expense", "account_type": "EXPENSE"}
