"""Catalogue management: list, edit, deactivate, and a delete that refuses.

Live FK introspection (2026-09-23) is the reason this rule exists:

    products.id <- invoice_items, credit_note_items, purchase_bill_items,
                   purchase_return_items, quotation_items   (all NO ACTION)
    services.id <- invoice_items, credit_note_items, quotation_items

``NO ACTION`` means the database REJECTS the delete of an item that any
document used, with an opaque constraint error.  Customers need to be told what
to do instead, so the service checks first and says:

    "'Desk' is already used by sales invoices, so deleting it would break those
     documents' history. Deactivate it instead ..."

Everything here is pinned from that requirement, not from the implementation.

No live provider, no live database (the DB layer is stubbed).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import pytest

from app.repositories import catalogue_repository as c_repo
from app.services import catalogue_rules as c_rules
from app.services import product_service, service_service

ORG = uuid.UUID("4f20f43c-bb3b-4747-abe1-02d9380884df")
PID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _product(**over: Any) -> Dict[str, Any]:
    row = {
        "id": str(PID), "organization_id": str(ORG), "product_code": "PRD-0001",
        "name": "Desk", "description": None, "unit": "pcs",
        "is_stock_tracked": False, "unit_price": 15000.0, "cost_price": 9000.0,
        "revenue_account_id": None, "category_id": None, "tax_rate_id": None,
        "is_active": True,
    }
    row.update(over)
    return row


def _service(**over: Any) -> Dict[str, Any]:
    row = {
        "id": str(SID), "organization_id": str(ORG), "service_code": "SRV-0001",
        "name": "Web Development", "description": None, "billing_unit": "HOUR",
        "standard_rate": 5000.0, "cost_rate": None, "category_id": None,
        "revenue_account_id": None, "tax_rate_id": None, "is_active": True,
    }
    row.update(over)
    return row


class _World:
    """Records every repository call so isolation/scope can be asserted.

    The DOUBLES are deliberately narrow: only the functions are replaced, never
    the modules — ``catalogue_rules`` also carries the reference tables and the
    refusal text the service needs, and ``list_rows`` filtering must run for
    real so the status tests exercise production code rather than this stub.
    """

    def __init__(self, *, rows: Optional[List[Dict[str, Any]]] = None,
                 used: Optional[List[str]] = None):
        self.rows = rows or []
        self.used_tables = used or []
        self.calls: List[tuple] = []

    # ---- catalogue_repository: READS (the real list_rows runs) ---------------
    async def fetch_many(self, table_name, *, filters, select="*", order=None,
                         limit=100, offset=0):
        self.calls.append(("fetch_many", table_name, str(filters.get(
            "organization_id")), order, limit))
        return [dict(r) for r in self.rows]

    async def search_ilike(self, table_name, *, column, value, organization_id,
                           select="*", limit=25):
        self.calls.append(("search_ilike", table_name, str(organization_id),
                           column, value))
        term = str(value or "").lower()
        return [dict(r) for r in self.rows
                if term in str(r.get(column, "")).lower()][:limit]

    # ---- catalogue_repository: WRITES ---------------------------------------
    async def patch_row(self, table, organization_id, *, row_id, changes):
        self.calls.append(("patch_row", table, str(organization_id), str(row_id),
                           tuple(sorted(changes))))
        for row in self.rows:
            if str(row["id"]) == str(row_id):
                return {**row, **changes}
        return None

    async def remove_row(self, table, organization_id, *, row_id):
        self.calls.append(("remove_row", table, str(organization_id), str(row_id)))
        return True

    # ---- catalogue_rules ----------------------------------------------------
    async def find_usage(self, organization_id, *, item_id, column, references,
                         limit=1):
        self.calls.append(("find_usage", str(organization_id), str(item_id),
                           column))
        return [(t, 1) for t in self.used_tables]


def _patch(monkeypatch, world: _World, *, products=None, services=None) -> None:
    """Replace the nine DB touch-points the services use — nothing else."""
    # writes + the usage probe
    monkeypatch.setattr(c_repo, "patch_row", world.patch_row)
    monkeypatch.setattr(c_repo, "remove_row", world.remove_row)
    monkeypatch.setattr(c_rules, "find_usage", world.find_usage)
    # reads (the real list_rows / status filter runs over these)
    monkeypatch.setattr(c_repo, "fetch_many", world.fetch_many)
    monkeypatch.setattr(c_repo, "search_ilike", world.search_ilike)

    async def get_product(organization_id, *, product_id):
        return (products if products is not None else _product())

    async def get_service(organization_id, *, service_id):
        return (services if services is not None else _service())

    async def search_products(organization_id, *, query, limit=25):
        return []

    async def search_services(organization_id, *, query, limit=25):
        return []

    monkeypatch.setattr(product_service.repo, "get_product", get_product)
    monkeypatch.setattr(product_service.repo, "search_products", search_products)
    monkeypatch.setattr(service_service.repo, "get_service", get_service)
    monkeypatch.setattr(service_service.repo, "search_services", search_services)




# ---------------------------------------------------------------------------
# 1. Listing: a blank term means the WHOLE catalogue
# ---------------------------------------------------------------------------


class TestListing:
    @pytest.mark.asyncio
    async def test_a_blank_query_lists_everything(self, monkeypatch):
        world = _World(rows=[_product(), _product(id=str(SID), name="Chair")])
        _patch(monkeypatch, world)

        rows = await product_service.list_catalog(ORG)

        assert len(rows) == 2
        # A blank term lists the table directly (no search RPC).
        assert world.calls[0][0] == "fetch_many"
        assert world.calls[0][1] == "products"
        assert world.calls[0][2] == str(ORG)

    @pytest.mark.asyncio
    async def test_a_query_is_passed_through(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        await product_service.list_catalog(ORG, query="desk")

        assert world.calls[0][0] == "search_ilike"
        assert world.calls[0][3:] == ("name", "desk")

    @pytest.mark.asyncio
    async def test_active_filter_keeps_only_active_rows(self, monkeypatch):
        world = _World(rows=[_product(), _product(id=str(SID), is_active=False)])
        _patch(monkeypatch, world)

        rows = await product_service.list_catalog(ORG, status="ACTIVE")

        assert [r["id"] for r in rows] == [str(PID)]

    @pytest.mark.asyncio
    async def test_inactive_filter_keeps_only_inactive_rows(self, monkeypatch):
        world = _World(rows=[_product(), _product(id=str(SID), is_active=False)])
        _patch(monkeypatch, world)

        rows = await product_service.list_catalog(ORG, status="INACTIVE")

        assert [r["id"] for r in rows] == [str(SID)]

    @pytest.mark.asyncio
    async def test_an_unknown_status_means_all(self, monkeypatch):
        world = _World(rows=[_product(), _product(id=str(SID), is_active=False)])
        _patch(monkeypatch, world)

        rows = await product_service.list_catalog(ORG, status="WHATEVER")

        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_counts_report_the_live_status_strip(self, monkeypatch):
        world = _World(rows=[_product(), _product(id=str(SID), is_active=False)])
        _patch(monkeypatch, world)

        counts = c_rules.status_counts(world.rows)

        assert counts == {"total": 2, "active": 1, "inactive": 1}



# ---------------------------------------------------------------------------
# 2. Editing: only editable fields, create-rules re-applied
# ---------------------------------------------------------------------------


class TestEditing:
    @pytest.mark.asyncio
    async def test_only_the_given_fields_are_patched(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        await product_service.update(ORG, product_id=PID, unit_price=18000)

        assert world.calls[0] == (
            "patch_row", "products", str(ORG), str(PID), ("unit_price",))

    @pytest.mark.asyncio
    async def test_the_code_can_never_be_rewritten(self, monkeypatch):
        """product_code is the item's identity in every past document."""
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        await product_service.update(
            ORG, product_id=PID, product_code="HACKED", name="Desk 2"
        )

        patched = world.calls[0]
        assert "product_code" not in patched[4]
        assert patched[4] == ("name",)

    @pytest.mark.asyncio
    async def test_a_negative_price_is_refused(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await product_service.update(ORG, product_id=PID, unit_price=-1)

        assert "non-negative" in str(err.value)
        assert world.calls == []          # nothing was written

    @pytest.mark.asyncio
    async def test_an_empty_name_is_refused(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await product_service.update(ORG, product_id=PID, name="   ")

        assert "name is required" in str(err.value).lower()

    @pytest.mark.asyncio
    async def test_an_empty_patch_is_refused(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError):
            await product_service.update(ORG, product_id=PID)

        assert world.calls == []

    @pytest.mark.asyncio
    async def test_renaming_onto_another_products_name_is_refused(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        async def clash(organization_id, *, query, limit=25):
            return [{"id": "99999999-9999-9999-9999-999999999999", "name": "Chair"}]

        monkeypatch.setattr(product_service.repo, "search_products", clash)

        with pytest.raises(ValueError) as err:
            await product_service.update(ORG, product_id=PID, name="Chair")

        assert "already named" in str(err.value)
        assert world.calls == []

    @pytest.mark.asyncio
    async def test_renaming_to_its_own_name_is_allowed(self, monkeypatch):
        """Case/spacing changes must not be mistaken for a clash with itself."""
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        async def itself(organization_id, *, query, limit=25):
            return [{"id": str(PID), "name": "desk"}]

        monkeypatch.setattr(product_service.repo, "search_products", itself)

        updated = await product_service.update(ORG, product_id=PID, name="desk")

        assert updated["name"] == "desk"

    @pytest.mark.asyncio
    async def test_cost_price_may_be_cleared(self, monkeypatch):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        await product_service.update(ORG, product_id=PID, cost_price=None)

        assert world.calls[0][4] == ("cost_price",)

    @pytest.mark.asyncio
    async def test_a_missing_product_is_refused(self, monkeypatch):
        world = _World(rows=[])
        _patch(monkeypatch, world)

        async def missing(organization_id, *, product_id):
            return None

        monkeypatch.setattr(product_service.repo, "get_product", missing)

        with pytest.raises(ValueError) as err:
            await product_service.update(ORG, product_id=PID, unit_price=1)

        assert "not found" in str(err.value).lower()

    @pytest.mark.asyncio
    async def test_service_billing_unit_is_validated_on_update(self, monkeypatch):
        world = _World(rows=[_service()])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await service_service.update(
                ORG, service_id=SID, billing_unit="FORTNIGHT"
            )

        assert "billing_unit" in str(err.value)
        assert world.calls == []

    @pytest.mark.asyncio
    async def test_service_update_applies_a_valid_billing_unit(self, monkeypatch):
        world = _World(rows=[_service()])
        _patch(monkeypatch, world)

        await service_service.update(ORG, service_id=SID, billing_unit="FIXED")

        assert world.calls[0][4] == ("billing_unit",)


# ---------------------------------------------------------------------------
# 3. Deactivate (soft) vs delete (hard) — the accounting-integrity rule
# ---------------------------------------------------------------------------


class TestDeleteRules:
    @pytest.mark.asyncio
    async def test_deactivate_flips_is_active_and_never_removes_the_row(
        self, monkeypatch
    ):
        world = _World(rows=[_product()])
        _patch(monkeypatch, world)

        updated = await product_service.set_active(ORG, product_id=PID, active=False)

        assert updated["is_active"] is False
        assert world.calls[0] == (
            "patch_row", "products", str(ORG), str(PID), ("is_active",))
        assert not any(call[0] == "remove_row" for call in world.calls)

    @pytest.mark.asyncio
    async def test_deactivate_can_be_undone(self, monkeypatch):
        world = _World(rows=[_product(is_active=False)])
        _patch(monkeypatch, world)

        updated = await product_service.set_active(ORG, product_id=PID, active=True)

        assert updated["is_active"] is True

    @pytest.mark.asyncio
    async def test_delete_removes_an_unused_product(self, monkeypatch):
        world = _World(rows=[_product()], used=[])
        _patch(monkeypatch, world)

        await product_service.delete(ORG, product_id=PID)

        assert ("remove_row", "products", str(ORG), str(PID)) in world.calls

    @pytest.mark.asyncio
    async def test_delete_is_refused_when_a_document_uses_the_product(
        self, monkeypatch
    ):
        world = _World(rows=[_product()], used=["invoice_items"])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await product_service.delete(ORG, product_id=PID)

        text = str(err.value)
        assert "sales invoices" in text          # says WHERE it is used
        assert "Deactivate" in text              # says WHAT to do instead
        assert not any(call[0] == "remove_row" for call in world.calls)

    @pytest.mark.asyncio
    async def test_the_refusal_lists_every_document_type(self, monkeypatch):
        world = _World(rows=[_product()],
                       used=["quotation_items", "credit_note_items"])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await product_service.delete(ORG, product_id=PID)

        text = str(err.value)
        assert "quotations" in text and "credit notes" in text

    @pytest.mark.asyncio
    async def test_delete_checks_the_products_own_id(self, monkeypatch):
        world = _World(rows=[_product()], used=[])
        _patch(monkeypatch, world)

        await product_service.delete(ORG, product_id=PID)

        usage_call = next(c for c in world.calls if c[0] == "find_usage")
        assert usage_call[3] == "product_id"

    @pytest.mark.asyncio
    async def test_delete_checks_the_services_own_column(self, monkeypatch):
        """invoice_items references BOTH — a service must be looked up by
        service_id, not product_id (that confusion was a real defect)."""
        world = _World(rows=[_service()], used=[])
        _patch(monkeypatch, world, services=_service())

        await service_service.delete(ORG, service_id=SID)

        usage_call = next(c for c in world.calls if c[0] == "find_usage")
        assert usage_call[3] == "service_id"

    @pytest.mark.asyncio
    async def test_a_service_used_by_a_quotation_cannot_be_deleted(self, monkeypatch):
        world = _World(rows=[_service()], used=["quotation_items"])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await service_service.delete(ORG, service_id=SID)

        assert "Deactivate" in str(err.value)

    @pytest.mark.asyncio
    async def test_a_missing_product_is_never_deleted(self, monkeypatch):
        world = _World(rows=[], used=[])
        _patch(monkeypatch, world)

        async def missing(organization_id, *, product_id):
            return None

        monkeypatch.setattr(product_service.repo, "get_product", missing)

        with pytest.raises(ValueError):
            await product_service.delete(ORG, product_id=PID)

        assert not any(call[0] == "remove_row" for call in world.calls)


# ---------------------------------------------------------------------------
# 4. Tenant isolation: every call carries the caller's organisation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_list_update_and_delete_are_all_org_scoped(self, monkeypatch):
        world = _World(rows=[_product()], used=[])
        _patch(monkeypatch, world)

        await product_service.list_catalog(ORG)
        await product_service.update(ORG, product_id=PID, unit_price=2)
        await product_service.set_active(ORG, product_id=PID, active=False)
        await product_service.delete(ORG, product_id=PID)

        for call in world.calls:
            assert any(str(ORG) == str(part) for part in call[1:]), call

        world = _World(rows=[_service()])
        _patch(monkeypatch, world)

        with pytest.raises(ValueError) as err:
            await service_service.update(
                ORG, service_id=SID, billing_unit="FORTNIGHT"
            )

        assert "billing_unit" in str(err.value)
        assert world.calls == []

    @pytest.mark.asyncio
    async def test_service_update_applies_a_valid_billing_unit(self, monkeypatch):
        world = _World(rows=[_service()])
        _patch(monkeypatch, world)

        await service_service.update(ORG, service_id=SID, billing_unit="FIXED")

        assert world.calls[0][4] == ("billing_unit",)
