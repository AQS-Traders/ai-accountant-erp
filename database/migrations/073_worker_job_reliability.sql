-- =====================================================================
-- 073 — WORKER JOB RELIABILITY (attempts, backoff, cancellation, lease)
-- =====================================================================
-- Evidence (live, 2026-09-18):
--   * ai.worker_jobs has NO attempts / max_attempts / cancellation column
--     (information_schema.columns) — so there is no retry accounting, no
--     dead-letter state and no way to cancel a queued run.
--   * ai.claim_worker_job claims `QUEUED OR (RUNNING AND lease expired)`
--     but never counts attempts: an abandoned job is re-claimed forever.
--   * scripts/ai_worker.py finishes a job with
--       .update({...}).eq("id", job_id)
--     with NO ownership check, so a worker whose lease already lapsed can
--     overwrite the result another worker just produced.
--   * nothing ever reads a cancellation flag before executing.
--
-- This migration adds the missing columns and moves completion behind an
-- ownership-checked, cancellation-aware, backoff-aware RPC.
-- =====================================================================

alter table ai.worker_jobs
  add column if not exists attempts integer not null default 0;
alter table ai.worker_jobs
  add column if not exists max_attempts integer not null default 3;
alter table ai.worker_jobs
  add column if not exists next_attempt_at timestamptz not null default now();
alter table ai.worker_jobs
  add column if not exists cancelled_at timestamptz;

comment on column ai.worker_jobs.attempts is
  'Number of times this job has been claimed. Incremented atomically by '
  'ai.claim_worker_job().';
comment on column ai.worker_jobs.max_attempts is
  'Give-up threshold. Once attempts >= max_attempts the job is terminal.';
comment on column ai.worker_jobs.next_attempt_at is
  'Earliest time a QUEUED job may be claimed (exponential backoff).';
comment on column ai.worker_jobs.cancelled_at is
  'Set when cancellation was requested; a running job honours it on finish.';

create index if not exists idx_ai_worker_jobs_claimable
  on ai.worker_jobs (next_attempt_at, created_at)
  where status = 'QUEUED';

-- ---------------------------------------------------------------------
-- claim: honour cancellation + backoff, count attempts, and terminalise
-- abandoned jobs whose attempts are exhausted (self-healing, so a hard
-- worker death cannot leave a job RUNNING forever).
-- ---------------------------------------------------------------------
create or replace function ai.claim_worker_job(
  p_worker text,
  p_lease_seconds integer default 180
)
returns ai.worker_jobs
language plpgsql
security definer
set search_path = ai, public
as $function$
declare
  v_job ai.worker_jobs;
begin
  -- 1. Dead-letter abandoned jobs that have no attempts left.
  update ai.worker_jobs j
  set status = 'FAILED',
      error = coalesce(j.error, 'attempts exhausted after lease expiry'),
      lease_expires_at = null,
      claimed_by = null,
      updated_at = now()
  where j.status = 'RUNNING'
    and j.lease_expires_at < now()
    and j.attempts >= j.max_attempts;

  -- 2. Claim the next eligible job.
  update ai.worker_jobs j
  set status = 'RUNNING',
      claimed_by = p_worker,
      claimed_at = now(),
      lease_expires_at = now() + make_interval(secs => p_lease_seconds),
      attempts = j.attempts + 1,
      updated_at = now()
  where j.id = (
    select id
    from ai.worker_jobs
    where cancelled_at is null
      and attempts < max_attempts
      and (
        (status = 'QUEUED' and next_attempt_at <= now())
        or (status = 'RUNNING' and lease_expires_at < now())
      )
    order by next_attempt_at, created_at
    limit 1
    for update skip locked
  )
  returning * into v_job;

  return v_job;
end;
$function$;

-- ---------------------------------------------------------------------
-- finish: ownership-checked, lease-checked, cancellation-aware completion
-- with exponential backoff on retryable failure.
--
-- Raises 42501 when the calling worker does not hold the lease, so a worker
-- whose lease lapsed can no longer overwrite another worker's result.
-- ---------------------------------------------------------------------
create or replace function ai.finish_worker_job(
  p_job_id uuid,
  p_worker text,
  p_status text,
  p_result jsonb default null,
  p_error text default null
)
returns ai.worker_jobs
language plpgsql
security definer
set search_path = ai, public
as $function$
declare
  v_job ai.worker_jobs;
  v_retry boolean;
