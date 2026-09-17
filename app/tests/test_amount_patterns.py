"""
R4.9 - labelled amount cue extraction.

"CREATE INVOICE FOR ABC COMPUTERS, AMOUNT 20,000" was asking for the
amount again: the strong-money-context pattern only accepted
"amounting to <num>", not the plain "amount <num>" label.  The agent
must never re-ask information the user already provided.
"""

from __future__ import annotations

from app.planner import _extract_amount, plan


class TestLabelledAmountCue:
    def test_uppercase_labelled_amount(self):
        assert _extract_amount(
            "CREATE INVOICE FOR ABC COMPUTERS, AMOUNT 20,000"
        ) == 20000.0

    def test_amount_with_colon(self):
        assert _extract_amount("invoice amount: 15000") == 15000.0

    def test_amount_with_dash(self):
        assert _extract_amount("amount - 8000 for rent") == 8000.0

    def test_amount_with_currency(self):
        assert _extract_amount("amount Rs 45,000") == 45000.0

    def test_small_count_is_not_money(self):
        # 1-2 digit figures after "amount" are counts, never money.
        assert _extract_amount("amount 2 items were damaged") is None

    def test_existing_patterns_still_work(self):
        assert _extract_amount("paid 5000 to the vendor") == 5000.0
        assert _extract_amount("record an expense of 25000") == 25000.0
        assert _extract_amount("Rs. 20,000 for stationery") == 20000.0


class TestAmountNeverReAsked:
    def test_invoice_plan_with_labelled_amount(self):
        """End-to-end: the answered amount must appear in the plan entities
        and the amount question must NOT be part of the missing fields."""
        p = plan("CREATE INVOICE FOR ABC COMPUTERS, AMOUNT 20,000")
        assert p.extracted_entities.get("amount") == 20000.0
        assert p.extracted_entities.get("customer_name") == "ABC COMPUTERS"
        assert "amount" not in (p.missing_fields or [])