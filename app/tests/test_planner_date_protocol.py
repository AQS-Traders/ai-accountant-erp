"""Work Stream A - mandatory transaction-date protocol (planner) tests."""

from datetime import date, timedelta

from app.planner import plan
from app.reasoning import nature_question_for_intent

DATE_Q = (
    "What is the transaction date? Reply TODAY, or the date as "
    "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
)
# Work Stream R: the purchase nature/purpose question (asset vs inventory
# vs consumable vs service) joins the FIRST consolidated round.
NATURE_Q = nature_question_for_intent("record_cash_purchase")


class TestMissingDateQuestion:
    def test_mutation_without_date_asks_once(self):
        p = plan("bought a chair for Rs.5,000 in cash")
        assert p.requires_clarification is True
        assert "transaction_date" in p.missing_fields
        # The date question joins the consolidated questionnaire - it is
        # never a standalone round.  Work Stream R: the nature/purpose
        # question precedes it in the SAME round (dependency-first).
        assert p.clarification_questions == [NATURE_Q, DATE_Q]

    def test_question_names_both_formats(self):
        p = plan("record expense: fuel Rs.3,000")
        q = next(q for q in p.clarification_questions if "date" in q.lower())
        assert "YYYY-MM-DD" in q and "DD/MM/YYYY" in q
        assert "2026-09-04" in q and "04/09/2026" in q

    def test_report_intent_never_asks_date(self):
        p = plan("show me the trial balance")
        assert "transaction_date" not in p.missing_fields
        assert p.requires_clarification is False


class TestDateResolution:
    def test_history_answer_day_first(self):
        p = plan(
            "bought a chair for Rs.5,000 in cash",
            clarification_history=[
                # Work Stream R: the nature answer resolves the decision tree.
                {"question": NATURE_Q, "answer": "b"},
                {"question": DATE_Q, "answer": "04/09/2026"},
            ],
        )
        assert p.requires_clarification is False
        assert "transaction_date" not in p.missing_fields
        assert p.extracted_entities["transaction_date"] == "2026-09-04"

    def test_history_answer_today(self):
        p = plan(
            "bought a chair for Rs.5,000 in cash",
            clarification_history=[
                {"question": NATURE_Q, "answer": "b"},
                {"question": DATE_Q, "answer": "today"},
            ],
        )
        assert p.requires_clarification is False
        assert p.extracted_entities["transaction_date"] == date.today().isoformat()

    def test_history_answer_yesterday(self):
        p = plan(
            "bought a chair for Rs.5,000 in cash",
            clarification_history=[
                {"question": NATURE_Q, "answer": "b"},
                {"question": DATE_Q, "answer": "yesterday"},
            ],
        )
        assert p.extracted_entities["transaction_date"] == (
            (date.today() - timedelta(days=1)).isoformat()
        )

    def test_silent_keyword_in_message(self):
        # "yesterday" is parsed silently - no date question.  Work Stream
        # R: the nature/purpose decision is the ONE remaining gap.
        p = plan("bought a chair for Rs.5,000 in cash yesterday")
        assert "transaction_date" not in p.missing_fields
        assert p.missing_fields == ["transaction_nature"]
        assert p.clarification_questions == [NATURE_Q]

    def test_iso_date_in_message(self):
        p = plan("bought a chair for Rs.5,000 in cash on 2026-08-15")
        assert p.extracted_entities["transaction_date"] == "2026-08-15"
        assert "transaction_date" not in p.missing_fields

    def test_invalid_calendar_date_is_reasked(self):
        # 31 February is not a real date: dropped, never guessed.
        p = plan("bought a chair for Rs.5,000 in cash on 31/02/2026")
        assert "transaction_date" in p.missing_fields
        assert p.clarification_questions == [NATURE_Q, DATE_Q]

    def test_invalid_answer_is_reasked(self):
        p = plan(
            "bought a chair for Rs.5,000 in cash",
            clarification_history=[{"question": DATE_Q, "answer": "3rd of March"}],
        )
        assert "transaction_date" in p.missing_fields
        assert p.requires_clarification is True


class TestBatchDateQuestions:
    def test_per_item_date_questions_merged(self):
        p = plan(
            "Record these 2 expenses:\n1. Office supplies Rs.5,000\n"
            "2. Fuel Rs.3,000"
        )
        assert p.requires_clarification is True
        assert any(
            q.startswith("For document 1:") and "transaction date" in q.lower()
            for q in p.clarification_questions
        )
        assert any(
            q.startswith("For document 2:") and "transaction date" in q.lower()
            for q in p.clarification_questions
        )

    def test_batch_one_round_total(self):
        # The date questions live in the SAME consolidated questionnaire.
        p = plan(
            "Record these 2 expenses:\n1. Office supplies Rs.5,000\n"
            "2. Fuel Rs.3,000"
        )
        date_qs = [q for q in p.clarification_questions if "date" in q.lower()]
        assert len(date_qs) == 2
