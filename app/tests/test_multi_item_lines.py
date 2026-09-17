"""Work Stream R4.10 — MULTI-LINE invoice items + catalog resolution.

Answers two live questions:

1. "If user sends multiple items + quantities in the same request, will
   it identify each?"  — YES: `2 laptops at 5000 each and 3 mice at 500`
   parses into DISTINCT invoice lines and the total is DERIVED (Σ qty ×
   price), never collapsed into one "Goods" line.

2. "If the product isn't found in the items list, will it add it
   automatically?"  — NEVER silently: a consolidated "Catalog check"
   round asks ONCE; YES creates the catalog entries and links them,
   NO keeps the lines as one-off free-text.  Exact catalog matches are
   always linked deterministically.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.planner import (
    _extract_line_items,
    _line_items_total,
    plan,
)
from app.reasoning import nature_question_for_intent

DATE_Q = "What is the transaction date? Reply TODAY, or the date as YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
ORG = uuid.uuid4()


class TestMultiItemParsing:
    def test_two_items_parsed_as_distinct_lines(self):
        lines = _extract_line_items(
            "2 laptops at 5000 each and 3 mice at 500"
        )
        assert lines == [
            {"description": "laptops", "quantity": 2.0, "unit_price": 5000.0},
            {"description": "mice", "quantity": 3.0, "unit_price": 500.0},
        ]
        assert _line_items_total(lines) == 11500.0

    def test_comma_and_at_variants(self):
        lines = _extract_line_items(
            "1 laptop for 50,000, 2 x keyboard @ 2,000"
        )
        assert lines is not None and len(lines) == 2
        assert lines[0] == {
            "description": "laptop", "quantity": 1.0, "unit_price": 50000.0,
        }
        assert lines[1]["description"] == "keyboard"
        assert lines[1]["quantity"] == 2.0
        assert lines[1]["unit_price"] == 2000.0

    def test_numbered_lines(self):
        lines = _extract_line_items(
            "1) 4 chairs at 1500 each 2) 1 table for 9000"
        )
        assert lines is not None and len(lines) == 2
        assert _line_items_total(lines) == 15000.0

    def test_single_item_is_not_multi(self):
        assert _extract_line_items("2 laptops at 5000 each") is None

    def test_plan_extracts_multi_item_request(self):
        p = plan(
            "create invoice for abc tech for "
            "2 laptops at 5000 each and 3 mice at 500"
        )
        assert p.intent == "create_invoice"
        ents = p.extracted_entities
        assert ents["line_items"] == [
            {"description": "laptops", "quantity": 2.0, "unit_price": 5000.0},
            {"description": "mice", "quantity": 3.0, "unit_price": 500.0},
        ]
        # Total is DERIVED from the lines — never the first number seen.
        assert ents["amount"] == 11500.0
        # The singular item/quantity questions are suppressed — every
        # line already carries its own detail.
        assert "item_description" not in p.missing_fields
        assert "quantity" not in p.missing_fields


class TestMultiItemAnswerMerge:
    def test_multi_item_answer_parses_into_lines(self):
        p = plan(
            "create invoice of 20,000",
            clarification_history=[
                {"question":
                 nature_question_for_intent("create_invoice"),
                 "answer": "a"},
                {"question":
                 "What item or service is being invoiced? (description "
                 "for the invoice line — e.g. 'Web development services', "
                 "'HP laptops')",
                 "answer": "2 laptops at 5000 each and 3 mice at 500"},
            ],
        )
        ents = p.extracted_entities
        assert ents.get("line_items") == [
            {"description": "laptops", "quantity": 2.0, "unit_price": 5000.0},
            {"description": "mice", "quantity": 3.0, "unit_price": 500.0},
        ]
        assert ents["amount"] == 11500.0
        assert "quantity" not in p.missing_fields


class TestMultiItemFastPath:
    def _plan(self, message):
        return plan(
            message,
            clarification_history=[
                {"question":
                 nature_question_for_intent("create_invoice"),
                 "answer": "a"},
                {"question": "Who is the customer?", "answer": "ABC Traders"},
                {"question": DATE_Q, "answer": "2026-09-01"},
            ],
        )

    @pytest.mark.asyncio
    async def test_multi_item_invoice_builds_one_line_per_item(self):
        from app.agent import _deterministic_mutation_calls

        p = self._plan(
            "create invoice for ABC Traders for "
            "2 laptops at 5000 each and 3 mice at 500"
        )
        assert p.requires_clarification is False
        with patch(
            "app.services.customer_service.search",
            new=AsyncMock(
                return_value=[{"id": str(uuid.UUID(int=300)),
                               "name": "ABC Traders"}]
            ),
        ), patch(
            "app.services.product_service.search",
            new=AsyncMock(return_value=[]),
        ):
            calls = await _deterministic_mutation_calls(
                organization_id=ORG,
                execution_plan=p,
                classification=None,
            )
        assert calls is not None and len(calls) == 1
        items = calls[0].arguments["items"]
        assert items == [
            {"description": "laptops", "quantity": 2.0, "unit_price": 5000.0},
            {"description": "mice", "quantity": 3.0, "unit_price": 500.0},
        ]

    @pytest.mark.asyncio
    async def test_fast_path_refuses_total_line_mismatch(self):
        from app.agent import _invoice_fast_path_call

        entities = {
            "amount": 99999.0,  # ≠ Σ lines (11500)
            "transaction_date": "2026-09-01",
            "transaction_nature": "GOODS",
            "customer_name": "ABC Traders",
            "line_items": [
                {"description": "laptops", "quantity": 2.0,
                 "unit_price": 5000.0},
                {"description": "mice", "quantity": 3.0,
                 "unit_price": 500.0},
            ],
        }
        # A stated total that disagrees with the lines is NEVER guessed
        # away — the fast path refuses and clarification/model decides.
        assert _invoice_fast_path_call(entities) is None


class TestCatalogCheck:
    """Q2: a product not in the items list is NEVER auto-added — the
    user decides once via the "Catalog check" round."""

    def _invoice_entities(self, nature="GOODS"):
        return {
            "amount": 11500.0,
            "transaction_date": "2026-09-01",
            "transaction_nature": nature,
            "customer_name": "ABC Traders",
            "line_items": [
                {"description": "laptops", "quantity": 2.0,
                 "unit_price": 5000.0},
                {"description": "mice", "quantity": 3.0,
                 "unit_price": 500.0},
            ],
        }

    def _plan_with_entities(self, entities):
        p = plan("create invoice for ABC Traders")
        p.extracted_entities.update(entities)
        return p

    @pytest.mark.asyncio
    async def test_unknown_items_ask_once(self):
        from app.agent import _catalog_check_question

        p = self._plan_with_entities(self._invoice_entities())
        with patch(
            "app.services.product_service.search",
            new=AsyncMock(return_value=[]),
        ):
            q = await _catalog_check_question(ORG, p)
        assert q is not None
        assert q["question"].startswith("Catalog check:")
        assert "'laptops'" in q["question"]
        assert "'mice'" in q["question"]
        assert len(q["options"]) == 2

    @pytest.mark.asyncio
    async def test_exact_catalog_match_never_asks(self):
        from app.agent import _catalog_check_question

        p = self._plan_with_entities(self._invoice_entities())
        with patch(
            "app.services.product_service.search",
            new=AsyncMock(return_value=[
                {"id": str(uuid.UUID(int=900)), "name": "laptops"},
                {"id": str(uuid.UUID(int=901)), "name": "mice"},
            ]),
        ):
            q = await _catalog_check_question(ORG, p)
        assert q is None

    @pytest.mark.asyncio
    async def test_services_search_the_service_catalog(self):
        from app.agent import _catalog_check_question

        p = self._plan_with_entities(self._invoice_entities(nature="SERVICE"))
        with patch(
            "app.services.service_service.search",
            new=AsyncMock(return_value=[]),
        ) as svc_search:
            q = await _catalog_check_question(ORG, p)
        assert q is not None
        assert "service catalog" in q["question"]
        assert svc_search.await_count == 2

    @pytest.mark.asyncio
    async def test_yes_creates_and_links_products(self):
        from app.agent import _link_or_create_catalog_items

        entities = {**self._invoice_entities(),
                    "catalog_create_confirmed": True}
        params = {"items": [
            {"description": "laptops", "quantity": 2.0, "unit_price": 5000.0},
            {"description": "mice", "quantity": 3.0, "unit_price": 500.0},
        ]}
        with patch(
            "app.services.product_service.search",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.services.product_service.create",
            new=AsyncMock(
                side_effect=[
                    {"id": str(uuid.UUID(int=910)), "name": "laptops"},
                    {"id": str(uuid.UUID(int=911)), "name": "mice"},
                ]
            ),
        ) as create_mock:
            await _link_or_create_catalog_items(ORG, entities, params)
        assert create_mock.await_count == 2
        assert params["items"][0]["product_id"] == str(uuid.UUID(int=910))
        assert params["items"][1]["product_id"] == str(uuid.UUID(int=911))

    @pytest.mark.asyncio
    async def test_no_keeps_one_off_free_text_lines(self):
        from app.agent import _link_or_create_catalog_items

        entities = {**self._invoice_entities(), "catalog_skip": True}
        params = {"items": [
            {"description": "laptops", "quantity": 2.0, "unit_price": 5000.0},
        ]}
        with patch(
            "app.services.product_service.search",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.services.product_service.create",
            new=AsyncMock(),
        ) as create_mock:
            await _link_or_create_catalog_items(ORG, entities, params)
        create_mock.assert_not_called()
        assert "product_id" not in params["items"][0]

    @pytest.mark.asyncio
    async def test_answer_merge_routes_catalog_reply(self):
        p = plan(
            "create invoice of 20,000",
            clarification_history=[
                {"question":
                 "Catalog check: 'laptops' is not in your product catalog "
                 "yet. Add it to the catalog? Reply YES to add, or NO to "
                 "invoice as one-off free-text lines.",
                 "answer": "Yes - add to catalog"},
            ],
        )
        assert p.extracted_entities.get("catalog_create_confirmed") is True
