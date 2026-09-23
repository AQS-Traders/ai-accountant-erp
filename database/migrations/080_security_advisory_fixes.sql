-- ============================================================================
-- 080_security_advisory_fixes.sql
--
-- Closes the two actionable WARN-level findings from the Supabase security
-- advisors (the other two are documented, deliberate design):
--
--   1. function_search_path_mutable
--      public.account_template_for_business_type had no fixed search_path, so
--      a malicious or careless role change on the search_path could redirect
--      its unqualified lookups.  The function body only ever references
--      public.account_templates (schema-qualified), so pinning the search
--      path changes no behaviour — it removes the attack surface.
--
--   2. anon_security_definer_function_executable
--      create_organization(...) and accept_my_team_invite() are SECURITY
--      DEFINER and were executable by `anon`.  Migration 071 deliberately
--      kept those grants because both functions self-reject when
--      auth.uid() IS NULL.  They are now revoked from anon/PUBLIC anyway:
--        * accept_my_team_invite is called ONLY from the authenticated
--          dashboard shell (frontend/src/app/(dashboard)/settings/page.tsx;
--          the (dashboard) layout requires a Supabase session and
--          src/middleware.ts redirects anonymous visitors).
--        * create_organization is called ONLY from the onboarding wizard with
--          the user's own JWT (frontend/src/app/onboarding/page.tsx), and
--          app/main.py documents that browser-with-JWT call as the intent.
--        * No anonymous sign-in flow exists (no signInAnonymously /
--          signInWithOtp anywhere in frontend/src), so `anon` never has a
--          legitimate reason to reach either RPC.
--      Revoking does not change any authenticated flow: EXECUTE is granted
--      back to `authenticated` explicitly.
--
--   NOT addressed here (deliberate):
--      * ai.worker_jobs has RLS enabled with no policies — service-role-only
--        table by design; the backend reaches it with the service key, which
--        bypasses RLS entirely.
--      * public.financial_operations — same service-role-only pattern.
--      * auth.leaked_password_protection — an Auth dashboard setting, not a
--        SQL object; must be enabled by hand in the Supabase dashboard.
--
-- Forward-only and idempotent: every statement is safe to re-run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Pin the search_path of account_template_for_business_type.
--    Idempotent: setting the same value twice is a no-op.
-- ---------------------------------------------------------------------------
alter function public.account_template_for_business_type(p_business_type public.business_type_code)
  set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- 2. Revoke anon EXECUTE on the two SECURITY DEFINER entry points.
--
--    Signatures are resolved from pg_proc by name (the same convention as
--    migration 071) so every overload is covered and a type-spelling
--    mismatch cannot make this silently miss one.
--    REVOKE/GRANT are themselves idempotent, so re-running is safe.
-- ---------------------------------------------------------------------------
do $$
declare
  target record;
  fn_names text[] := array[
    'create_organization',
    'accept_my_team_invite'
  ];
begin
  for target in
    select p.oid::regprocedure as signature
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname = any(fn_names)
  loop
    execute format('revoke execute on function %s from public', target.signature);
    execute format('revoke execute on function %s from anon', target.signature);
    execute format('grant execute on function %s to authenticated', target.signature);
  end loop;
end
$$;

-- Record the decision so a future audit does not re-litigate it.
comment on function public.account_template_for_business_type(p_business_type public.business_type_code) is
  'Returns the active account-template code for a business type, with a safe fallback chain. search_path pinned by migration 080 (advisor: function_search_path_mutable).';

comment on function public.create_organization(text, public.business_type_code, character, character, text, smallint, text, text, text, text, text, integer) is
  'SECURITY DEFINER onboarding entry point. anon EXECUTE revoked by migration 080; authenticated callers keep access. Self-rejects when auth.uid() is null.';
