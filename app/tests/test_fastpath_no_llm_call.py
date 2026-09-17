"""Regression test — deterministic fast path must NOT make an LLM call.

Live defect (session 3662feeb, 2026-09-07): after the user approved the
"create customer + invoice" confirmation, the resumed deterministic run
logged DETERMINISTIC_EXECUTION and then crashed with

    UnboundLocalError: cannot access local variable 'client'
    where it is not associated with a value

because the second (executor) LLM call `client.generate_with_tools(...)`
ran UNCONDITIONALLY while `client` / `light_budget` are only bound on the
LLM planning path.  EVERY deterministic mutation (invoices, receipts,
payments, cash sales, transfers, expenses) crashed on approval.

The fix executes the pre-built tool calls directly when deterministic
calls exist and only otherwise enters the LLM executor call.  This test
pins that structure so the unconditional call cannot silently return.
"""

import ast
import inspect
import textwrap

import app.agent as agent


def _executor_source() -> str:
    return inspect.getsource(agent.execute)


def test_fast_path_executes_planned_calls_without_client():
    src = _executor_source()
    # The deterministic branch must run the planned calls directly.
    assert "execute_planned_tool_calls(" in src, (
        "The deterministic fast path must execute its planned tool calls "
        "directly (execute_planned_tool_calls) instead of calling the LLM."
    )


def test_client_call_is_guarded_by_the_fast_path_branch():
    src = _executor_source()
    guard = src.index("if deterministic_calls is not None:")
    direct_exec = src.index("execute_planned_tool_calls(", guard)
    else_branch = src.index("else:", direct_exec)
    llm_call = src.index("client.generate_with_tools(", else_branch)
    # The LLM call must live INSIDE the else branch — i.e. after it and
    # before the code that follows the if/else (the llm_text readback).
    after = src.index('llm_text = final_result.get("text"', llm_call)
    assert guard < direct_exec < else_branch < llm_call < after


def _execute_ast() -> ast.Module:
    """AST of `agent.execute`, independent of comments and formatting."""
    return ast.parse(textwrap.dedent(inspect.getsource(agent.execute)))


def test_no_unconditional_client_generate_call_after_planning():
    """The exact crash shape: the LLM executor call reachable while the
    deterministic fast path is taken, where `client` is unbound.

    Structural (AST) check.  The previous version searched for the literal
    text "else:" / "client = " within 400 characters BEFORE the call, so it
    reported a FALSE failure as soon as a *comment* in that window grew long
    enough to push the `client = get_client()` assignment out of range.
    Nesting is the real invariant, and it survives reformatting, reordering
    and comments.
    """
    tree = _execute_ast()

    # agent.execute has TWO fast-path guards testing the same variable: one
    # around the PLANNING call and one around the EXECUTION call.  Both are
    # valid, and because they test the identical expression their branches stay
    # consistent — if the fast path is taken, BOTH if-branches are taken and
    # neither `else:` (LLM) branch can run.
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "deterministic_calls is not None"
    ]
    assert guards, (
        "agent.execute must keep its `if deterministic_calls is not None:` "
        "fast-path guards."
    )

    # Nodes that are reachable ONLY through some guard's `else:` branch — i.e.
    # the LLM path, which never runs when deterministic_calls is not None.
    llm_only: set = set()
    for guard in guards:
        assert guard.orelse, (
            f"The fast-path guard at line {guard.lineno} must keep an `else:` "
            "(LLM) branch."
        )
        for stmt in guard.orelse:
            for node in ast.walk(stmt):
                llm_only.add(id(node))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and "generate_with_tools" in ast.unparse(node.func)
    ]
    assert calls, (
        "The LLM executor call must still exist on the non-deterministic path."
    )

    for call in calls:
        assert id(call) in llm_only, (
            f"client.generate_with_tools at line {call.lineno} is NOT inside the "
            "`else:` branch of a deterministic fast-path guard.  Running it while "
            "deterministic_calls is not None would crash with UnboundLocalError."
        )

    # `client` must be bound on that same LLM path, before every use of it.
    assignments = [
        node.lineno
        for node in ast.walk(tree)
        if id(node) in llm_only and isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "client"
    ]
    assert assignments, (
        "`client` must be assigned on the LLM path (inside a fast-path `else:` "
        "branch), otherwise the executor call has nothing bound to call."
    )
    for call in calls:
        assert any(line < call.lineno for line in assignments), (
            f"`client` is never assigned before the LLM call at line "
            f"{call.lineno} on the LLM path."
        )
