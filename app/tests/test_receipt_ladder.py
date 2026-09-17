"""Work Stream R4 — the receipt/payment reasoning ladder and the
deterministic fast paths for every settlement/sale/transfer module.

R4.2: the receipt asks WHICH BUSINESS OPERATION the money is for.
R4.4: the settlement CHANNEL (cash vs bank) is asked, never defaulted —
it decides which ledger the money hits — and the party is material.
R4.5: once fully answered, receipt/payment/cash-sale/transfer execute
WITHOUT any LLM round-trip.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.planner import plan
from app.reasoning import (
    nature_question_for_intent,
    options_for_question,
)

OPERATION_Q = nature_question_for_intent("record_receipt")
CHANNEL_Q = (
    "Was this received in cash, or through the bank? Reply CASH or BANK."
)
DATE_Q = (
    "What is the transaction date? Reply TODAY, or the date as "
    "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
)


class TestReceiptLadder:
    """R4.2 + R4.4: operation -> channel -> party -> date, ONE round."""

    def test_bare_received_payment_is_recognized(self):
        p = plan("received payment of Rs.20,000")
        assert p.intent == "record_receipt"

    def test_operation_question_is_first(self):
        p = plan("received payment of Rs.20,000")
        assert p.missing_fields[0] == "transaction_nature"
        assert p.clarification_questions[0] == OPERATION_Q
        assert "business operation" in OPERATION_Q.lower()
        assert "invoice" in OPERATION_Q.lower()
        assert "advance" in OPERATION_Q.lower()
        assert "loan" in OPERATION_Q.lower()

    def test_channel_question_is_asked_when_unstated(self):
        p = plan("received payment of Rs.20,000")
        assert "payment_type" in p.missing_fields
        assert CHANNEL_Q in p.clarification_questions
        # R4.1: never defaulted — the channel answer decides the ledger.

    def test_party_is_a_next_stage_question(self):
        """R4.6: the customer is asked ONLY as the next stage AFTER the
        business operation resolves to an invoice/bill settlement — an
        advance or loan does not name a customer up front."""
        p = plan("received payment of Rs.20,000 today in cash")
        assert "customer_name" not in p.missing_fields

        # Stage 2 after "Invoice / bill settlement": the party is asked.
        p2 = plan(
            "received payment of Rs.20,000 today in cash",
            clarification_history=[{"question": OPERATION_Q, "answer": "a"}],
        )
        assert "customer_name" in p2.missing_fields

        # Stage 2 after "Advance": NO party question.
        p3 = plan(
            "received payment of Rs.20,000 today in cash",
            clarification_history=[{"question": OPERATION_Q, "answer": "b"}],
        )
        assert "customer_name" not in p3.missing_fields

    def test_channel_and_operation_options_ship_from_backend(self):
        op_opts = options_for_question(OPERATION_Q)
        assert [o["value"] for o in op_opts] == ["a", "b", "c"]
        assert [o["label"] for o in op_opts] == [
            "Invoice / bill settlement", "Advance", "Loan / drawing / other",
        ]
        ch_opts = options_for_question(CHANNEL_Q)
        assert [o["value"] for o in ch_opts] == ["a", "b"]
        assert [o["label"] for o in ch_opts] == ["Cash", "Bank / online"]

    def test_options_payload_stays_index_aligned(self):
        """R4.6 regression (screenshot bug): the backend must keep None
        placeholders for free-text questions — compacting the payload
        array shifted the chips onto the WRONG questions (the party
        question showed Cash/Bank chips, the channel question showed
        Today/Yesterday)."""
        assert options_for_question("Who is the customer?") is None
        assert options_for_question("Who is the supplier?") is None
        payload = [
            options_for_question(q)
            for q in [OPERATION_Q, "Who is the customer?", CHANNEL_Q, DATE_Q]
        ]
        assert payload[0] is not None      # operation chips
        assert payload[1] is None          # party: free text, NO chips
        assert payload[2] is not None      # channel chips
        assert payload[3] is not None      # date chips

    def test_channel_answer_bank_routes_the_bank_ledger_value(self):
        p = plan(
            "received payment of Rs.20,000",
            clarification_history=[
                {"question": OPERATION_Q, "answer": "a"},
                {"question": CHANNEL_Q, "answer": "b"},
            ],
        )
        assert p.extracted_entities["payment_method"] == "BANK_TRANSFER"
        assert p.extracted_entities["transaction_nature"] == "ALLOCATION"

    def test_channel_answer_cash_routes_the_cash_ledger_value(self):
        p = plan(
            "received payment of Rs.20,000",
            clarification_history=[
                {"question": OPERATION_Q, "answer": "a"},
                {"question": CHANNEL_Q, "answer": "a"},
            ],
        )
        assert p.extracted_entities["payment_method"] == "CASH"

    def test_operation_answer_advance_is_kept_for_the_journal(self):
        p = plan(
            "received payment of Rs.20,000 today in cash",
            clarification_history=[{"question": OPERATION_Q, "answer": "b"}],
        )
        # channel came from the "in cash" keyword; operation from the answer
        assert p.extracted_entities["payment_method"] == "CASH"
        assert p.extracted_entities["transaction_nature"] == "ADVANCE"


class TestPaymentLadder:
    def test_payment_asks_operation_and_channel_first(self):
        p = plan("paid the supplier Rs.12,000")
        assert p.intent == "record_payment"
        assert p.missing_fields[0] == "transaction_nature"
        assert "customer_name" not in p.missing_fields
        assert any(
            "paid in cash, or through the bank" in q.lower()
            for q in p.clarification_questions
        )

    def test_payment_supplier_is_staged_after_settlement_answer(self):
        # R4.6: the supplier is asked ONLY when the operation is a bill
        # settlement — never up front.
        p = plan("paid the supplier Rs.12,000 today in cash")
        assert "supplier_name" not in p.missing_fields
        p2 = plan(
            "paid the supplier Rs.12,000 today in cash",
            clarification_history=[{"question": OPERATION_Q, "answer": "a"}],
        )
        assert "supplier_name" in p2.missing_fields


class TestSettlementFastPaths:
    """R4.5: fully-answered settlement/sale/transfer plans build their
    tool calls WITHOUT any LLM round-trip; anything unresolved refuses."""

    CUSTOMER_ID = uuid.UUID(int=100)

    def _receipt_plan(self):
        return plan(
            "received payment of Rs.20,000",
            clarification_history=[
                {"question": OPERATION_Q, "answer": "a"},
                {"question": CHANNEL_Q, "answer": "b"},
                {"question": "Who is the customer?", "answer": "ABC Traders"},
                {"question": DATE_Q, "answer": "2026-09-01"},
            ],
        )

    @pytest.mark.asyncio
    async def test_receipt_fast_path_builds_call_without_llm(self):
        from app.agent import _deterministic_mutation_calls

        p = self._receipt_plan()
        assert p.intent == "record_receipt"
        assert p.requires_clarification is False
        with patch(
            "app.services.customer_service.search",
            new=AsyncMock(
                return_value=[{"id": str(self.CUSTOMER_ID),
                               "name": "ABC Traders"}]
            ),
        ):
            calls = await _deterministic_mutation_calls(
                organization_id=uuid.UUID(int=1),
                execution_plan=p,
                classification=None,
            )
        assert calls is not None and len(calls) == 1
        tc = calls[0]
        assert tc.tool_name == "record_customer_receipt"
        assert tc.arguments["customer_id"] == str(self.CUSTOMER_ID)
        assert tc.arguments["amount"] == 20000.0
        assert tc.arguments["payment_method"] == "BANK_TRANSFER"
        assert tc.arguments["transaction_nature"] == "ALLOCATION"
        assert tc.arguments["receipt_date"] == "2026-09-01"

    @pytest.mark.asyncio
    async def test_receipt_fast_path_refuses_unconfirmed_new_party(self):
        from app.agent import _deterministic_mutation_calls

        p = self._receipt_plan()
        with patch(
            "app.services.customer_service.search",
            new=AsyncMock(return_value=[]),
        ):
            calls = await _deterministic_mutation_calls(
                organization_id=uuid.UUID(int=1),
                execution_plan=p,
                classification=None,
            )
        # An unknown party is never silently invented.
        assert calls is None

    @pytest.mark.asyncio
    async def test_receipt_fast_path_refuses_unanswered_ladder(self):
        from app.agent import _deterministic_mutation_calls

        p = plan("received payment of Rs.20,000")
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None

    @pytest.mark.asyncio
    async def test_payment_fast_path_builds_call_without_llm(self):
        from app.agent import _deterministic_mutation_calls

        p = plan(
            "paid the supplier Rs.12,000",
            clarification_history=[
                {"question":
                 nature_question_for_intent("record_payment"),
                 "answer": "a"},
                {"question":
                 "Was this paid in cash, or through the bank? "
                 "Reply CASH or BANK.",
                 "answer": "a"},
                {"question": "Who is the supplier?", "answer": "Auto Zone"},
                {"question": DATE_Q, "answer": "2026-09-02"},
            ],
        )
        assert p.intent == "record_payment"
        with patch(
            "app.services.supplier_service.search",
            new=AsyncMock(
                return_value=[{"id": str(uuid.UUID(int=200)),
                               "name": "Auto Zone"}]
            ),
        ):
            calls = await _deterministic_mutation_calls(
                organization_id=uuid.UUID(int=1),
                execution_plan=p,
                classification=None,
            )
        assert calls is not None
        tc = calls[0]
        assert tc.tool_name == "record_supplier_payment"
        assert tc.arguments["payment_method"] == "CASH"
        assert tc.arguments["transaction_nature"] == "ALLOCATION"

    def test_cash_sale_fast_path_builds_call(self):
        from app.agent import _cash_sale_fast_path_call

        p = plan(
            "sold goods for Rs.8,000 in cash today",
            clarification_history=[
                {"question":
                 nature_question_for_intent("record_cash_sale"),
                 "answer": "a"},
            ],
        )
        calls = _cash_sale_fast_path_call(p.extracted_entities)
        assert calls is not None
        tc = calls[0]
        assert tc.tool_name == "record_cash_sale"
        assert tc.arguments["amount"] == 8000.0

    def test_cash_sale_fast_path_refuses_other_income(self):
        from app.agent import _cash_sale_fast_path_call

        entities = {
            "amount": 8000.0,
            "transaction_date": "2026-09-01",
            "transaction_nature": "OTHER_INCOME",
        }
        # Other income needs a revenue-account decision — model path.
        assert _cash_sale_fast_path_call(entities) is None

    def test_transfer_fast_path_builds_call_with_names(self):
        from app.agent import _transfer_fast_path_call

        p = plan("transfer Rs.50,000 from HBL to Meezan today")
        assert p.extracted_entities.get("source_bank_name") == "hbl"
        assert p.extracted_entities.get("destination_bank_name") == "meezan"
        calls = _transfer_fast_path_call(p.extracted_entities)
        assert calls is not None
        tc = calls[0]
        assert tc.tool_name == "record_bank_transfer"
        assert tc.arguments["source_bank_account_id"] == "hbl"
        assert tc.arguments["destination_bank_account_id"] == "meezan"

    def test_transfer_fast_path_refuses_unresolved_banks(self):
        from app.agent import _transfer_fast_path_call

        entities = {"amount": 50000.0, "transaction_date": "2026-09-01"}
        assert _transfer_fast_path_call(entities) is None


class TestInvoiceLadder:
    """R4.6 (screenshot bug): the invoice round asks nature -> customer
    -> date; the customer question is FREE TEXT (no chips) and the sale
    nature chips carry clean labels."""

    def test_invoice_asks_customer_before_date(self):
        p = plan("create invoice of 20,000")
        assert p.intent == "create_invoice"
        # R4.9: the line detail (item + quantity) is asked in the SAME
        # consolidated round — dependency-first: nature -> customer ->
        # item -> quantity -> amount -> date.  The manual invoice form
        # mandates a line item, so the AI path must never silently
        # default "Goods" x1.
        assert p.missing_fields == [
            "transaction_nature", "customer_name", "item_description",
            "quantity", "transaction_date",
        ]
        assert any(
            "customer" in q.lower() for q in p.clarification_questions
        )
        assert any(
            "item or service" in q.lower() for q in p.clarification_questions
        )
        assert any(
            "units are you invoicing" in q.lower()
            for q in p.clarification_questions
        )

    def test_sale_nature_chips_are_clean(self):
        opts = options_for_question(
            nature_question_for_intent("create_invoice")
        )
        assert [o["label"] for o in opts] == [
            "Goods", "Service", "Fixed-asset disposal", "Other income",
        ]
        assert [o["value"] for o in opts] == ["a", "b", "c", "d"]

    def test_invoice_payload_is_index_aligned_with_free_text_party(self):
        p = plan("create invoice of 20,000")
        payload = [
            options_for_question(q) for q in p.clarification_questions
        ]
        # nature: chips; customer/item/quantity: NONE (free text — never
        # date chips); date: Today/Yesterday chips.
        assert payload[0] is not None
        assert payload[1] is None
        assert payload[2] is None  # item description — free text
        assert payload[3] is None  # quantity — free text (bare number)
        assert [o["value"] for o in payload[4]] == ["TODAY", "YESTERDAY"]

    def test_agent_response_accepts_none_placeholders(self):
        """The wire contract allows None/[] placeholders inside
        question_options — a strict list-of-lists schema would 500 on
        every mixed (chips + free-text) questionnaire."""
        from app.models.schemas import AgentResponse, ExecutionStatus

        resp = AgentResponse(
            status=ExecutionStatus.AWAITING_CLARIFICATION,
            execution_id=uuid.uuid4(),
            question="q",
            question_options=[
                [{"value": "a", "label": "Goods"}],
                None,
                [],
            ],
            requires_user_input=True,
        )
        assert resp.question_options[1] is None
        assert resp.question_options[2] == []


class TestInvoiceFastPath:
    """R4.7: a fully-answered invoice builds the create_invoice tool
    call WITHOUT the 30-85s LLM planning round-trip (the answered
    clarification stage used to take 105-124s)."""

    def _resolved_invoice_plan(self):
        p = plan("create invoice of 20,000")
        return plan(
            "create invoice of 20,000",
            clarification_history=[
                {"question":
                 nature_question_for_intent("create_invoice"),
                 "answer": "a"},
                {"question": "Who is the customer?", "answer": "ABC Traders"},
                {"question":
                 "What item or service is being invoiced? (description "
                 "for the invoice line — e.g. 'Web development services', "
                 "'HP laptops')",
                 "answer": "Website development"},
                {"question":
                 "How many units are you invoicing? (quantity — reply "
                 "with just the number; the line total you gave is "
                 "divided across the units)",
                 "answer": "2"},
                {"question": DATE_Q, "answer": "2026-09-01"},
            ],
        )

    @pytest.mark.asyncio
    async def test_invoice_fast_path_builds_call_without_llm(self):
        from app.agent import _deterministic_mutation_calls

        p = self._resolved_invoice_plan()
        assert p.intent == "create_invoice"
        assert p.requires_clarification is False
        with patch(
            "app.services.customer_service.search",
            new=AsyncMock(
                return_value=[{"id": str(uuid.UUID(int=300)),
                               "name": "ABC Traders"}]
            ),
        ), patch(
            # R4.10: the fast path also resolves line items against the
            # product/service catalog — keep the test offline.
            "app.services.product_service.search",
            new=AsyncMock(return_value=[]),
        ):
            calls = await _deterministic_mutation_calls(
                organization_id=uuid.UUID(int=1),
                execution_plan=p,
                classification=None,
            )
        assert calls is not None and len(calls) == 1
        tc = calls[0]
        assert tc.tool_name == "create_invoice"
        assert tc.arguments["customer_id"] == str(uuid.UUID(int=300))
        assert tc.arguments["invoice_date"] == "2026-09-01"
        # R4.9: the answered quantity splits the known total into a
        # derived unit price (2 units of the 20,000 invoice = 10,000
        # each) — the header always reconciles with what the user said.
        items = tc.arguments["items"]
        assert len(items) == 1
        assert items[0]["description"] == "Website development"
        assert items[0]["quantity"] == 2.0
        assert items[0]["unit_price"] == 10000.0

    @pytest.mark.asyncio
    async def test_invoice_fast_path_refuses_other_income(self):
        from app.agent import _deterministic_mutation_calls

        p = plan(
            "create invoice of 20,000",
            clarification_history=[
                {"question":
                 nature_question_for_intent("create_invoice"),
                 "answer": "d"},
                {"question": "Who is the customer?", "answer": "ABC Traders"},
                {"question": DATE_Q, "answer": "2026-09-01"},
            ],
        )
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        # Other income needs a revenue-account decision — model path.
        assert calls is None

    @pytest.mark.asyncio
    async def test_invoice_fast_path_refuses_unconfirmed_new_customer(self):
        from app.agent import _deterministic_mutation_calls

        p = self._resolved_invoice_plan()
        with patch(
            "app.services.customer_service.search",
            new=AsyncMock(return_value=[]),
        ):
            calls = await _deterministic_mutation_calls(
                organization_id=uuid.UUID(int=1),
                execution_plan=p,
                classification=None,
            )
        # An unknown customer is never silently invented.
        assert calls is None

    @pytest.mark.asyncio
    async def test_invoice_fast_path_refuses_unanswered_ladder(self):
        from app.agent import _deterministic_mutation_calls

        p = plan("create invoice of 20,000")
        calls = await _deterministic_mutation_calls(
            organization_id=uuid.UUID(int=1),
            execution_plan=p,
            classification=None,
        )
        assert calls is None