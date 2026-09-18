"""
ERP AI Agent — Cross-Module Lifecycle & Integrity Tests (Phase 4)
===================================================================
Complete business lifecycles that cross module boundaries, verified at
the module-contract level (offline — database mocked at repository
boundaries; live verification is performed separately by
scripts/test_cross_module_live.py).

Verified dimensions:
* quotation → invoice conversion (source linkage, amount preservation,
  double-conversion refusal, no accounting at quotation stage)
* receivable settlement: partial receipt → PARTIALLY_PAID → PAID,
  allocation rows, source-tied posted journal
* payable settlement: partial supplier payment → PARTIALLY_PAID → PAID,
  allocation rows, source-tied posted journal
* no unnecessary cross-module creation (conversion never re-creates
  the customer or invents records)
* reasoning wiring: convert_quotation intent, DOCUMENT economic event,
  tool registry, entity contract, minimal clarification
* tax passthrough: tax carried inside the invoice total — never an
  invented tax-account split (DB has no tax GL mapping)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.repositories import invoice_repository as inv_repo
from app.services import invoice_service, payment_service, quotation_service
from app.tools import get_handler, list_tools
from app.reasoning import classify_economic_event
from app.planner import plan

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")

CUSTOMER_ID = "dddddddd-0000-0000-0000-000000000001"
SUPPLIER_ID = "dddddddd-0000-0000-0000-000000000002"
QUOTATION_ID = "eeeeeeee-0000-0000-0000-000000000001"
INVOICE_ID = "eeeeeeee-0000-0000-0000-000000000002"
JOURNAL_ID = "cccccccc-0000-0000-0000-000000000009"
RECEIPT_ID = "eeeeeeee-0000-0000-0000-000000000003"
PAYMENT_ID = "eeeeeeee-0000-0000-0000-000000000004"

CUSTOMER = {"id": CUSTOMER_ID, "name": "TechVision", "code": "CUST-1"}
QUOTATION = {
    "id": QUOTATION_ID,
    "quotation_number": "QT-0001",
    "customer_id": CUSTOMER_ID,
    "status": "ACCEPTED",
    "currency_code": "PKR",
    "subtotal": 100000.0,
    "discount_total": 0.0,
    "tax_total": 17000.0,
    "total": 117000.0,
    "project_id": None,
    "notes": "Website build",
    "terms": "30 days",
}


def _posted_journal(**over):
    """Shape returned by engine.* journal builders: {"entry": row, ...}."""
    entry = {"id": JOURNAL_ID, "status": "POSTED"}
    entry.update(over)
    return {"entry": entry, "lines": []}


def _auto_journal_result():
    """Shape returned by accounting_engine.auto_journal: the prepare_journal
    result nested under "journal_entry" ({"journal_entry": {"entry": row}}) —
    mirrors tools/_create_invoice's contract."""
    return {"journal_entry": _posted_journal(), "lines": []}


# ===================================================================
# Quotation → Invoice conversion (Priority 3)
# ===================================================================


