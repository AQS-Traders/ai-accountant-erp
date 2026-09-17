"""Work Stream A - deterministic transaction-date parser tests."""

from datetime import date

import pytest

from app.date_parser import parse_transaction_date, resolve_date_range

REF = date(2026, 9, 4)  # a Friday


class TestParseTransactionDate:
    def test_iso_format(self):
        r = parse_transaction_date("2026-09-04", today=REF)
        assert r.ok and r.iso_date == "2026-09-04"
        assert r.matched_format == "YYYY-MM-DD"

    def test_slash_day_first(self):
        r = parse_transaction_date("04/09/2026", today=REF)
        assert r.ok and r.iso_date == "2026-09-04"
        assert r.matched_format == "DD/MM/YYYY"

    def test_slash_day_first_disambiguates(self):
        # 03/04/2026 must be 3 April (day-first), never March 4.
        r = parse_transaction_date("03/04/2026", today=REF)
        assert r.ok and r.iso_date == "2026-04-03"

    def test_dash_day_first(self):
        r = parse_transaction_date("04-09-2026", today=REF)
        assert r.ok and r.iso_date == "2026-09-04"

    def test_mm_dd_rejected(self):
        # 09/13/2026 would be 13 September in MM/DD reading, but month 13
        # is invalid in day-first; the parser must reject, never reinterpret.
        r = parse_transaction_date("09/13/2026", today=REF)
        assert not r.ok
        assert r.iso_date is None
        assert "DD/MM/YYYY" in r.error

    def test_two_digit_year_rejected(self):
        assert parse_transaction_date("04/09/26", today=REF).ok is False

    def test_keyword_today(self):
        r = parse_transaction_date("TODAY", today=REF)
        assert r.ok and r.iso_date == "2026-09-04"

    def test_keyword_yesterday(self):
        r = parse_transaction_date("yesterday", today=REF)
        assert r.ok and r.iso_date == "2026-09-03"

    def test_keyword_tomorrow(self):
        r = parse_transaction_date("Tomorrow", today=REF)
        assert r.ok and r.iso_date == "2026-09-05"

    def test_invalid_month(self):
        r = parse_transaction_date("2026-13-01", today=REF)
        assert not r.ok and r.iso_date is None and r.error

    def test_invalid_calendar_day(self):
        # 31 February and 30 February are never real dates.
        assert parse_transaction_date("31/02/2026", today=REF).ok is False
        assert parse_transaction_date("2026-02-30", today=REF).ok is False

    def test_leap_year_accepted(self):
        assert parse_transaction_date("29/02/2024", today=REF).ok is True
        assert (
            parse_transaction_date("29/02/2024", today=REF).iso_date
            == "2024-02-29"
        )
        # 2026 is not a leap year.
        assert parse_transaction_date("29/02/2026", today=REF).ok is False

    def test_prose_rejected_with_helpful_error(self):
        r = parse_transaction_date("3rd of March", today=REF)
        assert not r.ok
        assert "YYYY-MM-DD" in r.error and "DD/MM/YYYY" in r.error

    def test_empty_rejected(self):
        assert parse_transaction_date("   ", today=REF).ok is False

    def test_case_and_whitespace_insensitive(self):
        r = parse_transaction_date("  2026-09-04  ", today=REF)
        assert r.ok and r.iso_date == "2026-09-04"


class TestResolveDateRange:
    def test_this_month(self):
        assert resolve_date_range("show P&L for this month", today=REF) == (
            "2026-09-01",
            "2026-09-30",
        )

    def test_last_month(self):
        assert resolve_date_range("last month", today=REF) == (
            "2026-08-01",
            "2026-08-31",
        )

    def test_last_month_year_boundary(self):
        # January -> last month is December of the previous year.
        assert resolve_date_range("last month", today=date(2026, 1, 15)) == (
            "2025-12-01",
            "2025-12-31",
        )

    def test_this_week(self):
        # REF is Friday 2026-09-04; week runs Monday 2026-08-31..Sunday.
        assert resolve_date_range("this week", today=REF) == (
            "2026-08-31",
            "2026-09-06",
        )

    def test_this_quarter(self):
        assert resolve_date_range("this quarter", today=REF) == (
            "2026-07-01",
            "2026-09-30",
        )

    def test_last_quarter(self):
        assert resolve_date_range("last quarter", today=REF) == (
            "2026-04-01",
            "2026-06-30",
        )

    def test_last_quarter_year_boundary(self):
        # Q1 (Jan) -> last quarter is Q4 of the previous year.
        assert resolve_date_range("last quarter", today=date(2026, 2, 1)) == (
            "2025-10-01",
            "2025-12-31",
        )

    def test_unrecognised_returns_none(self):
        assert resolve_date_range("in 2025", today=REF) is None
