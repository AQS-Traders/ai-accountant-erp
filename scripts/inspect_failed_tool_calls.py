"""Operator recipe — read the recorded cause of a FAILED AI tool call.

READ-ONLY: SELECT statements only.  No insert/update/delete, no RPC, no
migration — running it cannot change a single book entry.

Why this exists
---------------
The 2026-09-20 ``create_invoice`` failure (session
6a48a432-d9d9-480c-8e47-e6799305bc6f) could NOT be root-caused from the
database:

* ``ai.tool_calls.error_details`` was never written — the writer had no
  parameter for it and the caller dropped ``ToolResult.error``, so all 26
  FAILED tool-call rows in production carry NULL;
* the financial claim was left ``IN_PROGRESS`` with ``error = NULL``
  (public.financial_operations row 59777966-…), so the one durable place the
  raw exception could have survived was empty as well;
* the real text existed only in server-side structlog output, which the
  current Vercel plan does not expose (runtime-logs API: HTTP 404).

Both writers are fixed (see app/tests/test_tool_failure_observability.py), so
the NEXT occurrence is readable with this script instead of by hand.

Usage
-----
Export the target project's credentials first (never echo or commit them)::

    set SUPABASE_URL=https://<project>.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=<service-role-key>

    venv\\Scripts\\python.exe scripts\\inspect_failed_tool_calls.py
    venv\\Scripts\\python.exe scripts\\inspect_failed_tool_calls.py --session <uuid>
    venv\\Scripts\\python.exe scripts\\inspect_failed_tool_calls.py --failed-only

Output
------
1. the session (newest, or --session): request, status, phase;
2. every tool call in call order — tool, status and, for a failure, the
   recorded reason (``error_details``) plus the arguments that were used;
3. every non-terminal financial claim with its raw error, so a blocked retry
   or a lost cause is visible;
4. the persisted execution result: status, verification_status, summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _short(value: Any, limit: int = 400) -> str:
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, default=str)[:limit]


async def _tool_names() -> Dict[str, str]:
    from app.database import fetch_many

    rows = await fetch_many("ai_tools", filters={}, select="id,name", limit=200)
    return {str(r["id"]): str(r.get("name") or "") for r in rows}


async def _print_session(session_id: Optional[str]) -> Optional[str]:
    from app.database import fetch_many

    if session_id:
        rows = await fetch_many(
            "ai_execution_sessions", filters={"id": session_id}, limit=1
        )
    else:
        rows = await fetch_many(
            "ai_execution_sessions", filters={}, order="created_at.desc", limit=1
        )
    if not rows:
        print("no session found")
        return None
    s = rows[0]
    print("--- SESSION ---")
    print(f"id           : {s.get('id')}")
    print(f"request      : {_short(s.get('user_request'), 200)}")
    print(f"status/phase : {s.get('status')} / {s.get('current_phase')}")
    print(f"created_at   : {s.get('created_at')}")
    return str(s.get("id"))


async def _print_tool_calls(session_id: str) -> None:
    from app.database import fetch_many

    names = await _tool_names()
    calls = await fetch_many(
        "ai_tool_calls",
        filters={"execution_session_id": session_id},
        order="call_order.asc",
        limit=100,
    )
    print("\n--- TOOL CALLS (call order) ---")
    if not calls:
        print("none")
    for c in calls:
        tool = names.get(str(c.get("tool_id")), "?")
        print(f"[{c.get('call_order')}] {tool}  status={c.get('status')}")
        print(f"    arguments : {_short(c.get('input_payload'))}")
        print(f"    output    : {_short(c.get('output_payload'), 200)}")
        reason = c.get("error_details")
        print(f"    failure   : {reason if reason else '(none recorded)'}")


async def _print_failed_calls(limit: int = 40) -> None:
    from app.database import fetch_many

    names = await _tool_names()
    fails = await fetch_many(
        "ai_tool_calls",
        filters={"status": "FAILED"},
        order="created_at.desc",
        limit=limit,
    )
    print(f"\n--- FAILED TOOL CALLS (newest {limit}) ---")
    if not fails:
        print("none")
    for c in fails:
        tool = names.get(str(c.get("tool_id")), "?")
        print(f"{c.get('created_at')}  {tool}  session={c.get('execution_session_id')}")
        print(f"    failure : {c.get('error_details') or '(NULL - pre-fix row)'}")


async def _print_open_claims(limit: int = 40) -> None:
    from app.database import fetch_many

    claims = await fetch_many(
        "financial_operations",
        filters={"status": "IN_PROGRESS"},
        order="claimed_at.desc",
        limit=limit,
    )
    print(f"\n--- NON-TERMINAL FINANCIAL CLAIMS (newest {limit}) ---")
    if not claims:
        print("none (every claim is COMPLETED or FAILED)")
    for c in claims:
        print(f"{c.get('claimed_at')}  {c.get('operation')}  key={c.get('idempotency_key')}")
        print(f"    error : {c.get('error') or '(NULL)'}")


async def _print_result(session_id: str) -> None:
    from app.database import fetch_many

    rows = await fetch_many(
        "ai_execution_results",
        filters={"execution_session_id": session_id},
        order="created_at.desc",
        limit=5,
    )
    print("\n--- EXECUTION RESULT ---")
    if not rows:
        print("none")
    for r in rows:
        print(f"status={r.get('status')} verification={r.get('verification_status')}")
        print(f"summary: {_short(r.get('summary'), 400)}")


async def _main(args: argparse.Namespace) -> None:
    if args.failed_only:
        await _print_failed_calls()
        await _print_open_claims()
        return
    session_id = await _print_session(args.session)
    if session_id:
        await _print_tool_calls(session_id)
    await _print_open_claims()
    if session_id:
        await _print_result(session_id)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the cause of FAILED AI tool calls.")
    parser.add_argument("--session", help="execution session uuid (default: newest)")
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="list FAILED tool calls across sessions instead of one session",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))

