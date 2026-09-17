"""
AI Worker - DB-backed background job processor (Work Stream C)
===============================================================
A SUPERVISED process (not a FastAPI background task) that claims queued
AI runs from ``ai.worker_jobs`` (FOR UPDATE SKIP LOCKED via
``ai.claim_worker_job``) and executes them through the SAME trusted
``agent.execute()`` pipeline as the synchronous endpoint.

Usage:
    venv\\Scripts\\python scripts\\ai_worker.py            # poll forever
    venv\\Scripts\\python scripts\\ai_worker.py --once     # drain one job

Jobs are enqueued by POST /api/ai/jobs; clients poll GET /api/ai/jobs/{id}
or /api/ai/progress?conversation_id=... (the run records its steps exactly
like a synchronous run, because it IS the same pipeline).

Authorisation model: enqueueing requires a valid JWT (enforced by the
API), so the job's user identity is trustworthy.  The AUTHORIZATION is NOT
inherited from enqueue time: the worker reconstructs a fresh AuthContext
(membership + status + role) before every execution, because a job can sit
in the queue while the user is suspended, removed or downgraded.  A job
whose organization no longer matches the user's active membership is
refused rather than executed in the wrong tenant.
"""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import structlog

log = structlog.get_logger(__name__)

WORKER_ID = f"ai-worker-{uuid.uuid4().hex[:8]}"
POLL_INTERVAL_SECONDS = 1.0


async def process_one(client) -> bool:
    """Claim and run one job. Returns True when a job was processed."""
    from app.agent import execute
    from app.models.schemas import AttachmentRef

    response = (
        client.schema("ai")  # the function lives in the ai schema (052)
        .rpc(
            "claim_worker_job",
            {"p_worker": WORKER_ID, "p_lease_seconds": 300},
        )
        .execute()
    )
    rows = response.data or []
    # The function RETURNS a single ai.worker_jobs row (NULL when nothing
    # is claimable), so PostgREST yields an OBJECT or null - not an array.
    # Normalise both shapes; a scalar/None simply means "no job".
    if isinstance(rows, dict):
        rows = [rows]
    if not rows or not isinstance(rows[0], dict):
        return False
    job = rows[0]
    job_id = job.get("id")
    if not job_id:
        # PostgREST represents a NULL single-composite return (nothing
        # claimable) as an object of nulls - there is no job to run.
        return False
    payload = job.get("payload") or {}
    log.info("worker.claimed", job_id=job_id, worker=WORKER_ID)

    try:
        # SECURITY: reconstruct AUTHORIZATION at execution time.  The identity
        # was verified when the job was enqueued, but membership, its status
        # and the user's role may all have changed since.  Role, permissions
        # and organization are read from the database — never from the job
        # payload.  This also makes the permission check in tool_router run:
        # it used to be skipped entirely because auth was None.
        from app.auth import build_auth_context_for_user

        auth_ctx = await build_auth_context_for_user(uuid.UUID(job["user_id"]))
        job_org = uuid.UUID(job["organization_id"])
        if auth_ctx.organization_id != job_org:
            # The queue row disagrees with the user's live membership. Refuse
            # rather than execute a financial mutation in the wrong tenant.
            raise PermissionError(
                "Job organization does not match the user's active membership"
            )

        agent_response = await execute(
            user_message=payload.get("message", ""),
            user_id=auth_ctx.user_id,
            organization_id=auth_ctx.organization_id,
            auth=auth_ctx,
            conversation_id=payload.get("conversation_id"),
            attachments=[
                AttachmentRef(**a) for a in (payload.get("attachments") or [])
            ],
        )
        # Completion goes through an ownership-checked, cancellation-aware RPC.
        # The old code updated `worker_jobs` by id alone, so a worker whose
        # lease had already lapsed could overwrite the result another worker
        # produced.  finish_worker_job() also applies retry backoff and
        # terminalises the job once attempts are exhausted.
        try:
            client.schema("ai").rpc(
                "finish_worker_job",
                {
                    "p_job_id": job_id,
                    "p_worker": WORKER_ID,
                    "p_status": "SUCCEEDED",
                    "p_result": agent_response.model_dump(mode="json"),
                    "p_error": None,
                },
            ).execute()
        except Exception as finish_exc:  # noqa: BLE001 - lease lost; recorded below
            log.error("worker.finish_failed", job_id=job_id, error=str(finish_exc))
        log.info("worker.completed", job_id=job_id,
                 status=agent_response.status.value)
    except Exception as exc:  # noqa: BLE001 - job failure is recorded, not raised
        log.error("worker.failed", job_id=job_id, error=str(exc))
        try:
            client.schema("ai").rpc(
                "finish_worker_job",
                {
                    "p_job_id": job_id,
                    "p_worker": WORKER_ID,
                    "p_status": "FAILED",
                    "p_result": None,
                    "p_error": str(exc)[:2000],
                },
            ).execute()
        except Exception as finish_exc:  # noqa: BLE001 - lease lost
            log.error("worker.finish_failed", job_id=job_id, error=str(finish_exc))
    return True


async def main(once: bool = False) -> None:
    from app.database import get_service_client

    client = get_service_client()
    log.info("worker.started", worker=WORKER_ID)
    while True:
        try:
            processed = await process_one(client)
        except Exception as exc:  # noqa: BLE001 - the worker must survive
            # A malformed claim/row must never kill the supervised loop;
            # the job's lease lapses and it is re-claimed automatically.
            log.error("worker.loop_error", error=str(exc))
            processed = False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        if once and processed:
            break
        if not processed:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        if once:
            break


if __name__ == "__main__":
    asyncio.run(main(once="--once" in sys.argv))
