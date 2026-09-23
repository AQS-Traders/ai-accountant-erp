"""Operator utility - give every existing party its dedicated receivable/payable ledger.

Every customer gets a receivable account and every supplier a payable account as
a child of the corresponding control account, so the chart shows what each party
owes or is owed instead of merging all parties into one control balance.  New
parties are provisioned automatically on creation; this utility backfills the
parties that existed before that behaviour.

DRY RUN BY DEFAULT.  With no ``--apply`` flag it prints exactly which accounts
it WOULD create (party, code, control account) and writes nothing.  ``--apply``
creates the missing accounts and links them on the party rows.

What it does NOT do
-------------------
* no journal entries, no balance changes, no rewritten history: the chart gains
  a heading per party and the party row stores the link.  Postings that already
  happened keep the account they were posted to;
* nothing for another organisation unless you ask for it (``--org``);
* no duplicates: an existing child account with the party's name is reused, and
  a party that already carries an account id is skipped.

Usage
-----
Export the target project's credentials first (never echo or commit them)::

    set SUPABASE_URL=https://<project>.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=<service-role-key>

    venv\\Scripts\\python.exe scripts\\backfill_party_ledgers.py
    venv\\Scripts\\python.exe scripts\\backfill_party_ledgers.py --org <uuid> --json
    venv\\Scripts\\python.exe scripts\\backfill_party_ledgers.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import party_ledger_service as svc  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create the missing accounts and link them (default: dry run)",
    )
    parser.add_argument(
        "--org",
        default=None,
        help="restrict to one organisation id (default: every organisation)",
    )
    parser.add_argument(
        "--limit", type=int, default=1000, help="max parties per table (default 1000)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    organization_id = uuid.UUID(args.org) if args.org else None

    report = await svc.backfill_missing_accounts(
        organization_id=organization_id, apply=args.apply, limit=args.limit
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    mode = "APPLY" if args.apply else "DRY RUN"
    print("=" * 72)
    print("party ledger backfill - %s" % mode)
    print("=" * 72)
    print("scanned         : %d" % report["scanned"])
    print("already linked  : %d" % report["already_linked"])
    if args.apply:
        print("accounts created: %d" % len(report["created"]))
        print("accounts reused : %d" % len(report["reused"]))
        for entry in report["created"]:
            print("  + %-9s %-28s %-12s %s" % (
                entry["party_type"], entry["party"], entry["code"],
                entry["account_id"]))
        for entry in report["reused"]:
            print("  = %-9s %-28s %-12s %s" % (
                entry["party_type"], entry["party"], entry["code"],
                entry["account_id"]))
    else:
        print("would create    : %d" % len(report["planned"]))
        for entry in report["planned"]:
            print("  + %-9s %-28s %-12s under %s" % (
                entry["party_type"], entry["party"], entry["code"],
                entry["parent"]))
        print("")
        print("nothing was written - re-run with --apply to create these accounts")

    if report["errors"]:
        print("")
        print("errors: %d" % len(report["errors"]))
        for entry in report["errors"]:
            print("  ! %-9s %-28s %s" % (
                entry["party_type"], entry["party"], entry["error"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
