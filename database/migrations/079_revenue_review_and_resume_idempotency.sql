-- ============================================================================
-- 079: Revenue-ledger review + resume idempotency hardening
-- ============================================================================
-- Forward-only.  Idempotent (IF NOT EXISTS / guarded DO blocks) so it can be
-- applied to a database already patched by the API migration runner without
-- failing.
--
-- 1. Partial unique index on ai.clarifications: at most ONE pending
--    clarification per session.  Two concurrent ask paths can no longer
--    create two unanswered rows for the same session (which would make
--    resolve_clarification answer an arbitrary one of them).  The partial
--    form keeps answered sessions free to start a new question.
--
-- 2. ai.confirmations.plan: the APPROVED execution plan snapshot (tools,
--    arguments, entities — including the resolved revenue_account_id).
--    resume_with_confirmation stores the plan at confirm time; a resumed run
--    rebuilds a plan EQUIVALENT to what the user saw — it cannot silently
--    select different tools or lose the approved account id.
--    (The column adds no secret exposure risk: it holds the same tool
--    arguments already persisted in ai.tool_calls.input_payload.)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. One pending clarification per session
-- ---------------------------------------------------------------------------
create unique index if not exists ai_clarifications_one_pending_per_session_idx
    on ai.clarifications (execution_session_id)
    where status = 'WAITING_FOR_USER';

-- ---------------------------------------------------------------------------
-- 2. Approved-plan snapshot for confirmations
-- ---------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'ai'
          and table_name = 'confirmations'
          and column_name = 'plan'
    ) then
        alter table ai.confirmations
            add column plan jsonb;
        comment on column ai.confirmations.plan is
            'Approved execution plan snapshot (tools, arguments, entities incl. resolved revenue_account_id) recorded at confirm time so the resumed run cannot replan differently.';
    end if;
end
$$;
