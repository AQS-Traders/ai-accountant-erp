"""
ERP AI Agent — Item-Level Parity & Intake Protocol Tests
=========================================================
The five acceptance tests required by the AI/MANUAL PARITY master prompt:

* test_invoice_items_parity        — AI-path invoice with items ⇒ items
                                     persisted via the repository, header
                                     totals recomputed from the lines.
* test_conversion_copies_items     — quotation → invoice conversion copies
                                     the line items; double conversion
                                     refused.
* test_capital_vs_expense_branch   — machinery-like item ⇒ the 4-way
                                     decision tree is ASKED; explicit
                                     answers re-route the intent.
* test_asset_acquisition_contract  — register + schedule + sub-ledger
                                     transaction + posted journal; declining
                                     depreciation ⇒ explicit no-schedule
                                     record.
* test_no_silent_inventory_flag    — is_stock_tracked is NEVER set or
                                     inferred silently.

Offline: database mocked at repository boundaries (same pattern as
test_cross_module_lifecycles.py).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.repositories import invoice_repository as inv_repo
from app.services import (
    fixed_asset_service,
    invoice_service,
    product_service,
    quotation_service,
)
from app.classifier import classify_transaction, rule_based_nature
from app.planner import plan
from app.reasoning import analyze_requirements, collect_open_questions

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")

CUSTOMER_ID = "dddddddd-0000-0000-0000-000000000001"
QUOTATION_ID = "eeeeeeee-0000-0000-0000-000000000001"
INVOICE_ID = "eeeeeeee-0000-0000-0000-000000000002"
ASSET_ID = "eeeeeeee-0000-0000-0000-000000000003"
JOURNAL_ID = "cccccccc-0000-0000-0000-000000000009"
ASSET_ACC_ID = "bbbbbbbb-0000-0000-0000-000000000001"
CASH_ACC_ID = "bbbbbbbb-0000-0000-0000-000000000002"

ASSET_ACCOUNT = {
    "id": ASSET_ACC_ID, "code": "1520", "name": "Machinery",
    "account_type": "ASSET", "normal_balance": "DEBIT",
}
CASH_ACCOUNT = {
    "id": CASH_ACC_ID, "code": "1010", "name": "Cash in Hand",
    "account_type": "ASSET", "normal_balance": "DEBIT",
}

QUOTATION = {
    "id": QUOTATION_ID,
    "quotation_number": "QT-0001",
    "customer_id": CUSTOMER_ID,
    "status": "ACCEPTED",
    "currency_code": "PKR",
    "subtotal": 88000.0,
    "discount_total": 0.0,
    "tax_total": 0.0,
    "total": 88000.0,
    "project_id": None,
    "notes": None,
    "terms": None,
}

QUOTATION_ITEMS = [
    {
        "line_number": 1, "description": "Website design",
        "service_id": "aaaaaaa1-0000-0000-0000-000000000001",
        "product_id": None, "project_id": None,
        "quantity": 10, "unit_price": 8000.0, "discount_amount": 0,
        "tax_rate_id": "ccccccc1-0000-0000-0000-000000000001",
        "tax_amount": 0, "line_total": 80000.0,
    },
    {
        "line_number": 2, "description": "Hosting appliance",
        "service_id": None,
        "product_id": "aaaaaaa2-0000-0000-0000-000000000002",
        "project_id": None,
        "quantity": 1, "unit_price": 8000.0, "discount_amount": 0,
        "tax_rate_id": None, "tax_amount": 0, "line_total": 8000.0,
    },
]


def _posted_journal(**over):
    entry = {"id": JOURNAL_ID, "status": "POSTED"}
    entry.update(over)
    return {"entry": entry, "lines": []}


# ===================================================================
# 1. Invoice items parity
# ===================================================================


class TestInvoiceItemsParity:

    async def _create(self, items):
        with patch(
            "app.services.customer_service.get",
            new=AsyncMock(return_value={"id": CUSTOMER_ID, "name": "TechVision"}),
        ), patch.object(
            inv_repo, "create_invoice",
            new=AsyncMock(return_value={
                "id": INVOICE_ID, "invoice_number": "INV-0001",
                "invoice_date": "2026-09-04", "total": 0.0,
            }),
        ) as create_header, patch(
            "app.repositories.invoice_item_repository.add_invoice_items",
            new=AsyncMock(
                side_effect=lambda org, *, invoice_id, items: [dict(i) for i in items]
            ),
        ) as add_items:
            result = await invoice_service.create_invoice(
                organization_id=ORG,
                customer_id=uuid.UUID(CUSTOMER_ID),
                items=items,
            )
        return result, add_items, create_header

    @pytest.mark.asyncio
    async def test_invoice_items_parity(self):
        """AI-path invoice with 2 named items ⇒ exactly 2 invoice_items rows
        through the SAME repository the manual path uses; header totals
        recomputed from the validated lines; line_total computed per line."""
        items = [
            {"description": "Widget A", "quantity": 2, "unit_price": 500.0},
            {
                "description": "Widget B", "quantity": 1, "unit_price": 1200.0,
                "discount_amount": 200.0, "tax_amount": 100.0,
            },
        ]
        result, add_items, create_header = await self._create(items)

        add_items.assert_awaited_once()
        saved = add_items.await_args.kwargs["items"]
        assert len(saved) == 2
        # line_total computed by the service — never trusted from the caller
        assert saved[0]["line_total"] == 1000.0
        assert saved[1]["line_total"] == 1100.0  # 1200 − 200 + 100

        # header totals recomputed from the validated lines and passed to
        # the repository (subtotal = Σ quantity×unit_price; total = Σ lines)
        header_kwargs = create_header.await_args.kwargs
        assert header_kwargs["subtotal"] == 2200.0
        assert header_kwargs["discount_total"] == 200.0
        assert header_kwargs["tax_total"] == 100.0
        assert header_kwargs["total"] == 2100.0
        assert result["item_count"] == 2
        assert result["items"][0]["description"] == "Widget A"

    @pytest.mark.asyncio
    async def test_invalid_line_refuses_the_whole_invoice(self):
        """A bad line must fail BEFORE the header is written — no
        header-only partial state, identical validation for both paths."""
        for bad in (
            [{"description": "", "quantity": 1, "unit_price": 5.0}],
            [{"description": "X", "quantity": 0, "unit_price": 5.0}],
            [{"description": "X", "quantity": 1, "unit_price": -5.0}],
        ):
            with patch(
                "app.services.customer_service.get",
                new=AsyncMock(return_value={"id": CUSTOMER_ID}),
            ), patch.object(
                inv_repo, "create_invoice", new=AsyncMock()
            ) as create_header, patch(
                "app.repositories.invoice_item_repository.add_invoice_items",
                new=AsyncMock(),
            ):
                with pytest.raises(ValueError):
                    await invoice_service.create_invoice(
                        organization_id=ORG,
                        customer_id=uuid.UUID(CUSTOMER_ID),
                        items=bad,
                    )
            create_header.assert_not_awaited()


# ===================================================================
# 2. Quotation conversion copies items
# ===================================================================


class TestConversionCopiesItems:

    @pytest.mark.asyncio
    async def test_conversion_copies_items(self):
        """Conversion copies every quotation line into invoice_items with
        product/service/tax references preserved; item count verified."""
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value=dict(QUOTATION)),
        ), patch.object(
            quotation_service.repo, "get_quotation_items",
            new=AsyncMock(return_value=QUOTATION_ITEMS),
        ), patch.object(
            invoice_service, "create_invoice",
            new=AsyncMock(return_value={
                "id": INVOICE_ID, "total": 88000.0,
                "invoice_date": "2026-09-01", "item_count": 2,
            }),
        ) as create_invoice, patch(
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
        ):
            result = await quotation_service.convert_quotation(
                ORG, quotation_id=uuid.UUID(QUOTATION_ID)
            )

        kwargs = create_invoice.await_args.kwargs
        copied = kwargs["items"]
        assert len(copied) == len(QUOTATION_ITEMS) == 2
        assert copied[0]["service_id"] == QUOTATION_ITEMS[0]["service_id"]
        assert copied[0]["tax_rate_id"] == QUOTATION_ITEMS[0]["tax_rate_id"]
        assert copied[1]["product_id"] == QUOTATION_ITEMS[1]["product_id"]
        assert copied[0]["quantity"] == 10
        assert copied[0]["unit_price"] == 8000.0
        assert result["invoice"]["item_count"] == 2

    @pytest.mark.asyncio
    async def test_double_conversion_is_refused(self):
        with patch.object(
            quotation_service.repo, "get_quotation",
            new=AsyncMock(return_value={**QUOTATION, "status": "CONVERTED"}),
        ), patch.object(
            quotation_service.repo, "get_quotation_items", new=AsyncMock()
        ), patch.object(
            invoice_service, "create_invoice", new=AsyncMock()
        ) as create_invoice:
            with pytest.raises(ValueError):
                await quotation_service.convert_quotation(
                    ORG, quotation_id=uuid.UUID(QUOTATION_ID)
                )
        create_invoice.assert_not_awaited()


# ===================================================================
# 3. Capital vs expense vs inventory decision branch
# ===================================================================


class TestCapitalVsExpenseBranch:

    def test_machinery_item_triggers_the_decision_question(self):
        """The consolidated round asks the explicit 4-way decision tree for
        a plausibly-capital item — it is never guessed away."""
        nodes = analyze_requirements(
            intent="record_cash_purchase",
            entities={"amount": 150000.0, "item": "CNC machinery"},
            missing_fields=[],
        )
        questions = collect_open_questions(nodes)
        nature_q = [q for q in questions if "FIXED ASSET" in q["question"]]
        assert nature_q, "the 4-way nature decision must be asked"
        assert "INVENTORY-STOCKED PRODUCT" in nature_q[0]["question"]
        assert "CONSUMABLE" in nature_q[0]["question"]
        assert "SERVICE" in nature_q[0]["question"]
        # line detail capture in the SAME consolidated round
        assert any(
            "quantity" in q["question"].lower() for q in questions
        )

    @pytest.mark.asyncio
    async def test_durable_catalog_product_is_not_silently_classified(self):
        """A durable catalog product NOT marked stock-tracked must trigger
        clarification — the catalog flag alone must never decide
        INVENTORY vs OPERATING_EXPENSE."""
        with patch(
            "app.database.fetch_many",
            new=AsyncMock(return_value=[
                {"id": "p1", "name": "laptop", "is_stock_tracked": False}
            ]),
        ):
            cls = await classify_transaction(
                organization_id=ORG,
                intent="record_cash_purchase",
                entities={"item_description": "laptop", "amount": 150000.0},
                message="Bought a laptop for 150000",
            )
        assert cls.requires_clarification is True
        assert cls.transaction_nature is None

    @pytest.mark.asyncio
    async def test_stock_tracked_catalog_product_is_authoritative(self):
        with patch(
            "app.database.fetch_many",
            new=AsyncMock(return_value=[
                {"id": "p1", "name": "laptop", "is_stock_tracked": True}
            ]),
        ):
            cls = await classify_transaction(
                organization_id=ORG,
                intent="record_cash_purchase",
                entities={"item_description": "laptop"},
                message="Bought a laptop",
            )
        assert cls.transaction_nature == "INVENTORY"
        assert cls.requires_clarification is False

    def test_explicit_asset_answer_routes_to_register_fixed_asset(self):
        p = plan(
            "Bought machinery for the workshop",
            clarification_history=[{
                "question": (
                    "Is this item: (a) a FIXED ASSET, (b) an "
                    "INVENTORY-STOCKED PRODUCT, (c) a CONSUMABLE, or "
                    "(d) a SERVICE?"
                ),
                "answer": "a",
            }, {
                # Work Stream R2: the TREATMENT decides the destination -
                # routing fires once the cash/credit answer is known.
                "question": "Was this paid in cash or on credit?",
                "answer": "cash",
            }],
        )
        assert p.intent == "register_fixed_asset"
        assert p.extracted_entities["transaction_nature"] == "FIXED_ASSET"
        assert p.extracted_entities["asset_name"] == "machinery"

    def test_expense_answer_routes_to_record_expense(self):
        p = plan(
            "Bought machinery for the workshop",
            clarification_history=[{
                "question": (
                    "Is this item: (a) a FIXED ASSET, (b) an "
                    "INVENTORY-STOCKED PRODUCT, (c) a CONSUMABLE, or "
                    "(d) a SERVICE?"
                ),
                "answer": "consumable expense",
            }, {
                "question": "Was this paid in cash or on credit?",
                "answer": "cash",
            }],
        )
        assert p.intent == "record_expense"
        assert p.extracted_entities["transaction_nature"] == "OPERATING_EXPENSE"

    def test_line_detail_answer_is_parsed(self):
        p = plan(
            "Sold chairs to a customer",
            clarification_history=[{
                "question": "What quantity and unit price apply to 'chairs'?",
                "answer": "5 at 2,000 each",
            }],
        )
        assert p.extracted_entities.get("item_quantity") == 5.0
        assert p.extracted_entities.get("item_unit_price") == 2000.0

    def test_durable_nature_is_low_confidence_without_configuration(self):
        """The rules engine never hard-classifies a durable good — it stays
        at LOW confidence so the clarification branch fires."""
        nature, confidence = rule_based_nature("brand new machinery", {})
        assert nature is None or confidence == "LOW"


# ===================================================================
# 4. Fixed-asset acquisition contract
# ===================================================================


def _account_side_effect(_):
    async def _get(organization_id, *, account_id=None, **kw):
        if str(account_id) == ASSET_ACC_ID:
            return dict(ASSET_ACCOUNT)
        if str(account_id) == CASH_ACC_ID:
            return dict(CASH_ACCOUNT)
        return None
    return _get


class TestAssetAcquisitionContract:

    async def _register(self, useful_life_years):
        with patch.object(
            fixed_asset_service.repo, "create_asset",
            new=AsyncMock(return_value={
                "id": ASSET_ID, "name": "CNC Machine", "status": "ACTIVE",
            }),
        ) as create_asset, patch.object(
            fixed_asset_service.repo, "update_asset_by_id", new=AsyncMock()
        ), patch.object(
            fixed_asset_service.repo, "record_asset_transaction",
            new=AsyncMock(),
        ) as record_txn, patch.object(
            fixed_asset_service.repo, "record_depreciation_schedule",
            new=AsyncMock(),
        ) as record_schedule, patch.object(
            fixed_asset_service.account_repo, "get_account",
            new=AsyncMock(side_effect=_account_side_effect(None)),
        ), patch.object(
            fixed_asset_service.accounting_service, "prepare_journal",
            new=AsyncMock(return_value={
                "entry": {"id": JOURNAL_ID, "status": "DRAFT"}
            }),
        ) as prepare, patch.object(
            fixed_asset_service.accounting_service, "validate_journal",
            new=AsyncMock(),
        ), patch.object(
            fixed_asset_service.accounting_service, "post_journal",
            new=AsyncMock(return_value={"success": True}),
        ) as post:
            result = await fixed_asset_service.register_asset(
                ORG,
                name="CNC Machine",
                purchase_cost=250000.0,
                useful_life_years=useful_life_years,
                payment_method="CASH",
                asset_account_id=uuid.UUID(ASSET_ACC_ID),
                payment_account_id=uuid.UUID(CASH_ACC_ID),
            )
        return result, create_asset, record_txn, record_schedule, prepare, post

    @pytest.mark.asyncio
    async def test_asset_acquisition_contract(self):
        """Register + schedule + PURCHASE sub-ledger row + posted journal —
        all four artifacts created and disclosed."""
        result, create_asset, record_txn, record_schedule, prepare, post = (
            await self._register(useful_life_years=5)
        )

        create_asset.assert_awaited_once()
        record_schedule.assert_awaited_once()
        sched_kwargs = record_schedule.await_args.kwargs
        assert sched_kwargs["opening_book_value"] == 250000.0
        assert sched_kwargs["depreciation_amount"] == 0.0
        assert sched_kwargs["closing_book_value"] == 250000.0
        assert str(sched_kwargs["journal_entry_id"]) == JOURNAL_ID

        record_txn.assert_awaited_once()
        txn_kwargs = record_txn.await_args.kwargs
        assert txn_kwargs["transaction_type"] == "PURCHASE"
        assert txn_kwargs["amount"] == 250000.0
        assert str(txn_kwargs["journal_entry_id"]) == JOURNAL_ID

        prepare.assert_awaited_once()
        post.assert_awaited_once()

        assert result["asset_registered"] is True
        assert result["journal_posted"] is True
        assert result["depreciation_schedule_created"] is True
        assert result["depreciation_declined"] is False
        assert result["asset_account"] == "Machinery"
        assert result["settlement_account"] == "Cash in Hand"

    @pytest.mark.asyncio
    async def test_declining_depreciation_is_recorded_not_silent(self):
        """No useful life ⇒ NO schedule row, but the decline is recorded
        explicitly in the sub-ledger details AND the summary."""
        result, _, record_txn, record_schedule, prepare, post = (
            await self._register(useful_life_years=None)
        )
        record_schedule.assert_not_awaited()
        details = record_txn.await_args.kwargs["details"]
        assert "DECLINED" in str(details.get("depreciation", ""))
        assert result["depreciation_schedule_created"] is False
        assert result["depreciation_declined"] is True
        assert result["asset_registered"] is True
        assert result["journal_posted"] is True


# ===================================================================
# 5. No silent inventory flag
# ===================================================================


class TestNoSilentInventoryFlag:

    @pytest.mark.asyncio
    async def test_flag_defaults_to_false_without_explicit_answer(self):
        """Creating a product WITHOUT the inventory question must leave
        is_stock_tracked=False — the flag is never inferred."""
        with patch.object(
            product_service.repo, "search_products",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            product_service.repo, "create_product",
            new=AsyncMock(side_effect=lambda **kw: {"id": "p-new", **kw}),
        ) as create_product:
            result = await product_service.create(
                ORG, name="Consulting widget", unit_price=1000.0
            )
        kwargs = create_product.await_args.kwargs
        assert kwargs["is_stock_tracked"] is False
        assert result["is_stock_tracked"] is False

    @pytest.mark.asyncio
    async def test_flag_is_set_only_when_explicitly_passed(self):
        with patch.object(
            product_service.repo, "search_products",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            product_service.repo, "create_product",
            new=AsyncMock(side_effect=lambda **kw: {"id": "p-new", **kw}),
        ) as create_product:
            await product_service.create(
                ORG, name="Resale gadget", unit_price=1000.0,
                is_stock_tracked=True,
            )
        # true ONLY because the caller (the user's explicit answer) said so
        assert create_product.await_args.kwargs["is_stock_tracked"] is True

    def test_planner_never_sets_the_inventory_flag(self):
        """The intake branch records only the nature DECISION — it never
        flips a catalog stock flag as a side effect."""
        p = plan(
            "Bought machinery for the workshop",
            clarification_history=[{
                "question": "Is this item: (a) FIXED ASSET …",
                "answer": "inventory stock",
            }],
        )
        assert p.extracted_entities.get("transaction_nature") == "INVENTORY"
        assert "is_stock_tracked" not in p.extracted_entities