begin
  if p_status not in ('SUCCEEDED', 'FAILED') then
    raise exception 'invalid finish status: %', p_status using errcode = '22023';
  end if;

  select * into v_job from ai.worker_jobs where id = p_job_id for update;
  if not found then
    return null;
  end if;

  -- Ownership: only the worker currently holding the lease may finish it.
  if v_job.claimed_by is distinct from p_worker then
    raise exception 'job % is not held by worker %', p_job_id, p_worker
      using errcode = '42501';
  end if;
  if v_job.lease_expires_at is not null and v_job.lease_expires_at < now() then
    raise exception 'lease for job % has expired', p_job_id using errcode = '42501';
  end if;

  -- Cancellation wins over completion: a cancelled job never reports success.
  if v_job.cancelled_at is not null then
    update ai.worker_jobs j
    set status = 'CANCELLED',
        error = coalesce(p_error, 'cancelled by user'),
        lease_expires_at = null,
        claimed_by = null,
        updated_at = now()
    where j.id = p_job_id
    returning * into v_job;
    return v_job;
  end if;

  v_retry := (p_status = 'FAILED' and v_job.attempts < v_job.max_attempts);

  update ai.worker_jobs j
  set status = case when v_retry then 'QUEUED' else p_status end,
      result = case when v_retry then null else p_result end,
      error = p_error,
      lease_expires_at = null,
      claimed_by = null,
      -- 5s, 10s, 20s ... capped at 300s.
      next_attempt_at = case
        when v_retry
        then now() + make_interval(secs => least(300, (2 ^ v_job.attempts)::int * 5))
        else j.next_attempt_at
      end,
      updated_at = now()
  where j.id = p_job_id
  returning * into v_job;

  return v_job;
end;
$function$;

-- ---------------------------------------------------------------------
-- cancel: a user may cancel their OWN job; the backend (service_role /
-- postgres, auth.uid() IS NULL) may cancel any job.
-- ---------------------------------------------------------------------
create or replace function ai.cancel_worker_job(p_job_id uuid)
returns ai.worker_jobs
language plpgsql
security definer
set search_path = ai, public
as $function$
declare
  v_job ai.worker_jobs;
  v_uid uuid := auth.uid();
begin
  select * into v_job from ai.worker_jobs where id = p_job_id for update;
  if not found then
    return null;
  end if;

  if v_uid is not null and v_job.user_id <> v_uid then
    raise exception 'Not authorized to cancel job %', p_job_id
      using errcode = '42501';
  end if;

  update ai.worker_jobs j
  set cancelled_at = coalesce(j.cancelled_at, now()),
      -- A QUEUED job stops immediately; a RUNNING job is stopped at finish.
      status = case when j.status = 'QUEUED' then 'CANCELLED' else j.status end,
      lease_expires_at = case when j.status = 'QUEUED' then null else j.lease_expires_at end,
      updated_at = now()
  where j.id = p_job_id
  returning * into v_job;

  return v_job;
end;
$function$;

-- ---------------------------------------------------------------------
-- Grants: worker tables/queue functions are backend-only by design
-- (ai.worker_jobs has RLS enabled with NO policy).  cancel_worker_job is
-- callable by a signed-in user because it enforces job ownership itself.
-- ---------------------------------------------------------------------
revoke all on function ai.claim_worker_job(text, integer) from public;
revoke all on function ai.claim_worker_job(text, integer) from anon;
revoke all on function ai.claim_worker_job(text, integer) from authenticated;
grant execute on function ai.claim_worker_job(text, integer) to service_role;

revoke all on function ai.finish_worker_job(uuid, text, text, jsonb, text) from public;
revoke all on function ai.finish_worker_job(uuid, text, text, jsonb, text) from anon;
revoke all on function ai.finish_worker_job(uuid, text, text, jsonb, text) from authenticated;
grant execute on function ai.finish_worker_job(uuid, text, text, jsonb, text) to service_role;

revoke all on function ai.cancel_worker_job(uuid) from public;
revoke all on function ai.cancel_worker_job(uuid) from anon;
grant execute on function ai.cancel_worker_job(uuid) to authenticated;
grant execute on function ai.cancel_worker_job(uuid) to service_role;

-- ---------------------------------------------------------------------
-- Verification (run manually):
--   select column_name from information_schema.columns
--   where table_schema='ai' and table_name='worker_jobs'
--     and column_name in ('attempts','max_attempts','next_attempt_at','cancelled_at');
--   -- expect 4 rows.
--
--   -- A second worker must not be able to finish a job it does not hold:
--   select ai.finish_worker_job('<job uuid>', 'someone-else', 'SUCCEEDED');
--   -- expect: ERROR 42501 job ... is not held by worker someone-else
-- ---------------------------------------------------------------------