"""
Product Service — business logic for the product catalog.

Follows the established service conventions (see supplier_service):
search-before-create, duplicate-name reuse with an explicit ``reused``
marker, and NO stock-quantity semantics (the database has no stock
ledger — ``is_stock_tracked`` is a catalog flag only).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import product_repository as repo
from app.repositories import catalogue_repository as c_repo
from app.services import catalogue_rules as c_rules

log = structlog.get_logger(__name__)


async def search(
    organization_id: uuid.UUID, *, query: str, limit: int = 25
) -> List[Dict[str, Any]]:
    return await repo.search_products(organization_id, query=query, limit=limit)


async def get(
    organization_id: uuid.UUID, *, product_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await repo.get_product(organization_id, product_id=product_id)


async def create(
    organization_id: uuid.UUID,
    *,
    name: str,
    product_code: Optional[str] = None,
    description: Optional[str] = None,
    unit: Optional[str] = None,
    is_stock_tracked: bool = False,
    unit_price: float = 0.0,
    cost_price: Optional[float] = None,
    revenue_account_id: Optional[uuid.UUID] = None,
    tax_rate_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """Create a catalog product after verifying it doesn't already exist."""
    if not name or not name.strip():
        raise ValueError("Product name is required.")
    if unit_price is None or float(unit_price) < 0:
        raise ValueError("Product unit price must be a non-negative number.")

    existing = await repo.search_products(
        organization_id, query=name.strip(), limit=5
    )
    for p in existing:
        if p.get("name", "").lower().strip() == name.lower().strip():
            log.warning(
                "product.duplicate_detected",
                existing_id=p["id"],
                name=p["name"],
            )
            # Explicit reuse marker: the agent's narrative MUST reflect
            # reuse (never "newly created").
            return {**p, "reused": True}

    return await repo.create_product(
        organization_id=organization_id,
        name=name.strip(),
        product_code=product_code,
        description=description,
        unit=unit,
        is_stock_tracked=bool(is_stock_tracked),
        unit_price=float(unit_price),
        cost_price=cost_price,
        revenue_account_id=revenue_account_id,
        tax_rate_id=tax_rate_id,
    )


# ===================================================================
# Catalogue management (page + agent): list, edit, activate, delete
# ===================================================================

# Columns the page shows and the ONLY ones an edit may touch.  ``product_code``
# is deliberately absent: it is the item's identity in every historical
# document, so it is generated once and never rewritten.
PRODUCT_FIELDS = (
    "id", "product_code", "name", "description", "unit", "is_stock_tracked",
    "unit_price", "cost_price", "revenue_account_id", "category_id",
    "tax_rate_id", "is_active", "created_at", "updated_at",
)
EDITABLE = (
    "name", "description", "unit", "is_stock_tracked", "unit_price",
    "cost_price", "revenue_account_id", "category_id", "tax_rate_id",
)


async def list_catalog(
    organization_id: uuid.UUID,
    *,
    query: str = "",
    status: str = "ALL",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """The catalogue as the page shows it (products only — services are the
    service service's job; the page merges the two lists)."""
    return await c_repo.list_rows(
        "products", organization_id, columns=PRODUCT_FIELDS,
        query=query, status=status, limit=limit,
    )


async def update(
    organization_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    **changes: Any,
) -> Dict[str, Any]:
    """Edit a catalogue product. Only the validated, editable fields are applied.

    Every rule the create path enforces is enforced here too, so an edit can
    never introduce a state creation would have refused.
    """
    patch: Dict[str, Any] = {}
    for field in EDITABLE:
        if field not in changes:
            continue
        value = changes[field]
        if field == "name":
            if value is None or not str(value).strip():
                raise ValueError("Product name is required.")
            value = str(value).strip()
        elif field in ("unit_price", "cost_price"):
            if value in (None, ""):
                if field == "unit_price":
                    raise ValueError("Product unit price must be a non-negative number.")
                value = None
            else:
                value = float(value)
                if value < 0:
                    raise ValueError(
                        "Product %s must be a non-negative number."
                        % field.replace("_", " ")
                    )
        elif field == "is_stock_tracked":
            value = bool(value)
        elif field in ("description", "unit"):
            value = (str(value).strip() or None) if value is not None else None
        elif value is not None:
            value = str(value)
        patch[field] = value

    if not patch:
        raise ValueError("Nothing to update — no editable field was provided.")

    current = await repo.get_product(organization_id, product_id=product_id)
    if not current:
        raise ValueError("Product not found in this organization.")
    if "name" in patch and patch["name"].lower() != current["name"].lower():
        # Renaming must not silently create a second row with the same name.
        clash = await repo.search_products(
            organization_id, query=patch["name"], limit=5
        )
        for row in clash:
            if (row.get("name", "").lower().strip() == patch["name"].lower()
                    and str(row.get("id")) != str(product_id)):
                raise ValueError(
                    "Another product is already named '%s'." % patch["name"]
                )

    updated = await c_repo.patch_row(
        "products", organization_id, row_id=product_id, changes=patch
    )
    if not updated:
        raise ValueError("Product not found in this organization.")
    log.info(
        "product.updated",
        product_id=str(product_id),
        fields=sorted(patch.keys()),
    )
    return updated


async def set_active(
    organization_id: uuid.UUID, *, product_id: uuid.UUID, active: bool
) -> Dict[str, Any]:
    """Activate / deactivate (soft delete). Never removes history."""
    updated = await c_repo.patch_row(
        "products", organization_id, row_id=product_id,
        changes={"is_active": bool(active)},
    )
    if not updated:
        raise ValueError("Product not found in this organization.")
    log.info("product.status_changed", product_id=str(product_id), active=bool(active))
    return updated


async def usage(
    organization_id: uuid.UUID, *, product_id: uuid.UUID
) -> List[tuple]:
    """Where this product is referenced (empty = safe to delete)."""
    return await c_rules.find_usage(
        organization_id, item_id=product_id, column="product_id",
        references=c_rules.PRODUCT_REFERENCES,
    )


async def delete(organization_id: uuid.UUID, *, product_id: uuid.UUID) -> None:
    """Hard delete — REFUSED while any document references the product.

    The FKs are ``NO ACTION``, so the database would reject the delete anyway;
    checking first turns an opaque constraint error into an instruction the user
    can act on (deactivate instead).
    """
    current = await repo.get_product(organization_id, product_id=product_id)
    if not current:
        raise ValueError("Product not found in this organization.")
    used = await usage(organization_id, product_id=product_id)
    if used:
        raise ValueError(
            c_rules.usage_message(used, current.get("name", "This product"), "invoice")
        )
    removed = await c_repo.remove_row("products", organization_id, row_id=product_id)
    if not removed:
        raise ValueError("Product not found in this organization.")
    log.info("product.deleted", product_id=str(product_id), name=current.get("name"))

