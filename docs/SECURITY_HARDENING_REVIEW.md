# Security Hardening Review — repository ↔ live Supabase synchronisation

Audit date: 2026-09-18. Repository branch: `security/erp-hardening`.
Live project inspected: `gghkbpdaqogncbrwzmpp` (read-only, via MCP).

This document exists because the repository migrations and the live database
are **not** one-to-one, and because several findings are fixed in code but
**not yet applied** to the database.

---

## 1. Migrations added by this work

**APPLIED to the live project `gghkbpdaqogncbrwzmpp` on 2026-09-18 with explicit
approval**, and verified afterwards by re-inspection:

| # | Repository file | Purpose | Verified |
|---|---|---|---|
| 071 | `database/migrations/071_harden_report_rpcs_and_anon_grants.sql` | Revokes `PUBLIC`/`anon` EXECUTE on report RPCs and adds a membership guard so `get_trial_balance`/`get_income_statement` cannot be called cross-tenant | Dry-run inside `BEGIN … ROLLBACK` on 2026-09-18; behaviour equivalence proven (original returned 15 rows, rewritten returned 15) and the guard proven to raise `42501` for a non-member |
| 072 | `database/migrations/072_harden_organization_and_member_rls.sql` | Denies direct `organizations` INSERT; requires OWNER/ADMIN for org profile updates and all membership writes; blocks self-promotion and owner removal | Dry-run inside `BEGIN … ROLLBACK`; resulting `pg_policies` inspected and correct |
| 073 | `database/migrations/073_worker_job_reliability.sql` | Attempts, max_attempts, backoff, cancellation and ownership-checked completion for AI worker jobs | Dry-run: all 3 functions compile. After applying, `ai.finish_worker_job(<job>, 'impostor-worker', …)` was rejected with `42501: job … is not held by worker impostor-worker` |
| 074 | `database/migrations/074_ai_session_reaper.sql` | Expires abandoned `WAITING_FOR_USER` sessions (session bookkeeping only — no financial data) | After applying, `ai.reap_stale_sessions()` returned `1`; `CANCELLED` 4→5 and `WAITING_FOR_USER` 5→4. ACL is `postgres` + `service_role` only |

### Post-application verification of the Critical finding

```
-- before: {=X/postgres, postgres=X/postgres, anon=X/postgres, authenticated=X/postgres, …}
select p.acl from pg_proc p where p.proname='get_trial_balance';
-- after:  {postgres=X/postgres, authenticated=X/postgres, service_role=X/postgres}

set role anon;
select * from public.get_trial_balance('00000000-…'::uuid);
-- ERROR: 42501: permission denied for function get_trial_balance

-- the backend/report pages are unaffected:
select count(*) from public.get_trial_balance('<org uuid>');  -- 15 rows
```

### 072 dry-run finding (worth recording)

The first draft of 072 **would have failed to apply**:

```
ERROR: 42883: function public.has_org_role(uuid, integer) does not exist
HINT:  No function matches the given name and argument types.
```

`has_org_role(target_org uuid, min_rank smallint)` takes `smallint`; an
uncast integer literal is not implicitly matched. The live policies show
`has_org_role(organization_id, (2)::smallint)` for exactly this reason. All
rank literals were changed to `2::smallint` / `1::smallint` and the dry-run
then succeeded. This is why every migration here is dry-run before being
proposed for application.

---

## 2. Repository ↔ live drift observed

Migration-history comparison (`list_migrations` vs `database/migrations/`) did
**not** resolve cleanly, so no object is assumed applied merely because a file
exists.

| Observation | Detail | Classification |
|---|---|---|
| Duplicate applied migration name | `070_reporting_view_hierarchy_rollup` appears **twice** in live history (`…160828` and `…160858`) | `UNKNOWN_MIGRATION_STATUS` |
| Name/version mapping | Live entries carry names that do not correspond to repository filenames, e.g. live `20260829093037 add_implemented_enum_value` vs repository `026_create_ai_control_plane_schema.sql`; live `20260829075531 create_ai_control_plane_schema` vs repository `026` | `UNKNOWN_MIGRATION_STATUS` |
| Repository files absent from the live ledger | `database/migrations/054_*`, `057_*`–`064_*` do not exist as files at all | `MISSING_IN_LIVE` cannot be asserted — the numbering schemes differ |
| Extra live objects | `public.audit_log`, `public.recurring_*`, `public.anomaly_*`, `public.cashflow_*`, `public.bank_reconciliation_*`, `public.financial_health_*`, `public.account_catalog*` exist live | Consistent with later features; no action |

