"""Recurring Transactions Service — unit tests for the deterministic helpers."""
from __future__ import annotations

from datetime import date

import pytest

from app.services.recurring_service import (
    _advance_due,
    _days_in_month,
    _shift_months,
    _validate_journal_template,
)


class TestDateMath:
    def test_weekly(self):
        assert _advance_due(date(2026, 9, 12), "weekly") == date(2026, 9, 19)

    def test_biweekly(self):
        assert _advance_due(date(2026, 9, 12), "biweekly") == date(2026, 9, 26)

    def test_monthly_plain(self):
        assert _advance_due(date(2026, 9, 12), "monthly") == date(2026, 10, 12)

    def test_monthly_year_wrap(self):
        assert _advance_due(date(2026, 12, 15), "monthly") == date(2027, 1, 15)

    def test_monthly_clamps_short_month(self):
        # Jan 31 → Feb has only 28 days in 2026.
        assert _advance_due(date(2026, 1, 31), "monthly") == date(2026, 2, 28)

    def test_quarterly(self):
        assert _advance_due(date(2026, 2, 28), "quarterly") == date(2026, 5, 28)

    def test_semi_annual(self):
        assert _advance_due(date(2026, 9, 10), "semi-annual") == date(2027, 3, 10)

    def test_yearly(self):
        # Feb 29 in a leap year → next year clamps to Feb 28.
        assert _advance_due(date(2028, 2, 29), "yearly") == date(2029, 2, 28)

    def test_days_in_month_leap(self):
        assert _days_in_month(2028, 2) == 29
        assert _days_in_month(2026, 2) == 28

    def test_shift_months(self):
        assert _shift_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


class TestTemplateValidation:
    def test_balanced_template_passes(self):
        _validate_journal_template(
            {
                "lines": [
                    {"account_code": "x", "debit": 100, "credit": 0},
                    {"account_code": "y", "debit": 0, "credit": 100},
                ]
            }
        )

    def test_unbalanced_raises(self):
        with pytest.raises(ValueError):
            _validate_journal_template(
                {
                    "lines": [
                        {"account_code": "x", "debit": 100, "credit": 0},
                        {"account_code": "y", "debit": 50, "credit": 0},
                    ]
                }
            )

    def test_single_line_raises(self):
        with pytest.raises(ValueError):
            _validate_journal_template({"lines": [{"account_code": "x", "debit": 100}]})

    def test_missing_account_raises(self):
        with pytest.raises(ValueError):
            _validate_journal_template(
                {
                    "lines": [
                        {"debit": 100},
                        {"credit": 100},
                    ]
                }
            )