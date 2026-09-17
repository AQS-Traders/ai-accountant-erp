-- =====================================================================
-- Migration 070: hierarchy-aware reporting views
-- =====================================================================
-- Migration 067 introduced the parent/child chart of accounts, and a parent
-- ("grouping") account is already protected from receiving postings -- see
-- account_repository.get_grouping_account_ids() and accounting_engine's
-- _resolve_default_account().  The reporting views, however, still grouped by
-- account ALONE, so a parent such as
--
--     1500  Property, Plant & Equipment
--      +-- 1520 Computer Equipment
--      +-- 1525 Accumulated Depreciation - Computer Equipment
--      +-- 1550 Machinery & Plant                      ... etc.
--
-- was listed with only its OWN postings (normally zero) while the real values
-- sat under its children, with nothing in the report connecting the two.
--
-- This migration closes the chain required by the enhancement brief:
--
--   Account -> Parent Account -> Account Type
--           -> Statement classification -> Financial reporting
--
-- DESIGN CONSTRAINTS (deliberately conservative)
-- ----------------------------------------------
-- 1. PURELY ADDITIVE.  Every pre-existing column keeps its exact name, type,
--    order and meaning, so CREATE OR REPLACE succeeds and every current
--    consumer (app/repositories/report_repository.py, the reporting tools in
--    app/tools/__init__.py, the front end) keeps working untouched.
-- 2. `balance` / `net_amount` remain the account's OWN postings.  A consumer
--    that sums those still gets the correct total, exactly as before.  The
--    roll-up is exposed separately as `subtree_*` together with `is_group`,
--    so a consumer can opt in without risking double counting.
-- 3. `security_invoker = true` is preserved on every view.  These views rely
--    on row level security of the underlying tables; recreating them without
--    the option would make them run as the view owner and silently bypass RLS.
-- 4. No existing row is added or removed from v_trial_balance (it keeps its
--    INNER JOIN, so it still lists accounts with posted activity only).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Helper: every account -> all of its descendants (self included).
-- The organization guard on the recursive join is what stops a link from
-- ever crossing between organizations.
-- ---------------------------------------------------------------------
create or replace view public.v_account_hierarchy
with (security_invoker = true) as
with recursive tree as (
  select a.organization_id,
         a.id as account_id,
         a.id as descendant_id,
         0 as depth
  from public.accounts a
  union all
  select t.organization_id,
         t.account_id,
         c.id,
         t.depth + 1
  from tree t
  join public.accounts c
    on c.parent_account_id = t.descendant_id
   and c.organization_id = t.organization_id
  where t.depth < 32          -- cycle guard
)
select organization_id, account_id, descendant_id, depth
from tree;

-- ---------------------------------------------------------------------
-- Helper: depth of each account counting from its root (0 = root).
-- Used purely for presentation (indentation) in reports.
-- ---------------------------------------------------------------------
create or replace view public.v_account_depth
with (security_invoker = true) as
with recursive walk as (
  select a.organization_id, a.id as account_id, 0 as depth
  from public.accounts a
  where a.parent_account_id is null
  union all
  select c.organization_id, c.id, w.depth + 1
  from walk w
  join public.accounts c
    on c.parent_account_id = w.account_id
   and c.organization_id = w.organization_id
  where w.depth < 32          -- cycle guard
)
select organization_id, account_id, depth
from walk;

-- ---------------------------------------------------------------------
-- Helper: each account's OWN posted totals (accounts without postings
-- still appear, with zeros, so roll-ups below are complete).
-- ---------------------------------------------------------------------
create or replace view public.v_account_own_balance
with (security_invoker = true) as
select a.organization_id,
       a.id as account_id,
       coalesce(sum(gl.debit), 0)  as total_debit,
       coalesce(sum(gl.credit), 0) as total_credit,
       coalesce(sum(gl.debit), 0) - coalesce(sum(gl.credit), 0) as balance
from public.accounts a
left join public.v_general_ledger gl on gl.account_id = a.id
group by a.organization_id, a.id;

-- ---------------------------------------------------------------------
-- Helper: rolled-up totals for every account over its whole subtree.
-- For a LEAF account the subtree is itself, so subtree_* equals its own
-- figures.  For a GROUP account the subtree is itself plus all descendants.
-- ---------------------------------------------------------------------
create or replace view public.v_account_subtree_balance
with (security_invoker = true) as
select h.organization_id,
       h.account_id,
       sum(b.total_debit)  as subtree_total_debit,
       sum(b.total_credit) as subtree_total_credit,
       sum(b.balance)      as subtree_balance
from public.v_account_hierarchy h
join public.v_account_own_balance b on b.account_id = h.descendant_id
group by h.organization_id, h.account_id;

-- =====================================================================
-- v_trial_balance -- same rows as before, plus hierarchy columns.
-- The original 9 columns keep their exact names, types and order.
-- =====================================================================
create or replace view public.v_trial_balance
with (security_invoker = true) as
with own as (
  select a.organization_id,
         a.id as account_id,
         a.code as account_code,
         a.name as account_name,
         a.account_type,
         a.normal_balance,
         coalesce(sum(gl.debit), 0)  as total_debit,
         coalesce(sum(gl.credit), 0) as total_credit,
         coalesce(sum(gl.debit), 0) - coalesce(sum(gl.credit), 0) as balance
  from public.accounts a
  join public.v_general_ledger gl on gl.account_id = a.id
  group by a.organization_id, a.id, a.code, a.name, a.account_type, a.normal_balance
)
select o.organization_id,
       o.account_id,
       o.account_code,
       o.account_name,
       o.account_type,
       o.normal_balance,
       o.total_debit,
       o.total_credit,
       o.balance,
       a.parent_account_id,
       p.code as parent_account_code,
       p.name as parent_account_name,
       coalesce(d.depth, 0) as depth,
       (coalesce(kid.n, 0) > 0) as is_group,
       coalesce(s.subtree_total_debit, o.total_debit)   as subtree_total_debit,
       coalesce(s.subtree_total_credit, o.total_credit) as subtree_total_credit,
       coalesce(s.subtree_balance, o.balance)           as subtree_balance