**Guidance:** do not rerun historical migrations against the existing
database. Corrective changes go in **new forward migrations**, as 071–073 do.

### RPC signature drift

| Function | Live signature | Note |
|---|---|---|
| `create_organization` | 12 parameters (`p_name` … `p_fiscal_year_start_year`) | Matches the frontend RPC call |
| `has_org_role` | `(target_org uuid, min_rank smallint)` | **Must be called with a `smallint` literal** — see §1 |
| `claim_worker_job` | `(p_worker text, p_lease_seconds integer DEFAULT 180)` | Replaced by 073 |
| `account_template_for_business_type` | mutable `search_path` | Adviser lint `0011`; **still open** |

---

## 3. Confirmed findings fixed in code (branch only)

| Finding | Evidence | Fix |
|---|---|---|
| Development `X-User-Id` header auth enabled in every environment | `app/auth.py` had no environment gate | Requires `APP_ENV=development` **and** `ALLOW_DEV_HEADER_AUTH=true` (default false) |
| `get_trial_balance` / `get_income_statement` readable by `anon` for ANY organization | `SECURITY DEFINER`, `anon=X` grant, no `auth.uid()` check; proven at runtime with `set role anon` | Migration 071 |
| Worker executed mutations with `auth=None` | `scripts/ai_worker.py`; `app/tool_router.py` skipped permission **and** validation | Worker rebuilds a fresh `AuthContext`; mutations fail closed without one |
| Suspended/removed members still authenticated | `_resolve_membership` fell back to an unfiltered query | ACTIVE-only, no fallback |
| Self-promotion to OWNER via `organization_members` UPDATE | Live policy allowed `user_id = auth.uid()` | Migration 072 |
| Any member could change base currency / fiscal year end | Live `org_update` only required `is_org_member` | Migration 072 |
| Direct organization INSERT bypassing onboarding | Live `org_insert` was `WITH CHECK (true)` | Migration 072 |
| Any member could delete the OWNER membership | Live `members_delete` only required `is_org_member` | Migration 072 |
| Worker completion with no lease ownership | `ai_worker.py` updated by id only | Migration 073 + RPC |
| Stale permissions cache | process-lifetime cache; the worker never restarts | 60s TTL, poison-resistant |
| Invoice party extracted as `"selling"`; quantity re-asked; session crashed on `AgentResponse.options` | Live session `dc274515…`, recorded `ValidationError` | `app/planner.py`, `app/reasoning.py`, `app/agent.py` |

---

## 4. Residual risks — inspected, and what happened to each

Every residual risk from the previous report was re-inspected against the
code. Four were real and are now **FIXED**; three were reclassified after
measurement; two remain open by deliberate decision.

### 4a. Fixed in this pass

| Residual | What the inspection found | Fix |
|---|---|---|
| **Stale `WAITING_FOR_USER` sessions** | Four sessions (`9ac54f57`, `2b3943c4`, `dd7428cb`, `9a75ed04`) were still "waiting" after their runs ended; nothing expired them. | Migration 074: `expires_at`, a partial index and `ai.reap_stale_sessions()`. Verified: reaped 1, `WAITING_FOR_USER` 5→4. |
| **A cosmetic payload mismatch destroyed a whole session** | `AgentResponse.options` is `List[str]`; a producer shipped `{value,label}` dicts, so construction raised `ValidationError` and the run ended FAILED with a **raw Pydantic error shown to the user**. | Fixed the producer *and* added a `field_validator` backstop that flattens structured options to their label (and drops unrenderable ones) instead of failing. 5 tests. |
| **Unscoped read of `journal_lines`** | The repository's own audit flagged `get_journal_lines(entry_id)` as a latent hole. `journal_lines.organization_id` is `NOT NULL`, so it was cheaply fixable. | `get_journal_lines`/`accounting_service.get_lines` accept `organization_id` and filter on it. |
| **`update_one`/`delete_one` scoped by primary key only** | The service-role client bypasses RLS, so a bare id reaches any tenant's row. The read layer *is* org-scoped and every write I traced follows an org-scoped read, but that is a single layer of defence. | Both helpers now accept an optional `organization_id` second guard; applied to the payment-allocation writes (`invoices`, `purchase_bills`), which take a client-supplied id. |

### 4b. Reclassified after measurement (not defects)

