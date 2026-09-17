-- =====================================================================
-- 071 — SECURITY HARDENING (cross-tenant report leak, evidence-based)
-- =====================================================================
-- FINDING (CONFIRMED_VULNERABILITY, severity CRITICAL)
--
-- Live project gghkbpdaqogncbrwzmpp, inspected 2026-09-18 via pg_proc.
-- Verbatim evidence:
--
--   proname              | prosecdef | proacl
--   ---------------------+-----------+-------------------------------------
--   get_trial_balance    | true      | {=X/postgres, postgres=X/postgres,
--                        |           |  anon=X/postgres,
--                        |           |  authenticated=X/postgres, ...}
--   get_income_statement | true      | {=X/postgres, postgres=X/postgres,
--                        |           |  anon=X/postgres,
--                        |           |  authenticated=X/postgres, ...}
--
-- Both are SECURITY DEFINER (so RLS on accounts / journal_lines /
-- journal_entries is bypassed) and both accept an ARBITRARY target_org
-- argument.  Their bodies contain NEITHER `auth.uid()` NOR any membership
-- check (position('auth.uid()' in prosrc) = 0 and
-- position('organization_members' in prosrc) = 0 for both).
--
-- With the `anon` EXECUTE grant plus the public anon key that ships in the
-- browser bundle, any unauthenticated internet caller can read ANY
-- organization's trial balance and profit & loss through PostgREST:
--
--   POST /rest/v1/rpc/get_trial_balance   {"target_org": "<any uuid>"}
--
-- Runtime confirmation: `set role anon; select count(*) from
-- public.get_trial_balance('00000000-...'::uuid)` returned 0 rows WITHOUT a
-- permission error, i.e. anon genuinely holds EXECUTE.  The all-zero UUID
-- returned no rows only because it owns no data.
--
-- IMPACT: cross-tenant financial disclosure (priority 1) and
-- unauthenticated disclosure of financial statements.
--
-- FIX — two layers, because the browser legitimately calls these RPCs
-- directly with the signed-in user's own organization:
--   Layer 1 (authorization): a membership guard inside the function, so an
--     `authenticated` caller can only read organizations they belong to.
--   Layer 2 (grants): revoke EXECUTE from PUBLIC and anon, so an
--     unauthenticated caller cannot reach the function at all.
-- `authenticated` RETAINS EXECUTE (client-side report pages call these).
-- service_role / postgres (auth.uid() IS NULL) keep unrestricted access
-- because the Python backend performs its own organization scoping.
--
-- Frontend call sites preserved by this change:
--   frontend/src/app/(dashboard)/accounting/trial-balance/page.tsx
--   frontend/src/app/(dashboard)/reports/profit-loss/page.tsx
--   frontend/src/app/(dashboard)/reports/balance-sheet/page.tsx
--   frontend/src/app/(dashboard)/page.tsx
-- =====================================================================

-- ---------------------------------------------------------------------
-- Layer 1a — single expression of the authorization rule.
-- SECURITY DEFINER so it can read organization_members regardless of the
-- caller's RLS visibility.  STABLE: it performs no writes.
-- ---------------------------------------------------------------------
create or replace function public.assert_org_report_access(target_org uuid)
returns void
language plpgsql
stable
security definer
set search_path = public
as $function$
declare
  caller uuid := auth.uid();
begin
  -- No JWT subject => service_role / postgres (backend) or anon.
  -- anon is handled by the grant revoke below; the backend scopes itself.
  if caller is null then
    return;
  end if;

  if public.is_org_member(target_org) then
    return;
  end if;

  raise exception
    'Not authorized to read financial reports for organization %', target_org
    using errcode = '42501';
end;
$function$;

comment on function public.assert_org_report_access(uuid) is
  'Raises 42501 unless the JWT caller is an ACTIVE member of target_org. '
  'No-op for service_role/postgres (auth.uid() IS NULL).';

revoke all on function public.assert_org_report_access(uuid) from public;
revoke all on function public.assert_org_report_access(uuid) from anon;
grant execute on function public.assert_org_report_access(uuid) to authenticated;
grant execute on function public.assert_org_report_access(uuid) to service_role;

