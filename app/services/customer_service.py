"""
Customer Service — business logic for customer operations.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import customer_repository as repo

log = structlog.get_logger(__name__)


async def search(
    organization_id: uuid.UUID, *, query: str, limit: int = 25
) -> List[Dict[str, Any]]:
    return await repo.search_customers(organization_id, query=query, limit=limit)


async def get(
    organization_id: uuid.UUID, *, customer_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await repo.get_customer(organization_id, customer_id=customer_id)


async def _with_party_ledger(
    organization_id: uuid.UUID, customer: Dict[str, Any]
) -> Dict[str, Any]:
    """Link the customer's DEDICATED receivable ledger, then return the record.

    Best-effort by design: the customer exists either way, and a posting falls
    back to the AR control account (today's behaviour) when the ledger could not
    be provisioned - a bookkeeping failure must never lose the party.  On the
    reuse path a customer created before this feature still gets its ledger,
    once: ``ensure`` reuses an existing child account by name, so a second call
    creates nothing.
    """
    from app.services import party_ledger_service

    customer = dict(customer or {})
    try:
        account, created = await party_ledger_service.ensure_customer_receivable_account(
            organization_id, customer
        )
    except Exception as exc:  # noqa: BLE001 - never lose the customer
        log.warning(
            "customer.party_ledger_failed",
            name=customer.get("name"),
            error=str(exc)[:200],
        )
        return customer
    if not account or not account.get("id"):
        return customer

    account_id = str(account["id"])
    linked = str(customer.get("receivable_account_id") or "")
    if linked != account_id:
        try:
            await repo.set_receivable_account(
                organization_id,
                customer_id=uuid.UUID(str(customer["id"])),
                account_id=uuid.UUID(account_id),
            )
        except Exception as exc:  # noqa: BLE001 - the account exists; the link can retry
            log.warning(
                "customer.party_ledger_link_failed",
                name=customer.get("name"),
                account_code=account.get("code"),
                error=str(exc)[:200],
            )
    return {
        **customer,
        "receivable_account_id": account_id,
        "receivable_account_code": account.get("code"),
        "receivable_account_name": account.get("name"),
        "receivable_account_created": created,
    }


async def create(
    organization_id: uuid.UUID,
    *,
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
    """Create a customer after verifying it doesn't already exist."""
    # Search for duplicates first
    existing = await repo.search_customers(organization_id, query=name, limit=5)
    for c in existing:
        if c.get("name", "").lower().strip() == name.lower().strip():
            log.warning(
                "customer.duplicate_detected",
                existing_id=c["id"],
                name=c["name"],
            )
            # Return the existing customer instead of creating a duplicate.
            # Explicit reuse marker: the agent's narrative MUST reflect
            # reuse (never "newly created").
            return await _with_party_ledger(
                organization_id, {**c, "reused": True}
            )

    created = await repo.create_customer(
        organization_id=organization_id,
        name=name,
        email=email,
        phone=phone,
        legal_name=legal_name,
        tax_number=tax_number,
        currency_code=currency_code,
        payment_terms_days=payment_terms_days,
        credit_limit=credit_limit,
        receivable_account_id=receivable_account_id,
        notes=notes,
    )
    return await _with_party_ledger(organization_id, created)


async def get_ledger(
    organization_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    return await repo.get_customer_ledger(
        organization_id, customer_id=customer_id, limit=limit
    )


async def get_open_receivables(
    organization_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
) -> List[Dict[str, Any]]:
    return await repo.get_open_receivables(organization_id, customer_id=customer_id)