class TestQuotationConversionLifecycle:

    @pytest.mark.asyncio
    async def test_conversion_preserves_source_linkage_and_amounts(self):
        """Full conversion: quotation → invoice (quotation_id FK) → posted
        journal → quotation CONVERTED."""
        created_invoice = {
            "id": INVOICE_ID,
            "invoice_number": "INV-0001",
            "customer_id": CUSTOMER_ID,
            "quotation_id": QUOTATION_ID,
            "invoice_date": "2026-09-01",
            "subtotal": 100000.0,
            "tax_total": 17000.0,
            "total": 117000.0,
            "status": "DRAFT",
        }
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=dict(QUOTATION)),
        ), patch.object(
            quotation_service.repo, "get_quotation_items",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            invoice_service, "create_invoice",
            new=AsyncMock(return_value=dict(created_invoice)),
        ) as create_inv, patch(
            "app.accounting_engine.auto_journal",
            new=AsyncMock(return_value=_auto_journal_result()),
        ) as auto_j, patch(
            "app.services.accounting_service.validate_journal",
            new=AsyncMock(),
        ) as validate_j, patch(
            "app.services.accounting_service.post_journal",
            new=AsyncMock(),
        ) as post_j, patch(
            "app.repositories.invoice_repository.link_journal_to_invoice",
            new=AsyncMock(),
        ) as link_j, patch(
            "app.repositories.invoice_repository.mark_issued",
            new=AsyncMock(),
        ) as issued_j, patch.object(
            quotation_service.repo, "mark_converted",
            new=AsyncMock(return_value={**QUOTATION, "status": "CONVERTED"}),
        ) as converted:
            result = await quotation_service.convert_quotation(
                ORG, quotation_id=uuid.UUID(QUOTATION_ID)
            )

        # Invoice carries the source linkage and preserved amounts
        create_inv.assert_awaited_once()
        kwargs = create_inv.await_args.kwargs
        assert kwargs["quotation_id"] == uuid.UUID(QUOTATION_ID)
        assert kwargs["customer_id"] == uuid.UUID(CUSTOMER_ID)
        assert kwargs["total"] == 117000.0
        assert kwargs["tax_total"] == 17000.0
        assert kwargs["subtotal"] == 100000.0
        # Accounting: journal prepared, validated, posted, linked to the
        # invoice — source-tied.
        auto_j.assert_awaited_once()
        assert auto_j.await_args.kwargs["document_type"] == "invoice"
        assert auto_j.await_args.kwargs["amount"] == 117000.0
        assert auto_j.await_args.kwargs["customer_id"] == uuid.UUID(CUSTOMER_ID)
        validate_j.assert_awaited_once()
        post_j.assert_awaited_once()
        link_j.assert_awaited_once()
        assert link_j.await_args.kwargs["invoice_id"] == uuid.UUID(INVOICE_ID)
        assert link_j.await_args.kwargs["journal_entry_id"] == uuid.UUID(JOURNAL_ID)
        # Quotation transitioned AFTER the invoice exists
        converted.assert_awaited_once()
        assert result["converted"] is True
        assert result["invoice"]["id"] == INVOICE_ID
        assert result["invoice"]["journal_posted"] is True
        assert result["quotation"]["status"] == "CONVERTED"

    @pytest.mark.asyncio
    async def test_conversion_refuses_unknown_quotation(self):
        """MISSING dependency: no quotation → no invoice, no journal,
        no status mutation."""
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=None),
        ), patch.object(
            invoice_service, "create_invoice", new=AsyncMock()
        ) as create_inv, patch(
            "app.accounting_engine.auto_journal", new=AsyncMock()
        ), patch.object(
            quotation_service.repo, "mark_converted", new=AsyncMock()
        ) as converted:
            with pytest.raises(ValueError, match="not found"):
                await quotation_service.convert_quotation(
                    ORG, quotation_id=uuid.UUID(QUOTATION_ID)
                )
        create_inv.assert_not_awaited()
        converted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_conversion_refuses_customer_mismatch(self):
        """The receivable can never be attached to the wrong party: a
        quotation belonging to customer A is refused for customer B."""
        from app.repositories import quotation_repository as quote_repo
        from app.services import customer_service
        other_customer = {
            **QUOTATION, "customer_id": "dddddddd-9999-9999-9999-999999999999",
        }
        with patch.object(
            customer_service, "get",
            new=AsyncMock(return_value=dict(CUSTOMER)),
        ), patch.object(
            quote_repo, "get_quotation",
            new=AsyncMock(return_value=other_customer),
        ), patch.object(
            inv_repo, "create_invoice", new=AsyncMock()
        ) as create_inv:
            with pytest.raises(ValueError, match="different customer"):
                await invoice_service.create_invoice(
                    organization_id=ORG,
                    customer_id=uuid.UUID(CUSTOMER_ID),
                    quotation_id=uuid.UUID(QUOTATION_ID),
                )
        create_inv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_double_conversion_is_refused(self):
        """A CONVERTED quotation can never be converted again — duplicate
        revenue/receivable is prevented BEFORE any mutation."""
        converted_quotation = {**QUOTATION, "status": "CONVERTED"}
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=converted_quotation),
        ), patch.object(
            invoice_service, "create_invoice", new=AsyncMock()
        ) as create_inv, patch(
            "app.accounting_engine.auto_journal", new=AsyncMock()
        ) as auto_j, patch.object(
            quotation_service.repo, "mark_converted", new=AsyncMock()
        ) as converted:
            with pytest.raises(ValueError, match="already been converted"):
                await quotation_service.convert_quotation(
                    ORG, quotation_id=uuid.UUID(QUOTATION_ID)
                )
        create_inv.assert_not_awaited()
        auto_j.assert_not_awaited()
        converted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quotation_creation_posts_no_accounting(self):
        """Accounting occurs only at the appropriate stage: creating a
        quotation is a DOCUMENT event and must not touch the journal."""
        with patch.object(
            quotation_service.customer_repo, "get_customer",
            new=AsyncMock(return_value=dict(CUSTOMER)),
        ), patch.object(
            quotation_service.repo, "create_quotation",
            new=AsyncMock(return_value={"id": QUOTATION_ID, "total": 117000.0}),
        ) as create_q, patch(
            "app.accounting_engine.auto_journal", new=AsyncMock()
        ) as auto_j:
            await quotation_service.create_quotation(
                ORG,
                customer_id=CUSTOMER_ID,
                items=[{
                    "description": "Website build",
                    "quantity": 1,
                    "unit_price": 100000.0,
                    "tax_amount": 17000.0,
                    "line_total": 117000.0,
                }],
            )
        create_q.assert_awaited_once()
        auto_j.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_conversion_does_not_recreate_the_customer(self):
        """No unnecessary cross-module creation: conversion resolves the
        customer from the quotation — it never calls customer creation."""
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=dict(QUOTATION)),
        ), patch.object(
            quotation_service.repo, "get_quotation_items",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            invoice_service, "create_invoice",
            new=AsyncMock(return_value={"id": INVOICE_ID, "total": 117000.0,
                                        "invoice_date": "2026-09-01"}),
        ), patch(
            "app.accounting_engine.auto_journal",
            new=AsyncMock(return_value=_posted_journal()),
        ), patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ), patch(
            "app.services.accounting_service.post_journal", new=AsyncMock()
        ), patch(
            "app.repositories.invoice_repository.link_journal_to_invoice",
            new=AsyncMock(),
        ), patch(
            "app.repositories.invoice_repository.mark_issued",
            new=AsyncMock(),
        ), patch.object(
            quotation_service.repo, "mark_converted",
            new=AsyncMock(return_value={**QUOTATION, "status": "CONVERTED"}),
        ), patch.object(
            quotation_service.customer_repo, "create_customer",
            new=AsyncMock(side_effect=AssertionError("unnecessary creation")),
        ):
            await quotation_service.convert_quotation(
                ORG, quotation_id=uuid.UUID(QUOTATION_ID)
            )

    @pytest.mark.asyncio
    async def test_conversion_failed_invoice_never_converts_quotation(self):
        """If the invoice creation fails, the quotation must remain
        unconverted (no orphan CONVERTED status without a document)."""
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=dict(QUOTATION)),
        ), patch.object(
            quotation_service.repo, "get_quotation_items",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            invoice_service, "create_invoice",
            new=AsyncMock(side_effect=ValueError("Customer not found")),
        ), patch.object(
            quotation_service.repo, "mark_converted", new=AsyncMock()
        ) as converted:
            with pytest.raises(ValueError):
                await quotation_service.convert_quotation(
                    ORG, quotation_id=uuid.UUID(QUOTATION_ID)
                )
        converted.assert_not_awaited()


