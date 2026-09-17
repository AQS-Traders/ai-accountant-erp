"""Work Stream K - evaluation harness pytest integration.

The golden set (tests/golden/*.json) is replayed through the planner on
every suite run: a changed outcome is a failing test, so planner/prompt
changes can never silently drift behavior.  The full harness (pass-rate
reporting, exit codes) is also runnable standalone:

    venv\\Scripts\\python scripts\\eval_harness.py
"""

import io

import pytest

from app.eval_harness import evaluate_case, load_cases, run


def test_golden_set_exists():
    cases = load_cases()
    assert len(cases) >= 15, "golden set missing - run scripts/eval_harness.py --seed"


def test_golden_set_pass_rate_is_100():
    buffer = io.StringIO()
    rate = run(out=buffer)
    # Surface any failing case names in the assertion message.
    assert rate == 1.0, buffer.getvalue()


def test_evaluate_case_reports_failures():
    bad_case = {
        "name": "synthetic",
        "message": "show me the trial balance",
        "expected": {"intent": "generate_balance_sheet"},
    }
    failures = evaluate_case(bad_case)
    assert failures and "intent" in failures[0]
