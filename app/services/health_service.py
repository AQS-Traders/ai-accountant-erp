"""
Financial Health Scoring Service
==================================KCalculates and tracks financial health scores for organizations.
"""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional
import structlog
from app.database import fetch_one

log = structlog.get_logger(__name__)


async def calculate_health(organization_id: uuid.UUID) -> Dict[str, Any]:
    """Calculate and store financial health snapshot."""
    assets = await _sum_accounts(organization_id, "ASSET")
    liabilities = await _sum_accounts(organization_id, "LIABILITY")
    revenue = await _sum_accounts(organization_id, "REVENUE")
    expenses = await _sum_accounts(organization_id, "EXPENSE")

    # Liquidity (current ratio: 2.0 = 100, 1.0 = 50, 0.5 = 0)
    if liabilities > 0:
        liquidity = min(100, max(0, int((assets / liabilities) * 50)))
    else:
        liquidity = 100 if assets > 0 else 50

    # Profitability (net margin: 20%+ = 100, 0% = 50, negative = 0)
    if revenue > 0:
        margin = (revenue - expenses) / revenue
        profitability = min(100, max(0, int(margin * 250 + 50)))
    else:
        profitability = 50

    efficiency = 70  # Placeholder until receivables turnover is implemented
    overall = (liquidity * 30 + profitability * 40 + efficiency * 30) // 100

    metrics = {
        "current_assets": assets, "current_liabilities": liabilities,
        "current_ratio": round(assets / liabilities, 2) if liabilities > 0 else None,
        "revenue": revenue, "expenses": expenses, "net_profit": revenue - expenses,
        "net_margin": round((revenue - expenses) / revenue * 100, 2) if revenue > 0 else 0,
    }

    alerts = []
    if liquidity < 40:
        alerts.append({"type": "critical", "message": "Low liquidity", "action": "Review payables"})
    if profitability < 40:
        alerts.append({"type": "warning", "message": "Low profitability", "action": "Review expenses"})

    await _store_snapshot(organization_id, overall, liquidity, profitability, efficiency, metrics, alerts)

    return {"overall": overall, "liquidity": liquidity, "profitability": profitability,
            "efficiency": efficiency, "metrics": metrics, "alerts": alerts}


async def _sum_accounts(org_id: uuid.UUID, acc_type: str) -> float:
    result = await fetch_one(
        "journal_lines",
        columns="SUM(CASE WHEN a.account_type = %(t)s THEN l.debit - l.credit ELSE 0 END) as total",
        filters={"l.organization_id": str(org_id)},
    )
    return float(result.get("total", 0)) if result and result.get("total") else 0


async def _store_snapshot(org_id, overall, liquidity, profitability, efficiency, metrics, alerts) -> None:
    from app.database import insert_one
    await insert_one("financial_health_snapshots", data={
        "organization_id": str(org_id), "overall_score": overall,
        "liquidity_score": liquidity, "profitability_score": profitability,
        "efficiency_score": efficiency, "metrics": metrics, "alerts": alerts,
    })


async def get_latest_snapshot(organization_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    return await fetch_one("financial_health_snapshots",
                           filters={"organization_id": str(organization_id)},
                           order="snapshot_date DESC")


async def get_history(organization_id: uuid.UUID, limit: int = 12) -> List[Dict[str, Any]]:
    from app.database import fetch_many
    return await fetch_many("financial_health_snapshots",
                           filters={"organization_id": str(organization_id)},
                           order="snapshot_date DESC", limit=limit)