# ===================================================================
# Receivable settlement lifecycle (Priority 2)
# ===================================================================


class TestReceivableSettlementLifecycle:

    @pytest.mark.asyncio
    async def test_partial_receipt_sets_partially_paid(self):
        """Partial settlement: amount_paid increments, status becomes
        PARTIALLY_PAID (still an open receivable)."""
        invoice = {"id": INVOICE_ID, "total": 117000.0, "amount_paid": 0.0,
                   "status": "ISSUED"}
        with patch.object(
            inv_repo, "get_invoice", new=AsyncMock(return_value=invoice),
        ), patch.object(
            payment_service, "update_one", new=AsyncMock(),
        ) as upd:
            await payment_service._update_invoice_paid(
                ORG, uuid.UUID(INVOICE_ID), 50000.0
            )
        upd.assert_awaited_once()
        data = upd.await_args.kwargs["data"]
        assert data["amount_paid"] == 50000.0
        assert data["status"] == "PARTIALLY_PAID"

    @pytest.mark.asyncio
    async def test_complete_receipt_sets_paid(self):
        invoice = {"id": INVOICE_ID, "total": 117000.0,
                   "amount_paid": 67000.0, "status": "PARTIALLY_PAID"}
        with patch.object(
            inv_repo, "get_invoice", new=AsyncMock(return_value=invoice),
        ), patch.object(
            payment_service, "update_one", new=AsyncMock(),
        ) as upd:
            await payment_service._update_invoice_paid(
                ORG, uuid.UUID(INVOICE_ID), 50000.0
            )
        data = upd.await_args.kwargs["data"]
        assert data["amount_paid"] == 117000.0
        assert data["status"] == "PAID"

    @pytest.mark.asyncio
    async def test_receipt_is_source_tied_and_allocated(self):
        """A receipt against an invoice: posted journal tied to the
        receipt id, allocation row, invoice settlement — all three."""
        async def _rpc(name, *, params=None):
            assert name == "create_receipt_atomic", name
            return {
                "receipt": {"id": RECEIPT_ID, "amount": 50000.0},
                "journal_entry_id": JOURNAL_ID,
            }

        rpc = AsyncMock(side_effect=_rpc)
        with patch.object(
            payment_service, "call_rpc", new=rpc,
        ), patch.object(
            payment_service, "_resolve_settlement_gl",
            new=AsyncMock(return_value=uuid.UUID(CUSTOMER_ID)),
        ), patch.object(
            payment_service, "_resolve_receipt_credit_account",
            new=AsyncMock(return_value=(uuid.UUID(SUPPLIER_ID), None)),
        ), patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ) as validate_j, patch(
            "app.services.accounting_service.post_journal", new=AsyncMock()
        ) as post_j, patch.object(
            payment_service.repo, "create_receipt_allocation",
            new=AsyncMock(return_value={"id": "alloc-1"}),
        ) as alloc, patch.object(
            payment_service, "_update_invoice_paid", new=AsyncMock(),
        ) as settle:
            await payment_service.record_customer_receipt(
                organization_id=ORG,
                customer_id=uuid.UUID(CUSTOMER_ID),
                amount=50000.0,
                invoice_id=uuid.UUID(INVOICE_ID),
            )
        rpc.assert_awaited_once()
        params = rpc.await_args.kwargs["params"]
        # the journal goes in WITH the receipt — one transaction, balanced
        lines = params["p_journal"]["lines"]
        assert lines[0]["debit"] == 50000.0
        assert lines[1]["credit"] == 50000.0
        assert lines[1]["customer_id"] == CUSTOMER_ID
        # the DRAFT entry the RPC linked is validate+posted
        validate_j.assert_awaited_once_with(entry_id=uuid.UUID(JOURNAL_ID))
        post_j.assert_awaited_once_with(entry_id=uuid.UUID(JOURNAL_ID))
        # Allocation row + settlement against the SOURCE invoice
        alloc.assert_awaited_once()
        ak = alloc.await_args.kwargs
        assert str(ak["invoice_id"]) == INVOICE_ID
        assert str(ak["receipt_id"]) == RECEIPT_ID
        assert ak["amount_allocated"] == 50000.0
        settle.assert_awaited_once_with(ORG, uuid.UUID(INVOICE_ID), 50000.0)

    @pytest.mark.asyncio
    async def test_receipt_without_invoice_still_posts_journal(self):
        """A receipt with no allocation target still records cash
        (unapplied receipt) — but never invents an invoice."""
        async def _rpc(name, *, params=None):
            assert name == "create_receipt_atomic", name
            return {
                "receipt": {"id": RECEIPT_ID, "amount": 20000.0},
                "journal_entry_id": JOURNAL_ID,
            }

        with patch.object(
            payment_service, "call_rpc", new=AsyncMock(side_effect=_rpc),
        ), patch.object(
            payment_service, "_resolve_settlement_gl",
            new=AsyncMock(return_value=uuid.UUID(CUSTOMER_ID)),
        ), patch.object(
            payment_service, "_resolve_receipt_credit_account",
            new=AsyncMock(return_value=(uuid.UUID(SUPPLIER_ID), None)),
        ), patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ), patch(
            "app.services.accounting_service.post_journal", new=AsyncMock()
        ) as post_j, patch.object(
            payment_service.repo, "create_receipt_allocation", new=AsyncMock()
        ) as alloc:
            await payment_service.record_customer_receipt(
                organization_id=ORG,
                customer_id=uuid.UUID(CUSTOMER_ID),
                amount=20000.0,
            )
        post_j.assert_awaited_once()
        alloc.assert_not_awaited()