-- ---------------------------------------------------------------------
-- Layer 1b — get_trial_balance: body copied VERBATIM from pg_get_functiondef
-- and converted from LANGUAGE sql to LANGUAGE plpgsql so the guard can run
-- first.  Column list, types, ordering and the current-financial-year filter
-- are unchanged.
-- ---------------------------------------------------------------------
create or replace function public.get_trial_balance(target_org uuid)
returns table(
  account_id uuid,
  account_code text,
  account_name text,
  account_type text,
  normal_balance text,
  total_debit numeric,
  total_credit numeric,
  balance numeric
)
language plpgsql
stable
security definer
set search_path = public
as $function$
begin
  perform public.assert_org_report_access(target_org);

  return query
  select a.id,
         a.code,
         a.name,
         a.account_type::text,
         a.normal_balance::text,
         coalesce(sum(gl.debit), 0) as total_debit,
         coalesce(sum(gl.credit), 0) as total_credit,
         (coalesce(sum(gl.debit), 0) - coalesce(sum(gl.credit), 0)) as balance
  from public.accounts a
  join public.journal_lines gl on gl.account_id = a.id
  join public.journal_entries e on e.id = gl.entry_id
  left join public.financial_years fy
    on fy.organization_id = a.organization_id and fy.is_current
  where a.organization_id = target_org
    and e.status in ('POSTED', 'REVERSED')
    and (fy.id is null
         or e.transaction_date between fy.start_date and fy.end_date)
  group by a.id, a.code, a.name, a.account_type, a.normal_balance;
end;
$function$;

-- ---------------------------------------------------------------------
-- Layer 1c — get_income_statement: same treatment.
-- ---------------------------------------------------------------------
create or replace function public.get_income_statement(target_org uuid)
returns table(
  organization_id uuid,
  account_type text,
  account_code text,
  account_name text,
  net_amount numeric
)
language plpgsql
stable
security definer
set search_path = public
as $function$
begin
  perform public.assert_org_report_access(target_org);

  return query
  select a.organization_id,
         a.account_type::text,
         a.code,
         a.name,
         coalesce(sum(gl.debit - gl.credit), 0) as net_amount
  from public.accounts a
  join public.journal_lines gl on gl.account_id = a.id
  join public.journal_entries e on e.id = gl.entry_id
  left join public.financial_years fy
    on fy.organization_id = a.organization_id and fy.is_current
  where a.organization_id = target_org
    and a.account_type in ('REVENUE', 'EXPENSE')
    and a.is_active
    and e.status in ('POSTED', 'REVERSED')
    and (fy.id is null
         or e.transaction_date between fy.start_date and fy.end_date)
  group by a.organization_id, a.account_type, a.code, a.name;
end;
$function$;

-- ---------------------------------------------------------------------
-- Layer 2 — grants.
--
-- Revoked from PUBLIC *and* anon.  Signatures are resolved from pg_proc by
-- name rather than hand-written, so this cannot silently miss a function
-- because of a type-spelling mismatch, and it covers every overload.
--
-- DELIBERATELY NOT REVOKED (documented intent + provably safe for anon):
--   * create_organization   — migration 045 records this anon grant as the
--     intentional signup/onboarding entry point.  It cannot be abused
--     anonymously anyway: it begins with
--     `v_user_id uuid := auth.uid(); IF v_user_id IS NULL THEN RAISE`,
--     so an anon call always fails.
--   * accept_my_team_invite — returns 0 immediately when auth.uid() IS NULL.
-- ---------------------------------------------------------------------
do $$
declare
  target record;
  fn_names text[] := array[
    'get_trial_balance',
    'get_income_statement',
    'create_next_financial_year',
    'set_reporting_year',
    'list_team_members',
    'audit_journal_entry_change',
    'guard_journal_entry_delete',
    'link_pending_team_invite'
  ];
begin
  for target in
    select p.oid::regprocedure as signature
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname = any (fn_names)
  loop
    execute format('revoke all on function %s from public', target.signature);
    execute format('revoke all on function %s from anon', target.signature);
    raise notice 'hardened grants for %', target.signature;
  end loop;
end
$$;

-- `authenticated` must still be able to call the two report RPCs: the
-- trial-balance, profit-loss and balance-sheet pages call them directly.
grant execute on function public.get_trial_balance(uuid) to authenticated;
grant execute on function public.get_income_statement(uuid) to authenticated;

-- ---------------------------------------------------------------------
-- Post-migration verification (run manually; expected results inline).
--
--   -- 1. anon must no longer hold EXECUTE on the report RPCs:
--   select p.proname, p.proacl::text
--   from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--   where n.nspname = 'public'
--     and p.proname in ('get_trial_balance','get_income_statement');
--   -- expect: acl lists authenticated + service_role + postgres, with
--   --         NO 'anon=X' entry and NO leading '=X/postgres' (PUBLIC).
--
--   -- 2. anon call must now be refused:
--   set role anon;
--   select * from public.get_trial_balance(
--     '00000000-0000-0000-0000-000000000000'::uuid);
--   -- expect: ERROR: permission denied for function get_trial_balance
--
--   -- 3. the guard must refuse a non-member even as `authenticated`:
--   --    (run as a signed-in user's JWT for an organization they are not in)
--   -- expect: ERROR 42501 Not authorized to read financial reports for ...
--
--   -- 4. the app must still work for a member (frontend report pages):
--   select count(*) from public.get_trial_balance('<your own org uuid>');
--   -- expect: a row count, no error.
-- ---------------------------------------------------------------------