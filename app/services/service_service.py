"""
Service Service — business logic for the service catalog.

Follows the established entity conventions (supplier_service /
product_service): search-before-create, exact-name reuse with an
explicit ``reused`` marker, and NO inventory semantics — a service is
never stock, never a fixed asset, regardless of price.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import service_repository as repo
from app.repositories import catalogue_repository as c_repo
from app.services import catalogue_rules as c_rules

log = structlog.get_logger(__name__)

# Authoritative DB enum: service_unit_code
BILLING_UNITS = ("HOUR", "DAY", "MONTH", "FIXED", "ITEM")


async def search(
    organization_id: uuid.UUID, *, query: str, limit: int = 25
) -> List[Dict[str, Any]]:
    return await repo.search_services(organization_id, query=query, limit=limit)


async def get(
    organization_id: uuid.UUID, *, service_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await repo.get_service(organization_id, service_id=service_id)


async def create(
    organization_id: uuid.UUID,
    *,
    name: str,
    service_code: Optional[str] = None,
    description: Optional[str] = None,
    billing_unit: str = "HOUR",
    standard_rate: float = 0.0,
    cost_rate: Optional[float] = None,
    category_id: Optional[uuid.UUID] = None,
    revenue_account_id: Optional[uuid.UUID] = None,
    tax_rate_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Create a catalog service after verifying it doesn't already exist."""
    if not name or not name.strip():
        raise ValueError("Service name is required.")
    if billing_unit not in BILLING_UNITS:
        raise ValueError(
            f"billing_unit must be one of {', '.join(BILLING_UNITS)} "
            f"(got {billing_unit})."
        )
    if standard_rate is None or float(standard_rate) < 0:
        raise ValueError("Service standard rate must be a non-negative number.")
    if cost_rate is not None and float(cost_rate) < 0:
        raise ValueError("Service cost rate must be a non-negative number.")

    existing = await repo.search_services(
        organization_id, query=name.strip(), limit=5
    )
    for s in existing:
        if s.get("name", "").lower().strip() == name.lower().strip():
            log.warning(
                "service.duplicate_detected",
                existing_id=s["id"],
                name=s["name"],
            )
            # Explicit reuse marker: the agent's narrative MUST reflect
            # reuse (never "newly created").
            return {**s, "reused": True}

    return await repo.create_service(
        organization_id=organization_id,
        name=name.strip(),
        service_code=service_code,
        description=description,
        billing_unit=billing_unit,
        standard_rate=float(standard_rate),
        cost_rate=cost_rate,
        category_id=category_id,
        revenue_account_id=revenue_account_id,
        tax_rate_id=tax_rate_id,
    )


# ===================================================================
# Catalogue management (page + agent): list, edit, activate, delete
# ===================================================================

# ``service_code`` is its identity in every historical document: generated once,
# never rewritten.  ``category_id`` is the service category (not a GL account).
SERVICE_FIELDS = (
    "id", "service_code", "name", "description", "billing_unit",
    "standard_rate", "cost_rate", "category_id", "revenue_account_id",
    "tax_rate_id", "is_active", "created_at", "updated_at",
)
EDITABLE = (
    "name", "description", "billing_unit", "standard_rate", "cost_rate",
    "category_id", "revenue_account_id", "tax_rate_id",
)


async def list_catalog(
    organization_id: uuid.UUID,
    *,
    query: str = "",
    status: str = "ALL",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """The service catalogue as the page shows it."""
    return await c_repo.list_rows(
        "services", organization_id, columns=SERVICE_FIELDS,
        query=query, status=status, limit=limit,
    )


async def update(
    organization_id: uuid.UUID,
    *,
    service_id: uuid.UUID,
    **changes: Any,
) -> Dict[str, Any]:
    """Edit a catalog service — the create rules are re-applied here too."""
    patch: Dict[str, Any] = {}
    for field in EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "name":
            if value is None or not str(value).strip():
                raise ValueError("Service name is required.")
            value = str(value).strip()
        elif field == "billing_unit":
            if value not in BILLING_UNITS:
                raise ValueError(
                    f"billing_unit must be one of {', '.join(BILLING_UNITS)} "
                    f"(got {value})."
                )
        elif field in ("standard_rate", "cost_rate"):
            if value in (None, ""):
                if field == "standard_rate":
                    raise ValueError(
                        "Service standard rate must be a non-negative number."
                    )
                value = None
            else:
                value = float(value)
                if value < 0:
                    raise ValueError(
                        "Service %s must be a non-negative number."
                        % field.replace("_", " ")
                    )
        elif field == "description":
            value = (str(value).strip() or None) if value is not None else None
        elif value is not None:
            value = str(value)
        patch[field] = value

    if not patch:
        raise ValueError("Nothing to update — no editable field was provided.")

    current = await repo.get_service(organization_id, service_id=service_id)
    if not current:
        raise ValueError("Service not found in this organization.")
    if "name" in patch and patch["name"].lower() != current["name"].lower():
        clash = await repo.search_services(
            organization_id, query=patch["name"], limit=5
        )
        for row in clash:
            if (row.get("name", "").lower().strip() == patch["name"].lower()
                    and str(row.get("id")) != str(service_id)):
                raise ValueError(
                    "Another service is already named '%s'." % patch["name"]
                )

    updated = await c_repo.patch_row(
        "services", organization_id, row_id=service_id, changes=patch
    )
    if not updated:
        raise ValueError("Service not found in this organization.")
    log.info(
        "service.updated", service_id=str(service_id), fields=sorted(patch.keys())
    )
    return updated


async def set_active(
    organization_id: uuid.UUID, *, service_id: uuid.UUID, active: bool
) -> Dict[str, Any]:
    """Activate / deactivate (soft delete). Never removes history."""
    updated = await c_repo.patch_row(
        "services", organization_id, row_id=service_id,
        changes={"is_active": bool(active)},
    )
    if not updated:
        raise ValueError("Service not found in this organization.")
    log.info("service.status_changed", service_id=str(service_id), active=bool(active))
    return updated


async def usage(
    organization_id: uuid.UUID, *, service_id: uuid.UUID
) -> List[tuple]:
    """Where this service is referenced (empty = safe to delete)."""
    return await c_rules.find_usage(
        organization_id, item_id=service_id, column="service_id",
        references=c_rules.SERVICE_REFERENCES,
    )


async def delete(organization_id: uuid.UUID, *, service_id: uuid.UUID) -> None:
    """Hard delete — REFUSED while any document references the service."""
    current = await repo.get_service(organization_id, service_id=service_id)
    if not current:
        raise ValueError("Service not found in this organization.")
    used = await usage(organization_id, service_id=service_id)
    if used:
        raise ValueError(
            c_rules.usage_message(used, current.get("name", "This service"), "invoice")
        )
    removed = await c_repo.remove_row("services", organization_id, row_id=service_id)
    if not removed:
        raise ValueError("Service not found in this organization.")
    log.info("service.deleted", service_id=str(service_id), name=current.get("name"))