# ===================================================================
# Payable settlement lifecycle (Priority 2)
# ===================================================================


class TestPayableSettlementLifecycle:

    @pytest.mark.asyncio
    async def test_partial_payment_sets_partially_paid(self):
        bill = {"id": INVOICE_ID, "total": 90000.0, "amount_paid": 0.0,
                "status": "OPEN"}
        with patch.object(
            payment_service.pur_repo, "get_purchase_bill",
            new=AsyncMock(return_value=bill),
        ), patch.object(
            payment_service, "update_one", new=AsyncMock(),
        ) as upd:
            await payment_service._update_bill_paid(
                ORG, uuid.UUID(INVOICE_ID), 40000.0
            )
        data = upd.await_args.kwargs["data"]
        assert data["amount_paid"] == 40000.0
        assert data["status"] == "PARTIALLY_PAID"

    @pytest.mark.asyncio
    async def test_complete_payment_sets_paid(self):
        bill = {"id": INVOICE_ID, "total": 90000.0, "amount_paid": 40000.0,
                "status": "PARTIALLY_PAID"}
        with patch.object(
            payment_service.pur_repo, "get_purchase_bill",
            new=AsyncMock(return_value=bill),
        ), patch.object(
            payment_service, "update_one", new=AsyncMock(),
        ) as upd:
            await payment_service._update_bill_paid(
                ORG, uuid.UUID(INVOICE_ID), 50000.0
            )
        data = upd.await_args.kwargs["data"]
        assert data["amount_paid"] == 90000.0
        assert data["status"] == "PAID"

    @pytest.mark.asyncio
    async def test_supplier_payment_is_source_tied_and_allocated(self):
        async def _rpc(name, *, params=None):
            assert name == "create_payment_atomic", name
            return {
                "payment": {"id": PAYMENT_ID, "amount": 90000.0},
                "journal_entry_id": JOURNAL_ID,
            }

        rpc = AsyncMock(side_effect=_rpc)
        with patch.object(
            payment_service, "call_rpc", new=rpc,
        ), patch.object(
            payment_service, "_resolve_settlement_gl",
            new=AsyncMock(return_value=uuid.UUID(CUSTOMER_ID)),
        ), patch.object(
            payment_service, "_resolve_payment_debit_account",
            new=AsyncMock(return_value=(uuid.UUID(SUPPLIER_ID), None)),
        ), patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ), patch(
            "app.services.accounting_service.post_journal", new=AsyncMock()
        ) as post_j, patch.object(
            payment_service.repo, "create_payment_allocation",
            new=AsyncMock(return_value={"id": "alloc-2"}),
        ) as alloc, patch.object(
            payment_service, "_update_bill_paid", new=AsyncMock(),
        ) as settle:
            await payment_service.record_supplier_payment(
                organization_id=ORG,
                supplier_id=uuid.UUID(SUPPLIER_ID),
                amount=90000.0,
                bill_id=uuid.UUID(INVOICE_ID),
            )
        rpc.assert_awaited_once()
        params = rpc.await_args.kwargs["params"]
        # DB contract: supplier payments are OUTFLOW, source-tied to the supplier
        assert params["p_payment"]["direction"] == "OUTFLOW"
        assert params["p_payment"]["supplier_id"] == SUPPLIER_ID
        lines = params["p_journal"]["lines"]
        assert lines[0]["debit"] == 90000.0
        assert lines[0]["supplier_id"] == SUPPLIER_ID
        assert lines[1]["credit"] == 90000.0
        post_j.assert_awaited_once_with(entry_id=uuid.UUID(JOURNAL_ID))
        alloc.assert_awaited_once()
        ak = alloc.await_args.kwargs
        assert str(ak["bill_id"]) == INVOICE_ID
        assert str(ak["payment_id"]) == PAYMENT_ID
        settle.assert_awaited_once_with(ORG, uuid.UUID(INVOICE_ID), 90000.0)


