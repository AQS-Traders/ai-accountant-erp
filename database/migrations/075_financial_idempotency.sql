-- =====================================================================
-- 075 — IDEMPOTENCY FOR FINANCIAL MUTATIONS
-- =====================================================================
-- FINDING (CONFIRMED, High). The only replay protection in the codebase was
-- in app/tool_router.py:
--
--   if slug == "record_cash_sale" and session_id:
--       ... compare float(arguments["amount"]) against prior calls ...
--
-- All four problems verified in the source:
--   * ONE tool slug only — every other financial mutation (invoice, bill,
--     expense, payment, receipt, credit note, journal, transfer, asset) had
--     no protection at all;
--   * ONE execution session — a browser retry, HTTP retry, provider retry,
--     worker retry or process restart produced a duplicate document;
--   * keyed on the AMOUNT — two legitimately identical transactions were
--     treated as one, i.e. a MISSING financial record, the worse failure;
--   * nothing persisted, so nothing survived a restart.
--
-- This adds an explicit, persistent, organization-scoped idempotency ledger.
-- =====================================================================

create table if not exists public.financial_operations (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null
    references public.organizations(id) on delete cascade,
  user_id uuid,
  operation text not null,
  idempotency_key text not null,
  request_hash text not null,
  status text not null default 'IN_PROGRESS',
  result jsonb,
  error text,
  claimed_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint financial_operations_status_chk
    check (status in ('IN_PROGRESS', 'COMPLETED', 'FAILED')),
  constraint financial_operations_key_uniq
    unique (organization_id, operation, idempotency_key)
);

comment on table public.financial_operations is
  'Idempotency ledger for financial mutations. One row per '
  '(organization, operation, idempotency_key). An identical retry replays '
  'the stored result; the same key with a different request_hash is refused.';

create index if not exists idx_financial_operations_stale
  on public.financial_operations (claimed_at)
  where status = 'IN_PROGRESS';

-- Backend-only: the service-role client writes here. RLS enabled with NO
-- policy by design, mirroring public.document_sequences.
alter table public.financial_operations enable row level security;
revoke all on public.financial_operations from anon;
revoke all on public.financial_operations from authenticated;
grant all on public.financial_operations to service_role;

-- ---------------------------------------------------------------------
-- claim: atomically take the key, or report why not.
--
--   claimed      -> caller owns the key and should perform the mutation
--   replay       -> already COMPLETED with the SAME parameters; use `result`
--   in_progress  -> another attempt holds it, or the claim is still live
--
-- Raises 40001 when the key was used before with DIFFERENT parameters (that
-- is not a retry).  An IN_PROGRESS row older than p_stale_after is treated as
-- abandoned — the process died mid-mutation — and may be re-claimed, so a
-- crash cannot wedge a key forever.
-- ---------------------------------------------------------------------
create or replace function public.claim_financial_operation(
  p_organization_id uuid,
  p_operation text,
  p_idempotency_key text,
  p_request_hash text,
  p_user_id uuid default null,
  p_stale_after interval default interval '10 minutes'
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_row public.financial_operations;
begin
  if p_organization_id is null or p_operation is null
     or p_idempotency_key is null or p_request_hash is null then
    raise exception 'organization, operation, key and hash are required'
      using errcode = '22023';
  end if;

  insert into public.financial_operations (
    organization_id, user_id, operation, idempotency_key, request_hash
  )
  values (
    p_organization_id, p_user_id, p_operation, p_idempotency_key, p_request_hash
  )
  on conflict (organization_id, operation, idempotency_key) do nothing
  returning * into v_row;

  if found then
    return jsonb_build_object('state', 'claimed', 'id', v_row.id);
  end if;

  -- Existing row: lock it so two racers cannot both act on it.
  select * into v_row
  from public.financial_operations
  where organization_id = p_organization_id
    and operation = p_operation
    and idempotency_key = p_idempotency_key
  for update;

  if not found then
    return jsonb_build_object('state', 'in_progress');
  end if;

  if v_row.request_hash <> p_request_hash then
    raise exception
      'idempotency key % was already used for % with different parameters',
      p_idempotency_key, p_operation
      using errcode = '40001';
  end if;

  if v_row.status = 'COMPLETED' then
    return jsonb_build_object(
      'state', 'replay',
      'id', v_row.id,
      'result', coalesce(v_row.result, '{}'::jsonb)
    );
  end if;

  if v_row.status = 'FAILED' then
    update public.financial_operations
    set status = 'IN_PROGRESS', error = null,
        claimed_at = now(), completed_at = null
    where id = v_row.id;
    return jsonb_build_object('state', 'claimed', 'id', v_row.id);
  end if;

  -- IN_PROGRESS: only reclaim when the claim looks abandoned.
  if v_row.claimed_at < now() - p_stale_after then
    update public.financial_operations
    set claimed_at = now() where id = v_row.id;
    return jsonb_build_object('state', 'claimed', 'id', v_row.id, 'reclaimed', true);
  end if;

  return jsonb_build_object('state', 'in_progress', 'id', v_row.id);
end;
$function$;

-- ---------------------------------------------------------------------
-- complete: record the terminal outcome so a later retry can replay it.
-- ---------------------------------------------------------------------
create or replace function public.complete_financial_operation(
  p_id uuid,
  p_status text,
  p_result jsonb default null,
  p_error text default null
)
returns void
language plpgsql
security definer
set search_path = public
as $function$
begin
  if p_status not in ('COMPLETED', 'FAILED') then
    raise exception 'invalid status: %', p_status using errcode = '22023';
  end if;
  update public.financial_operations
  set status = p_status,
      result = case
        when p_status = 'COMPLETED' then coalesce(p_result, '{}'::jsonb)
        else null
      end,
      error = p_error,
      completed_at = now()
  where id = p_id;
end;
$function$;

revoke all on function public.claim_financial_operation(uuid, text, text, text, uuid, interval) from public;
revoke all on function public.claim_financial_operation(uuid, text, text, text, uuid, interval) from anon;
revoke all on function public.claim_financial_operation(uuid, text, text, text, uuid, interval) from authenticated;
grant execute on function public.claim_financial_operation(uuid, text, text, text, uuid, interval) to service_role;

revoke all on function public.complete_financial_operation(uuid, text, jsonb, text) from public;
revoke all on function public.complete_financial_operation(uuid, text, jsonb, text) from anon;
revoke all on function public.complete_financial_operation(uuid, text, jsonb, text) from authenticated;
grant execute on function public.complete_financial_operation(uuid, text, jsonb, text) to service_role;