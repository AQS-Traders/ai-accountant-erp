"""
Recurring Transactions Service — trusted business logic for recurring templates.

Lifecycle:  create template → each due date generates a journal entry
            (draft or auto-posted per template.auto_post).

Accounting authority stays with the Accounting Engine:  every generation
calls ``accounting_service.prepare_journal`` (+ validate/post when
auto_post).  The LLM never manufactures lines.

Guard-rails:
* frequencies are a fixed enum and validated up-front;
* the journal template must balance (total debit == total credit) at
  CREATE time — a broken template can never be created;
* each account in a line is resolved by exact code, else name, else a
  precise configuration error (never silently guessed);
* generation is idempotent per template+next_due_date (a successful run
  advances next_due_date and is skipped if the template changed).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import structlog

from app.repositories import account_repository as account_repo
from app.repositories import recurring_repository as repo
from app.services import accounting_service

log = structlog.get_logger(__name__)

_FREQUENCIES = (
    "weekly",
    "biweekly",
    "monthly",
    "quarterly",
    "semi-annual",
    "yearly",
)

_VALID_STATUSES = ("active", "paused", "completed", "cancelled")


def _advance_due(current: date, frequency: str) -> date:
    """Roll current forward by one frequency step (calendar correct)."""
    if frequency == "weekly":
        return current + timedelta(days=7)
    if frequency == "biweekly":
        return current + timedelta(days=14)
    if frequency == "monthly":
        return _shift_months(current, 1)
    if frequency == "quarterly":
        return _shift_months(current, 3)
    if frequency == "semi-annual":
        return _shift_months(current, 6)
    if frequency == "yearly":
        return _shift_months(current, 12)
    raise ValueError(f"Unsupported frequency: {frequency}")


def _shift_months(current: date, months: int) -> date:
    """Clamp day to the target month's day count (e.g. Jan 31 → Feb 28)."""
    total = current.year * 12 + (current.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(current.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


def _validate_journal_template(journal_template: Dict[str, Any]) -> None:
    """Balance + account-required checks at creation time."""
    lines = journal_template.get("lines") or []
    if len(lines) < 2:
        raise ValueError(
            "A recurring journal template needs at least 2 lines (debit + credit)."
        )
    total_debit = sum(float(l.get("debit", 0) or 0) for l in lines)
    total_credit = sum(float(l.get("credit", 0) or 0) for l in lines)
    if abs(total_debit - total_credit) > 0.01:
        raise ValueError(
            "Recurring journal template is not balanced "
            f"(debit={total_debit}, credit={total_credit})."
        )
    if total_debit <= 0:
        raise ValueError("Recurring journal template amounts must be positive.")
    for idx, line in enumerate(lines):
        if not line.get("account_code") and not line.get("account_name"):
            raise ValueError(f"Line {idx + 1} is missing account_code/account_name")


async def create_template(
    *,
    organization_id: uuid.UUID,
    name: str,
    frequency: str,
    next_due_date: str,
    journal_template: Dict[str, Any],
    description: Optional[str] = None,
    auto_post: bool = False,
    reminder_days: int = 3,
    source_type: str = "recurring",
    created_by: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    if frequency not in _FREQUENCIES:
        raise ValueError(f"Invalid frequency '{frequency}'. Must be one of {_FREQUENCIES}")
    if reminder_days < 0:
        raise ValueError("reminder_days cannot be negative")
    _validate_journal_template(journal_template)

    parsed = date.fromisoformat(next_due_date)
    template = await repo.create_template(
        organization_id=organization_id,
        name=name,
        description=description,
        frequency=frequency,
        schedule_day=None,
        next_due_date=parsed.isoformat(),
        auto_post=auto_post,
        reminder_days=reminder_days,
        journal_template=journal_template,
        source_type=source_type,
        created_by=created_by,
    )
    log.info(
        "recurring.template_created",
        template_id=template["id"],
        frequency=frequency,
        next_due_date=parsed.isoformat(),
        auto_post=auto_post,
    )
    return template


async def get_template(
    organization_id: uuid.UUID, *, template_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await repo.get_template(organization_id, template_id=template_id)


async def list_templates(
    organization_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if status and status not in _VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {_VALID_STATUSES}")
    return await repo.list_templates(organization_id, status=status, limit=limit)


async def set_status(
    *,
    organization_id: uuid.UUID,
    template_id: uuid.UUID,
    status: str,
) -> Dict[str, Any]:
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {_VALID_STATUSES}")
    template = await repo.get_template(organization_id, template_id=template_id)
    if not template:
        raise ValueError("Recurring template not found in this organization.")
    updated = await repo.update_template(
        template_id=template_id,
        data={"status": status},
    )
    log.info("recurring.template_status", template_id=str(template_id), status=status)
    return updated or template


async def generate_due(
    organization_id: uuid.UUID,
    *,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate journal entries for every due template.

    Returns a per-template report:
      {template_id, name, due_date, status: generated|failed,
       mode: draft|posted, journal_entry_id, error}
    """
    report: Dict[str, Any] = {"generated": [], "failed": []}
    due_date = as_of or date.today().isoformat()
    due_templates = await repo.get_due_templates(organization_id, as_of=due_date)

    for template in due_templates:
        tid = uuid.UUID(str(template["id"]))
        name = template.get("name") or "untitled"
        try:
            entry = await _execute_template(
                organization_id,
                template_id=tid,
                template=template,
                due_date=str(template["next_due_date"]),
            )
            await repo.create_execution(
                template_id=tid,
                organization_id=organization_id,
                scheduled_date=str(template["next_due_date"]),
                status="generated",
                journal_entry_id=uuid.UUID(str(entry["id"])),
            )
            next_due = _advance_due(
                date.fromisoformat(str(template["next_due_date"])),
                template.get("frequency") or "monthly",
            )
            await repo.update_template(
                template_id=tid,
                data={
                    "next_due_date": next_due.isoformat(),
                    "last_generated_date": date.today().isoformat(),
                    "total_generated": (template.get("total_generated") or 0) + 1,
                },
            )
            report["generated"].append(
                {
                    "template_id": template["id"],
                    "name": name,
                    "due_date": template["next_due_date"],
                    "mode": "posted" if template.get("auto_post") else "draft",
                    "journal_entry_id": entry["id"],
                }
            )
            log.info(
                "recurring.generated",
                template_id=str(tid), name=name,
                auto_post=bool(template.get("auto_post")), entry_id=entry["id"],
            )
        except Exception as exc:  # noqa: BLE001 — isolated per template
            log.error(
                "recurring.failed", template_id=str(tid), name=name, error=str(exc)
            )
            await repo.create_execution(
                template_id=tid,
                organization_id=organization_id,
                scheduled_date=str(template.get("next_due_date") or due_date),
                status="failed",
                error_message=str(exc)[:500],
            )
            report["failed"].append(
                {
                    "template_id": template["id"],
                    "name": name,
                    "due_date": template["next_due_date"],
                    "error": str(exc),
                }
            )
    return report


async def _execute_template(
    organization_id: uuid.UUID,
    *,
    template_id: uuid.UUID,
    template: Dict[str, Any],
    due_date: str,
) -> Dict[str, Any]:
    """Resolve accounts and post (prepare → validate → post) the journal."""
    journal_template = template.get("journal_template") or {}
    lines = []
    for item in journal_template.get("lines") or []:
        account = None
        if item.get("account_code"):
            account = await account_repo.get_account_by_code(
                organization_id, code=str(item["account_code"]).strip()
            )
        if account is None and item.get("account_name"):
            matches = await account_repo.search_accounts(
                organization_id,
                query=str(item["account_name"]).strip(),
                limit=5,
            )
            account = next(
                (
                    m
                    for m in matches
                    if m.get("name", "").lower().strip()
                    == str(item["account_name"]).lower().strip()
                ),
                matches[0] if matches else None,
            )
        if account is None:
            raise ValueError(
                f"Recurring template account "
                f"'{item.get('account_code') or item.get('account_name')}' "
                "does not exist in this organization's chart of accounts."
            )
        lines.append(
            {
                "account_id": str(account["id"]),
                "description": item.get("description")
                or journal_template.get("description")
                or template.get("name")
                or "Recurring entry",
                "debit": float(item.get("debit", 0) or 0),
                "credit": float(item.get("credit", 0) or 0),
            }
        )

    description = (
        journal_template.get("description")
        or template.get("name")
        or "Recurring journal entry"
    )
    journal = await accounting_service.prepare_journal(
        organization_id=organization_id,
        transaction_date=due_date,
        description=description,
        lines=lines,
        source_type="recurring",
        source_id=template_id,
    )
    entry = journal.get("entry") or {}
    entry_id = uuid.UUID(str(entry["id"]))

    if template.get("auto_post"):
        await accounting_service.validate_journal(entry_id=entry_id)
        posted = await accounting_service.post_journal(entry_id=entry_id)
        journal["entry"] = {**entry, "status": "POSTED", "post_result": posted}
    return journal["entry"]


async def list_executions(
    organization_id: uuid.UUID,
    *,
    template_id: Optional[uuid.UUID] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    return await repo.list_executions(
        organization_id, template_id=template_id, limit=limit
    )