# Security Hardening Review — repository ↔ live Supabase synchronisation

Audit date: 2026-09-18. Repository branch: `security/erp-hardening`.
Live project inspected: `gghkbpdaqogncbrwzmpp` (read-only, via MCP).

This document exists because the repository migrations and the live database
are **not** one-to-one, and because several findings are fixed in code but
**not yet applied** to the database.

---

## 1. Migrations added by this work (NOT YET APPLIED)

None of these is applied to any environment yet. Applying to production
requires explicit approval.

| # | Repository file | Purpose | Verified |
|---|---|---|---|
| 071 | `database/migrations/071_harden_report_rpcs_and_anon_grants.sql` | Revokes `PUBLIC`/`anon` EXECUTE on report RPCs and adds a membership guard so `get_trial_balance`/`get_income_statement` cannot be called cross-tenant | Dry-run inside `BEGIN … ROLLBACK` on 2026-09-18; behaviour equivalence proven (original returned 15 rows, rewritten returned 15) and the guard proven to raise `42501` for a non-member |
| 072 | `database/migrations/072_harden_organization_and_member_rls.sql` | Denies direct `organizations` INSERT; requires OWNER/ADMIN for org profile updates and all membership writes; blocks self-promotion and owner removal | Dry-run inside `BEGIN … ROLLBACK`; resulting `pg_policies` inspected and correct |
| 073 | `database/migrations/073_worker_job_reliability.sql` | Attempts, max_attempts, backoff, cancellation and ownership-checked completion for AI worker jobs | Dry-run: `claim_worker_job`, `finish_worker_job`, `cancel_worker_job` all compile |

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

## 4. Known remaining gaps (NOT fixed — recorded honestly)

| Gap | Evidence | Why not fixed here |
|---|---|---|
| **No idempotency keys for financial mutations** | The only replay guard is in `app/tool_router.py`, scoped to `slug == "record_cash_sale"` within one execution session and comparing `amount`. No financial table has an `idempotency_key` column. | Requires new schema plus every mutation path; a partial implementation would be worse than none. Two legitimately identical cash sales in one session could still be suppressed, i.e. a **missing financial record**. |
| **No transaction atomicity for multi-write workflows** | Repository's own `docs/CODEBASE_INTEGRATION_AUDIT.md:23`: invoice → items → journal are separate REST writes; no RPC wraps them. | Needs per-workflow PostgreSQL functions; too large to land safely in this pass. |
| **`ai.tool_calls` not written on the failing path** | The `FAILED` invoice session recorded **0** tool calls and 0 `error_details`, so the failure was invisible at call level. | Observability gap; the writer must be reached from the executor. |
| **`ai.worker_jobs` has RLS enabled with no policy** | Adviser lint `0008` | Intentional (backend-only; service role bypasses RLS). Migration 073 now carries explicit grants instead. |
| **`account_template_for_business_type` has a mutable `search_path`** | Adviser lint `0011` | Not yet changed; low risk (not SECURITY DEFINER) but should be pinned. |
| **Session status can go stale** | Sessions `9ac54f57`, `2b3943c4`, `dd7428cb`, `9a75ed04` remain `WAITING_FOR_USER` after their runs ended. | Needs a reaper; `expires_at` column plus a sweep. |
| **Broad `except Exception` at the executor boundary** | `app/tool_execution.py` turns any exception into `{"success": False}`, and an internal Pydantic `ValidationError` reached the user as `"Error: 2 validation errors for AgentResponse…"`. | Error classification is cross-cutting; the specific defect that produced it is fixed. |
| **Leaked-password protection disabled** | Adviser `auth_leaked_password_protection` | Supabase Auth setting, not code — see §5. |

---

## 5. Required manual actions

1. **Rotate the GitHub PAT.** The PAT in `.secrets/tokens.env` was rejected by
   GitHub (`Invalid username or token`), so the hardening branch could not be
   pushed and no Vercel preview exists.
2. **Approve migration application.** 071 closes a Critical unauth cross-tenant
   read; 072 closes self-promotion to OWNER. Both are additive and
   non-destructive.
3. **Confirm the Supabase project's role.** `gghkbpdaqogncbrwzmpp` was treated
   as production throughout; nothing was written to it.
4. **Set `APP_ENV=production`** in the Vercel environment and leave
   `ALLOW_DEV_HEADER_AUTH` unset/false.
5. **Enable leaked-password protection** in Supabase Auth settings.
6. **Decide the tests policy.** `.gitignore` no longer excludes `app/tests`,
   `tests` and `frontend/tests`; CI (`.github/workflows/ci.yml`) runs them.

---

## 6. Verification performed

| Check | Result |
|---|---|
| Full backend suite | **766 passed, 0 failed** (`python -m pytest app/tests -q`) |
| Baseline before this work | 733 passed |
| Migrations 071/072/073 | Dry-run inside `BEGIN … ROLLBACK`; production left unchanged (`assert_org_report_access` absent from `pg_proc` afterwards) |
| Production side effects | None: 0 tests, 0 invoices created; the failing invoice session's writes never reached the database |