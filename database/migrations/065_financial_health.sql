-- Migration: financial_health_snapshots

create table if not exists public.financial_health_snapshots (
  id              uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations(id) on delete cascade,
  snapshot_date   date not null default current_date,
  overall_score   smallint not null check (overall_score >= 0 and overall_score <= 100),
  liquidity_score smallint not null check (liquidity_score >= 0 and liquidity_score <= 100),
  profitability_score smallint not null check (profitability_score >= 0 and profitability_score <= 100),
  efficiency_score smallint not null check (efficiency_score >= 0 and efficiency_score <= 100),
  metrics         jsonb not null default '{}',
  alerts          jsonb not null default '[]',
  created_at      timestamptz not null default now()
);

create index if not exists idx_health_date on public.financial_health_snapshots(organization_id, snapshot_date desc);

alter table public.financial_health_snapshots enable row level security;

drop policy if exists "org_members_view_health" on public.financial_health_snapshots;
create policy "org_members_view_health" on public.financial_health_snapshots for select to authenticated
  using (organization_id in (select organization_id from public.organization_members where user_id = auth.uid() and status = 'ACTIVE'));
