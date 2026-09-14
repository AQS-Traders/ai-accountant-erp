-- Migration: recurring_templates + recurring_executions

create table if not exists public.recurring_templates (
  id              uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations(id) on delete cascade,
  name            text not null,
  description     text,
  frequency       text not null default 'monthly'
                    check (frequency in ('weekly','biweekly','monthly','quarterly','semi-annual','yearly')),
  schedule_day    smallint,
  next_due_date   date not null,
  auto_post       boolean not null default false,
  reminder_days   smallint not null default 3,
  journal_template jsonb not null,
  source_type     text not null default 'recurring',
  status          text not null default 'active'
                    check (status in ('active','paused','completed','cancelled')),
  last_generated_date date,
  total_generated     integer not null default 0,
  created_by      uuid references auth.users(id),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists idx_recurring_org on public.recurring_templates(organization_id);
create index if not exists idx_recurring_next_due on public.recurring_templates(next_due_date) where status = 'active';

create table if not exists public.recurring_executions (
  id              uuid primary key default gen_random_uuid(),
  template_id     uuid not null references recurring_templates(id) on delete cascade,
  organization_id uuid not null references organizations(id) on delete cascade,
  scheduled_date  date not null,
  executed_at     timestamptz,
  status          text not null default 'pending'
                    check (status in ('pending','generated','skipped','failed')),
  journal_entry_id uuid references journal_entries(id),
  error_message   text,
  created_at      timestamptz not null default now()
);

create index if not exists idx_recurring_exec_template on public.recurring_executions(template_id);
create index if not exists idx_recurring_exec_date on public.recurring_executions(scheduled_date, status);

alter table public.recurring_templates enable row level security;
alter table public.recurring_executions enable row level security;

create policy "org_members_view_recurring" on public.recurring_templates for select to authenticated
  using (organization_id in (select organization_id from public.organization_members where user_id = auth.uid() and status = 'ACTIVE'));
create policy "org_members_modify_recurring" on public.recurring_templates for all to authenticated
  using (organization_id in (select organization_id from public.organization_members where user_id = auth.uid() and status = 'ACTIVE'));
create policy "org_members_view_recurring_exec" on public.recurring_executions for select to authenticated
  using (organization_id in (select organization_id from public.organization_members where user_id = auth.uid() and status = 'ACTIVE'));