# ===================================================================
# Tax passthrough audit (Priority 6 — DB contract, not invention)
# ===================================================================


class TestTaxPassthroughAudit:

    @pytest.mark.asyncio
    async def test_tax_is_carried_inside_the_total_not_split(self):
        """The invoice carries tax_total inside the document total and the
        journal bills the FULL total — the DB has no tax→GL account
        mapping (taxes/tax_categories have no account columns), so tax
        accounting is passthrough-only.  A dedicated tax-liability split
        would require inventing an account mapping — BLOCKED.
        """
        created_invoice = {
            "id": INVOICE_ID,
            "invoice_number": "INV-0001",
            "customer_id": CUSTOMER_ID,
            "quotation_id": QUOTATION_ID,
            "invoice_date": "2026-09-01",
            "subtotal": 100000.0,
            "tax_total": 17000.0,
            "total": 117000.0,
        }
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=dict(QUOTATION)),
        ), patch.object(
            quotation_service.repo, "get_quotation_items",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            invoice_service, "create_invoice",
            new=AsyncMock(return_value=dict(created_invoice)),
        ), patch(
            "app.accounting_engine.auto_journal",
            new=AsyncMock(return_value=_posted_journal()),
        ) as auto_j, patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ), patch(
            "app.services.accounting_service.post_journal", new=AsyncMock()
        ), patch(
            "app.repositories.invoice_repository.link_journal_to_invoice",
            new=AsyncMock(),
        ), patch(
            "app.repositories.invoice_repository.mark_issued",
            new=AsyncMock(),
        ), patch.object(
            quotation_service.repo, "mark_converted",
            new=AsyncMock(return_value={**QUOTATION, "status": "CONVERTED"}),
        ):
            await quotation_service.convert_quotation(
                ORG, quotation_id=uuid.UUID(QUOTATION_ID)
            )
        # The full tax-inclusive total goes to the journal; no separate
        # tax line / tax rate is ever invented by the engine.
        assert auto_j.await_args.kwargs["amount"] == 117000.0


