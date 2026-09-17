-- =====================================================================
-- 072 — RLS AUTHORIZATION HARDENING (evidence-based)
-- =====================================================================
-- Live project gghkbpdaqogncbrwzmpp, pg_policies inspected 2026-09-18.
-- Every finding below is a verbatim policy from that inspection.
--
-- FINDING 1 (CONFIRMED, High) — organizations INSERT is wide open:
--   org_insert  ALLOW INSERT TO authenticated  WITH CHECK (true)
--   Any signed-in user can insert arbitrary organization rows, bypassing
--   create_organization() — which is the only path that also provisions the
--   owner membership, financial year, accounting periods and chart of
--   accounts. Result: orphan tenant rows with no owner and no COA.
--   Verified: the frontend calls supabase.rpc("create_organization", ...)
--   and NEVER inserts into organizations directly.
--
-- FINDING 2 (CONFIRMED, High — accounting integrity) — organizations UPDATE
--   only requires ordinary membership:
--   org_update  USING (is_org_member(id)) WITH CHECK (is_org_member(id))
--   The settings page updates business_type, base_currency_code,
--   fiscal_year_end_month, tax_number and country_code through this policy,
--   so ANY member — including VIEWER (rank 5) — can silently change the
--   organization's base currency and fiscal year end. That is a financially
--   material change, not a profile tweak.
--
-- FINDING 3 (CONFIRMED, Critical) — self-service PRIVILEGE ESCALATION:
--   members_update USING/WITH CHECK ((user_id = auth.uid()) OR is_org_member(organization_id))
--   Because `user_id = auth.uid()` satisfies the policy for the caller's OWN
--   row, a VIEWER can simply UPDATE their own organization_members.role_id to
--   the OWNER role. Nothing in the policy compares role ranks.
--
-- FINDING 4 (CONFIRMED, High) — any member can delete any member:
--   members_delete USING (is_org_member(organization_id))
--   An ordinary member can delete the OWNER's membership row.
--
-- FINDING 5 (CONFIRMED, High) — any member can add members with any role:
--   members_insert WITH CHECK (is_org_member(organization_id))
--
-- RLS role ranks (verified): OWNER=1, ADMIN=2, ACCOUNTANT=3, MANAGER=4,
-- VIEWER=5, and has_org_role(org, min_rank) is true when rank <= min_rank.
-- So has_org_role(org, 2) means "owner or admin".
--
-- SCOPE / SAFETY: these policies govern the CLIENT path (anon key + user
-- JWT). The Python backend uses the service-role key and bypasses RLS, so
-- these changes cannot break server-side flows. Verified safe against the
-- frontend: it SELECTs organization_members only (never insert/update/
-- delete), revokes invites through team_invites (already owner/admin gated),
-- and links invitations via the accept_my_team_invite() SECURITY DEFINER
-- RPC, which is unaffected by RLS.
-- =====================================================================

-- ---------------------------------------------------------------------
-- FINDING 1 — deny direct organization INSERT.
-- create_organization() is SECURITY DEFINER and therefore unaffected; it is
-- the single authorized provisioning path.
-- ---------------------------------------------------------------------
drop policy if exists org_insert on public.organizations;
-- No replacement INSERT policy is created on purpose: with RLS enabled and
-- no permissive INSERT policy, PostgreSQL denies client INSERTs. The
-- explicit denial is recorded here rather than left implicit.
comment on table public.organizations is
  'Tenant root. Client INSERT is intentionally denied (no INSERT policy): '
  'provisioning must go through public.create_organization(), which also '
  'creates the owner membership, financial year, periods and chart of '
  'accounts. UPDATE requires OWNER or ADMIN.';

-- ---------------------------------------------------------------------
-- FINDING 2 — organization profile changes require OWNER or ADMIN.
-- ---------------------------------------------------------------------
drop policy if exists org_update on public.organizations;
create policy org_update on public.organizations
  for update
  to authenticated
  using (public.has_org_role(id, 2))
  with check (public.has_org_role(id, 2));

-- ---------------------------------------------------------------------
-- FINDING 3/4/5 — membership writes require OWNER or ADMIN.
--
-- Specifically closes:
--   * self-promotion: the old `user_id = auth.uid()` branch let a member
--     write their OWN row, including role_id;
--   * owner removal: the OWNER membership can no longer be deleted by an
--     ordinary member;
--   * arbitrary role assignment: only an OWNER may grant the OWNER role, so
--     an ADMIN cannot promote themselves either.
-- ---------------------------------------------------------------------
drop policy if exists members_insert on public.organization_members;
create policy members_insert on public.organization_members
  for insert
  to authenticated
  with check (public.has_org_role(organization_id, 2));

drop policy if exists members_update on public.organization_members;
create policy members_update on public.organization_members
  for update
  to authenticated
  using (public.has_org_role(organization_id, 2))
  with check (
    public.has_org_role(organization_id, 2)
    and (
      -- Granting OWNER requires being an OWNER.
      role_id <> (
        select r.id from public.organization_roles r where r.code = 'OWNER'
      )
      or public.has_org_role(organization_id, 1)
    )
  );

drop policy if exists members_delete on public.organization_members;
create policy members_delete on public.organization_members
  for delete
  to authenticated
  using (
    public.has_org_role(organization_id, 2)
    and (
      -- The OWNER membership is not removable through the client API.
      role_id <> (
        select r.id from public.organization_roles r where r.code = 'OWNER'
      )
    )
  );

-- members_select is deliberately unchanged: the dashboard, onboarding layout
-- and useOrg/useOrgRole hooks all resolve the signed-in user's membership
-- through it, and it already restricts to (own row OR co-member).

-- ---------------------------------------------------------------------
-- Verification (run manually; expected results inline).
--
--   select tablename, policyname, cmd, qual, with_check
--   from pg_policies
--   where schemaname='public'
--     and tablename in ('organizations','organization_members')
--   order by tablename, policyname;
--   -- expect: no org_insert row at all; org_update gated on
--   --         has_org_role(id, 2); members_* gated on
--   --         has_org_role(organization_id, 2).
--
--   -- Self-promotion must now fail (run as a signed-in VIEWER's JWT):
--   update public.organization_members set role_id =
--     (select id from public.organization_roles where code='OWNER')
--   where user_id = auth.uid();
--   -- expect: 0 rows updated (RLS filters it out) — not an escalation.
--
--   -- Removing the owner must now fail (run as a signed-in non-owner):
--   delete from public.organization_members where role_id =
--     (select id from public.organization_roles where code='OWNER');
--   -- expect: 0 rows deleted.
--
--   -- Org profile edit must now require owner/admin:
--   update public.organizations set base_currency_code='USD' where id = '...';
--   -- expect: 0 rows for a VIEWER; 1 row for an OWNER/ADMIN.
-- ---------------------------------------------------------------------