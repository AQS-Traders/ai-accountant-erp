"""
Recurring Repository — Supabase data access for recurring templates + executions.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.database import fetch_many, fetch_one, insert_one, update_one


async def get_template(
    organization_id: uuid.UUID, *, template_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    return await fetch_one(
        "recurring_templates",
        filters={
            "id": str(template_id),
            "organization_id": str(organization_id),
        },
    )


async def list_templates(
    organization_id: uuid.UUID,
    *,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {"organization_id": str(organization_id)}
    if status:
        filters["status"] = status
    return await fetch_many(
        "recurring_templates",
        filters=filters,
        order="next_due_date.asc",
        limit=limit,
    )


async def create_template(
    *,
    organization_id: uuid.UUID,
    name: str,
    description: Optional[str],
    frequency: str,
    schedule_day: Optional[int],
    next_due_date: str,
    auto_post: bool,
    reminder_days: int,
    journal_template: Dict[str, Any],
    source_type: str,
    created_by: Optional[uuid.UUID],
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "organization_id": str(organization_id),
        "name": name,
        "description": description,
        "frequency": frequency,
        "schedule_day": schedule_day,
        "next_due_date": next_due_date,
        "auto_post": auto_post,
        "reminder_days": reminder_days,
        "journal_template": journal_template,
        "source_type": source_type,
        "status": "active",
        "total_generated": 0,
        "created_by": str(created_by) if created_by else None,
    }
    return await insert_one("recurring_templates", data=data)


async def update_template(
    *,
    template_id: uuid.UUID,
    data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    return await update_one("recurring_templates", row_id=template_id, data=data)


async def get_due_templates(
    organization_id: uuid.UUID, *, as_of: str, limit: int = 200
) -> List[Dict[str, Any]]:
    """All ACTIVE templates whose next_due_date <= as_of (sorted by due date)."""
    active = await fetch_many(
        "recurring_templates",
        filters={"organization_id": str(organization_id), "status": "active"},
        order="next_due_date.asc",
        limit=500,
    )
    due = [t for t in active if str(t.get("next_due_date") or "") <= as_of]
    return due[:limit]


async def create_execution(
    *,
    template_id: uuid.UUID,
    organization_id: uuid.UUID,
    scheduled_date: str,
    status: str = "pending",
    journal_entry_id: Optional[uuid.UUID] = None,
    error_message: Optional[str] = None,
) -> Dict[str, Any]:
    return await insert_one(
        "recurring_executions",
        data={
            "template_id": str(template_id),
            "organization_id": str(organization_id),
            "scheduled_date": scheduled_date,
            "status": status,
            "journal_entry_id": str(journal_entry_id) if journal_entry_id else None,
            "error_message": error_message,
            "executed_at": None,
        },
    )


async def list_executions(
    organization_id: uuid.UUID,
    *,
    template_id: Optional[uuid.UUID] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {"organization_id": str(organization_id)}
    if template_id:
        filters["template_id"] = str(template_id)
    return await fetch_many(
        "recurring_executions",
        filters=filters,
        order="scheduled_date.desc",
        limit=limit,
    )