# ===================================================================
# Cross-module reasoning wiring
# ===================================================================


class TestConversionReasoningWiring:

    def test_intent_is_detected(self):
        p = plan("Convert quotation QT-0001 into an invoice for TechVision")
        assert p.intent == "convert_quotation"

    def test_intent_beats_generic_quotation_pattern(self):
        p = plan("convert the quotation to an invoice")
        assert p.intent == "convert_quotation"

    def test_economic_event_is_document(self):
        """Conversion mirrors create_invoice: a DOCUMENT whose receivable
        journal is the internal downstream consequence — not a separate
        guessed event."""
        assert classify_economic_event("convert_quotation").value == "DOCUMENT"

    def test_tool_is_registered_as_mutation(self):
        assert "convert_quotation" in list_tools()
        handler = get_handler("convert_quotation")
        assert handler is not None
        assert handler["read_only"] is False

    def test_plan_requires_confirmation_before_accounting(self):
        p = plan("convert the quotation to an invoice")
        assert p.requires_confirmation is True

    def test_plan_does_not_ask_amount_or_customer(self):
        """Minimal clarification (rule 25): the amount and customer are
        authoritative from the quotation — asking for them would be
        questionnaire noise.  No consolidated questionnaire needed."""
        p = plan("convert the quotation to an invoice")
        assert p.requires_clarification is False
        assert p.missing_fields == []
        assert p.clarification_questions == []

    def test_plan_uses_conversion_tool_dependency_first(self):
        p = plan("convert the quotation to an invoice")
        tools = p.potential_tools
        assert "convert_quotation" in tools
        # dependency-first: search before mutation
        assert tools.index("convert_quotation") > tools.index("search_customer")

    def test_entity_contract_covers_conversion(self):
        from app.entity_contract import _TOOL_ENTITY_MAP
        assert _TOOL_ENTITY_MAP["convert_quotation"] == ("invoice", "created")


