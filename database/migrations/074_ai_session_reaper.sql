-- =====================================================================
-- 074 — AI SESSION REAPER (stale WAITING_FOR_USER sessions)
-- =====================================================================
-- Evidence (live, 2026-09-18): four sessions opened from the same failed
-- invoice request were left in WAITING_FOR_USER / AWAITING_CLARIFICATION
-- after their runs had ended:
--   9ac54f57, 2b3943c4, dd7428cb, 9a75ed04
-- Nothing expires an abandoned clarification.  The dashboard then shows
-- "waiting for your answer" for a question the user has already moved on
-- from, which is what made users re-send instead of answering and left
-- orphaned rows behind.
--
-- This adds an explicit expiry marker and a bounded reaper.  It does NOT
-- touch financial data: only session rows whose status is a WAITING state
-- and which have been idle past the cutoff.
-- =====================================================================

alter table ai.execution_sessions
  add column if not exists expires_at timestamptz;

comment on column ai.execution_sessions.expires_at is
  'When a WAITING_FOR_USER session should be considered abandoned. Set by '
  'the waiter itself; swept by ai.reap_stale_sessions().';

create index if not exists idx_ai_execution_sessions_reapable
  on ai.execution_sessions (updated_at)
  where status in ('WAITING_FOR_USER');

-- ---------------------------------------------------------------------
-- reap: expire abandoned waiting sessions.
--   * only sessions in a WAITING state are eligible
--   * only when idle past the cutoff (default 24h)
--   * terminal status is CANCELLED, never COMPLETED/FAILED, so nothing is
--     reported as a success it never was
--   * a bounded batch keeps the sweep cheap
-- ---------------------------------------------------------------------
create or replace function ai.reap_stale_sessions(
  p_stale_after interval default interval '24 hours',
  p_limit integer default 500
)
returns integer
language plpgsql
security definer
set search_path = ai, public
as $function$
declare
  v_count integer;
begin
  with stale as (
    select id
    from ai.execution_sessions
    where status in ('WAITING_FOR_USER')
      and coalesce(expires_at, updated_at + p_stale_after) < now()
    order by updated_at
    limit p_limit
    for update skip locked
  )
  update ai.execution_sessions s
  set status = 'CANCELLED',
      current_phase = 'CANCELLED',
      completed_at = coalesce(s.completed_at, now()),
      updated_at = now()
  where s.id in (select id from stale);

  get diagnostics v_count = row_count;
  return v_count;
end;
$function$;

revoke all on function ai.reap_stale_sessions(interval, integer) from public;
revoke all on function ai.reap_stale_sessions(interval, integer) from anon;
revoke all on function ai.reap_stale_sessions(interval, integer) from authenticated;
grant execute on function ai.reap_stale_sessions(interval, integer) to service_role;

-- ---------------------------------------------------------------------
-- Verification (run manually):
--   select ai.reap_stale_sessions();          -- sweep, returns rows expired
--   select status, count(*) from ai.execution_sessions group by status;
--
--   -- Financially safe: only session bookkeeping changes. Verify no
--   -- financial table is touched by the function:
--   --   pg_get_functiondef('ai.reap_stale_sessions'::regproc) references
--   --   ai.execution_sessions only.
-- ---------------------------------------------------------------------