from own o
join public.accounts a on a.id = o.account_id
left join public.accounts p on p.id = a.parent_account_id
left join public.v_account_depth d on d.account_id = o.account_id
left join public.v_account_subtree_balance s on s.account_id = o.account_id
left join (
  select parent_account_id, count(*) as n
  from public.accounts
  where parent_account_id is not null and is_active = true
  group by parent_account_id
) kid on kid.parent_account_id = o.account_id;

-- =====================================================================
-- v_balance_sheet -- ASSET / LIABILITY / EQUITY, plus hierarchy columns.
-- =====================================================================
create or replace view public.v_balance_sheet
with (security_invoker = true) as
with own as (
  select a.organization_id,
         a.id as account_id,
         a.account_type,
         a.code as account_code,
         a.name as account_name,
         coalesce(sum(gl.debit - gl.credit), 0) as net_amount
  from public.accounts a
  left join public.v_general_ledger gl on gl.account_id = a.id
  where a.account_type in ('ASSET', 'LIABILITY', 'EQUITY')
    and a.is_active = true
  group by a.organization_id, a.id, a.account_type, a.code, a.name
)
select o.organization_id,
       o.account_type,
       o.account_code,
       o.account_name,
       o.net_amount,
       o.account_id,
       a.parent_account_id,
       p.code as parent_account_code,
       p.name as parent_account_name,
       coalesce(d.depth, 0) as depth,
       (coalesce(kid.n, 0) > 0) as is_group,
       coalesce(s.subtree_balance, o.net_amount) as subtree_balance
from own o
join public.accounts a on a.id = o.account_id
left join public.accounts p on p.id = a.parent_account_id
left join public.v_account_depth d on d.account_id = o.account_id
left join public.v_account_subtree_balance s on s.account_id = o.account_id
left join (
  select parent_account_id, count(*) as n
  from public.accounts
  where parent_account_id is not null and is_active = true
  group by parent_account_id
) kid on kid.parent_account_id = o.account_id;

-- =====================================================================
-- v_income_statement -- REVENUE / EXPENSE, plus hierarchy columns.
-- =====================================================================
create or replace view public.v_income_statement
with (security_invoker = true) as
with own as (
  select a.organization_id,
         a.id as account_id,
         a.account_type,
         a.code as account_code,
         a.name as account_name,
         coalesce(sum(gl.debit - gl.credit), 0) as net_amount
  from public.accounts a
  left join public.v_general_ledger gl on gl.account_id = a.id
  where a.account_type in ('REVENUE', 'EXPENSE')
    and a.is_active = true
  group by a.organization_id, a.id, a.account_type, a.code, a.name
)
select o.organization_id,
       o.account_type,
       o.account_code,
       o.account_name,
       o.net_amount,
       o.account_id,
       a.parent_account_id,
       p.code as parent_account_code,
       p.name as parent_account_name,
       coalesce(d.depth, 0) as depth,
       (coalesce(kid.n, 0) > 0) as is_group,
       coalesce(s.subtree_balance, o.net_amount) as subtree_balance
from own o
join public.accounts a on a.id = o.account_id
left join public.accounts p on p.id = a.parent_account_id
left join public.v_account_depth d on d.account_id = o.account_id
left join public.v_account_subtree_balance s on s.account_id = o.account_id
left join (
  select parent_account_id, count(*) as n
  from public.accounts
  where parent_account_id is not null and is_active = true
  group by parent_account_id
) kid on kid.parent_account_id = o.account_id;

-- =====================================================================
-- Grants for the four NEW helper views.  They carry security_invoker = true,
-- so RLS on `accounts` / `v_general_ledger` still applies to the caller.
-- anon is deliberately NOT granted -- reporting requires a session.
-- =====================================================================
grant select on public.v_account_hierarchy       to authenticated, service_role;
grant select on public.v_account_depth           to authenticated, service_role;
grant select on public.v_account_own_balance     to authenticated, service_role;
grant select on public.v_account_subtree_balance to authenticated, service_role;

revoke all on public.v_account_hierarchy       from anon;
revoke all on public.v_account_depth           from anon;
revoke all on public.v_account_own_balance     from anon;
revoke all on public.v_account_subtree_balance from anon;

-- =====================================================================
-- CONSUMPTION NOTES (important for report renderers)
-- ---------------------------------------------------------------------
-- balance / net_amount = the account's OWN postings   (meaning UNCHANGED)
-- subtree_balance      = own + every descendant's own postings
-- is_group             = this account is the parent of an active account
-- depth                = distance from the root account (0 = root)
--
-- A renderer that displays a hierarchy should print a group row using
-- subtree_balance and must NOT also add its children's rows on top, or the
-- figure is counted twice.  A renderer that keeps summing balance /
-- net_amount across all rows is unaffected and remains correct.
--
-- v_cash_flow is intentionally NOT changed: it is transaction-level and
-- classified by activity, so parent/child roll-up does not apply.
-- =====================================================================