| Residual | Finding |
|---|---|
| **Service-role tenant scoping (category 3)** | **Not a defect.** `update_one`/`delete_one` are by-id, but every repository *read* entry point takes `organization_id` as its first argument and filters on it (`get_invoice` filters on `id` **and** `organization_id`), and writes are reached only after such a read. `_update_invoice_paid` even returns early when the org-scoped getter finds nothing. Previously `POTENTIAL_REQUIRES_VERIFICATION`; now resolved with a defence-in-depth improvement rather than a vulnerability fix. |
| **`ai.tool_calls` "not written"** | Partly a **false positive**: the writer (`create_tool_call`, aliased `log_tool_call`) *is* wired into the executor. The failing invoice session recorded no tool calls because **no tool ever executed** — the crash happened during reasoning, before execution. The genuine residual is narrower: those writes are fire-and-forget (`_spawn_step_write`), so they can be lost if the invocation is torn down mid-flight. |
| **Vercel `APP_ENV`** | **Not a defect**: the API shows it is already `production`. My earlier report assumed otherwise; that was an assumption, now corrected. |

### 4c. Still open (deliberate)

| Gap | Evidence | Why still open |
|---|---|---|
| **No idempotency keys for financial mutations** | The only replay guard is `app/tool_router.py`, scoped to `slug == "record_cash_sale"` within one execution session and comparing `amount`. No financial table has an `idempotency_key` column. | Needs new schema plus every mutation path, and the retry surfaces are browser / HTTP / AI provider / worker. Two legitimately identical cash sales in one session could still be suppressed, i.e. a **missing financial record**. Half-building this would be worse than leaving it clearly recorded. |
| **No transaction atomicity for multi-write workflows** | Repository's own `docs/CODEBASE_INTEGRATION_AUDIT.md:23`: invoice → items → journal are separate REST writes; no RPC wraps them. | Needs one PostgreSQL function per workflow. Large, and touching financial posting logic without staging is how you create the very corruption this is meant to prevent. |
| **`ai.worker_jobs` has RLS enabled with no policy** | Adviser lint `0008` | Intentional (backend-only; service role bypasses RLS). |
| **`account_template_for_business_type` has a mutable `search_path`** | Adviser lint `0011` | Low risk (not SECURITY DEFINER) but should be pinned. |
| **Leaked-password protection disabled** | Adviser `auth_leaked_password_protection` | Supabase Auth portal setting, not code. |

---

## 5. Required manual actions

1. **Rotate the GitHub PAT — STILL OUTSTANDING.** The PAT in
   `.secrets/tokens.env` is still rejected by GitHub
   (`Invalid username or token`), so the branch could not be pushed and no
   PR or Vercel preview could be produced. Only you can rotate it.
2. **Migration application — DONE** (071, 072, 073, 074 applied to the live
   project on 2026-09-18 with approval, then verified).
3. **Supabase project role — confirmed production by the operator**;
   `gghkbpdaqogncbrwzmpp` is where the migrations were applied.
4. **`APP_ENV` — NO ACTION NEEDED.** Verified through the Vercel API that
   `APP_ENV` is already `production` (targets: production, preview,
   development). An earlier draft of this document said otherwise; that was
   an assumption, not a measurement. Adding `ALLOW_DEV_HEADER_AUTH=false`
   explicitly is optional belt-and-braces — the code default is already
   false and the flag is ignored outside development.
5. **Enable leaked-password protection** in Supabase Auth settings.
6. **Deploy the code fixes** (Vercel) so the application-side hardening takes
   effect. The database fixes are already live; the code fixes are not.
7. **Decide the tests policy.** `.gitignore` no longer excludes `app/tests`,
   `tests` and `frontend/tests`; CI (`.github/workflows/ci.yml`) runs them.

---

## 6. Verification performed

| Check | Result |
|---|---|
| Full backend suite | **770 passed, 0 failed** (`python -m pytest app/tests -q`) |
| Baseline before this work | 733 passed |
| Migrations 071–074 | Dry-run inside `BEGIN … ROLLBACK` first, then applied and re-inspected |
| `anon` cross-tenant read | **Closed**: `set role anon` now returns `42501: permission denied for function get_trial_balance` |
| Backend report path | Unaffected: `get_trial_balance('<org>')` still returns its 15 rows |
| Membership escalation | `org_update` requires `has_org_role(id, (2)::smallint)`; `members_update` requires owner/admin and only an OWNER may grant OWNER |
| Worker lease ownership | `finish_worker_job(<job>, 'impostor-worker', …)` → `42501: job … is not held by worker impostor-worker` |
| Stale sessions | `ai.reap_stale_sessions()` reaped 1; `WAITING_FOR_USER` 5→4, `CANCELLED` 4→5 |
| Financial data | **Not touched** by any migration: no invoice/journal/payment rows created, altered or deleted |