# ===================================================================
# Cross-module chain integrity (document → journal → source)
# ===================================================================


class TestSourceTiedChainIntegrity:

    @pytest.mark.asyncio
    async def test_journal_linkage_chain_survives_partial_failure(self):
        """If posting fails, the warning is surfaced — success is never
        faked; the document linkage is skipped; the quotation is still
        converted only because the invoice DOCUMENT exists (document
        creation and posting are separable stages)."""
        created_invoice = {
            "id": INVOICE_ID,
            "invoice_number": "INV-0002",
            "customer_id": CUSTOMER_ID,
            "invoice_date": "2026-09-01",
            "total": 117000.0,
        }
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=dict(QUOTATION)),
        ), patch.object(
            quotation_service.repo, "get_quotation_items",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            invoice_service, "create_invoice",
            new=AsyncMock(return_value=dict(created_invoice)),
        ), patch(
            "app.accounting_engine.auto_journal",
            new=AsyncMock(return_value={"journal_entry": {
                "entry": {"id": JOURNAL_ID, "status": "DRAFT"}}}),
        ), patch(
            "app.services.accounting_service.validate_journal", new=AsyncMock()
        ), patch(
            "app.services.accounting_service.post_journal",
            new=AsyncMock(side_effect=RuntimeError("period closed")),
        ), patch(
            "app.repositories.invoice_repository.link_journal_to_invoice",
            new=AsyncMock(),
        ) as link_j, patch.object(
            quotation_service.repo, "mark_converted",
            new=AsyncMock(return_value={**QUOTATION, "status": "CONVERTED"}),
        ):
            result = await quotation_service.convert_quotation(
                ORG, quotation_id=uuid.UUID(QUOTATION_ID)
            )
        assert result["invoice"]["journal_posted"] is False
        assert "posting failed" in result["invoice"]["journal_warning"]
        link_j.assert_not_awaited()



# ===================================================================
# DB-contract regression: payment_status enum fix (found live, Phase 4)
# ===================================================================


class TestPaymentStatusContract:
    """receipts.status / payments.status are payment_status enums
    {PENDING, COMPLETED, CANCELLED, REVERSED} — the repositories used to
    write "DRAFT", which made EVERY receipt/payment insert fail against
    the live database.  Locked here so it can never regress."""

    @pytest.mark.asyncio
    async def test_receipt_created_as_pending_never_draft(self):
        from app.repositories import payment_repository as pay_repo
        with patch.object(
            pay_repo, "insert_one",
            new=AsyncMock(return_value={"id": RECEIPT_ID}),
        ) as ins:
            await pay_repo.create_receipt(
                organization_id=ORG,
                receipt_date="2026-09-01",
                customer_id=uuid.UUID(CUSTOMER_ID),
                amount=100.0,
            )
        data = ins.await_args.kwargs["data"]
        assert data["status"] == "PENDING"
        assert data["status"] != "DRAFT"

    @pytest.mark.asyncio
    async def test_payment_created_as_pending_never_draft(self):
        from app.repositories import payment_repository as pay_repo
        with patch.object(
            pay_repo, "insert_one",
            new=AsyncMock(return_value={"id": PAYMENT_ID}),
        ) as ins:
            await pay_repo.create_payment(
                organization_id=ORG,
                payment_date="2026-09-01",
                supplier_id=uuid.UUID(SUPPLIER_ID),
                amount=100.0,
            )
        data = ins.await_args.kwargs["data"]
        assert data["status"] == "PENDING"
        assert data["status"] != "DRAFT"

    @pytest.mark.asyncio
    async def test_journal_link_completes_the_receipt(self):
        from app.repositories import payment_repository as pay_repo
        with patch.object(
            pay_repo, "update_one", new=AsyncMock(return_value={}),
        ) as upd:
            await pay_repo.link_journal_to_receipt(
                receipt_id=uuid.UUID(RECEIPT_ID),
                journal_entry_id=uuid.UUID(JOURNAL_ID),
            )
        data = upd.await_args.kwargs["data"]
        assert data["status"] == "COMPLETED"
        assert data["journal_entry_id"] == JOURNAL_ID


