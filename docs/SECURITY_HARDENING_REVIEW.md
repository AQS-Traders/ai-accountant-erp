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
code. Six were real and are now **FIXED**; three were reclassified after
measurement; three remain open by deliberate decision.

### 4a. Fixed in this pass

| Residual | What the inspection found | Fix |
|---|---|---|
| **Stale `WAITING_FOR_USER` sessions** | Four sessions (`9ac54f57`, `2b3943c4`, `dd7428cb`, `9a75ed04`) were still "waiting" after their runs ended; nothing expired them. | Migration 074: `expires_at`, a partial index and `ai.reap_stale_sessions()`. Verified: reaped 1, `WAITING_FOR_USER` 5→4. |
| **A cosmetic payload mismatch destroyed a whole session** | `AgentResponse.options` is `List[str]`; a producer shipped `{value,label}` dicts, so construction raised `ValidationError` and the run ended FAILED with a **raw Pydantic error shown to the user**. | Fixed the producer *and* added a `field_validator` backstop that flattens structured options to their label (and drops unrenderable ones) instead of failing. 5 tests. |
| **Unscoped read of `journal_lines`** | The repository's own audit flagged `get_journal_lines(entry_id)` as a latent hole. `journal_lines.organization_id` is `NOT NULL`, so it was cheaply fixable. | `get_journal_lines`/`accounting_service.get_lines` accept `organization_id` and filter on it. |
| **`update_one`/`delete_one` scoped by primary key only** | The service-role client bypasses RLS, so a bare id reaches any tenant's row. The read layer *is* org-scoped and every write I traced follows an org-scoped read, but that is a single layer of defence. | Both helpers now accept an optional `organization_id` second guard; applied to the payment-allocation writes (`invoices`, `purchase_bills`), which take a client-supplied id. |
| **No idempotency keys for financial mutations** | The only replay guard was a `record_cash_sale`-only, amount-based in-session check. Retries at any surface (browser / HTTP / AI provider / worker) could double-post, and amount-based suppression could wrongly drop a legitimate second identical sale. | Migration 075: `financial_operations` table keyed on `(organization_id, operation, idempotency_key)` with `claim_financial_operation` (request-hash mismatch rejection, result replay, stale-claim reclaim, FAILED retry) and `complete_financial_operation`; wired through `app/idempotency.py` at the tool-router choke point so every mutation path is protected by request keys. Both RPCs `service_role`-only. |
| **No transaction atomicity for multi-write workflows** | Invoice → items → journal (and receipt/payment/bill → journal → linkage) were separate REST writes; a mid-sequence failure left a document without its lines or a document with no journal — the receipt/payment flows even swallowed journal failure into a warning. | Migrations 076/077/078: `post_invoice_atomic`, `create_payment_atomic`, `create_purchase_bill_atomic`, `create_receipt_atomic` — each creates the document, its lines and its journal in ONE transaction with tenant-ownership checks and balanced-journal rejection. Wired into `invoice_service`, `payment_service` and `purchase_service` (PR #3, CI green, 787 tests). All RPCs `service_role`-only. Journals are created DRAFT and still go through the app's validate/post flow. Residual: bills/payments/receipts *allocation + settlement* writes (allocate-to-invoice, `amount_paid` update) remain outside the RPC and are protected by idempotency (075) and org-scoped update guards, not full atomicity. |

### 4b. Reclassified after measurement (not defects)

| Residual | Finding |
|---|---|
| **Service-role tenant scoping (category 3)** | **Not a defect.** `update_one`/`delete_one` are by-id, but every repository *read* entry point takes `organization_id` as its first argument and filters on it (`get_invoice` filters on `id` **and** `organization_id`), and writes are reached only after such a read. `_update_invoice_paid` even returns early when the org-scoped getter finds nothing. Previously `POTENTIAL_REQUIRES_VERIFICATION`; now resolved with a defence-in-depth improvement rather than a vulnerability fix. |
| **`ai.tool_calls` "not written"** | Partly a **false positive**: the writer (`create_tool_call`, aliased `log_tool_call`) *is* wired into the executor. The failing invoice session recorded no tool calls because **no tool ever executed** — the crash happened during reasoning, before execution. The genuine residual is narrower: those writes are fire-and-forget (`_spawn_step_write`), so they can be lost if the invocation is torn down mid-flight. |
| **Vercel `APP_ENV`** | **Not a defect**: the API shows it is already `production`. My earlier report assumed otherwise; that was an assumption, now corrected. |

### 4c. Still open (deliberate)

| Gap | Evidence | Why still open |
|---|---|---|
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

---

## 7. Deployment and credential findings (2026-09-18)

### The GitHub push failure was NOT an expired token

An earlier report concluded the PAT was expired. **That was wrong.** Diagnosis:

| Check | Result |
|---|---|
| `GET https://api.github.com/user` | **200 OK**, `login=zameerchattha0-ops` |
| Repo permissions | `push_permission=true`, `admin=true` |
| `x-oauth-scopes` header | empty → a **fine-grained** PAT (fine-grained tokens send no scopes header) |
| Keys in `.secrets/tokens.env` | `GITHUB_PAT` set (len 93); `GITHUB_USER` present but **blank** |

The real cause: a **fine-grained** PAT must be paired with the account's real
GitHub username as the HTTPS username. `x-access-token` — which is correct for
classic PATs and App installation tokens, and is a common piece of advice for
this exact error — is **rejected** for fine-grained PATs with
`Invalid username or token. Password authentication is not supported for Git
operations.` The username is now derived from the API rather than hardcoded.

Two secondary traps were also hit and are worth recording:

* `git config --local credential.helper <ours>` **appends** to the helper list
  rather than replacing it, so Git Credential Manager still ran and blocked the
  push on an interactive browser prompt. `-c credential.helper=` (empty) must be
  passed first to reset the list.
* Therefore `credential.helper` alone is not safe to script on this machine.

### Token permissions actually granted vs needed

| Need | Status |
|---|---|
| Push commits to `security/erp-hardening` | ✅ **works** |
| `workflow` scope (create/update `.github/workflows/*`) | ❌ missing → `refusing to allow a Personal Access Token to create or update workflow .github/workflows/ci.yml without workflow scope` |
| Create a pull request | ❌ missing → `403 Resource not accessible by personal access token` |

`.github/workflows/ci.yml` is therefore **held back** and not committed; the file
remains on disk and the pre-strip history is preserved on the local branch
`backup/full-with-ci` (`e2b3e86`). With no CI file, every other change pushes
cleanly.

### Vercel

* `APP_ENV` is already `production` (targets production, preview, development).
* A preview was built for the branch: `ai-accountant-m706ztq1g-zameerchattha0-ops.vercel.app` (state READY).
* **`/api/*` routing differs between production and preview.** Production
  `https://ai-accountant-erp.vercel.app/api/health` returns **JSON**
  (`{"status":"ok","version":"1.0.0","config_ok":true}`), but the same path on
  the preview host returns **Next.js HTML**. So an API smoke test against a
  preview URL does not exercise the FastAPI backend and must not be read as
  proof about backend behaviour. (Initial smoke results were misinterpreted this
  way and corrected.)

### Live proof that the header-auth bypass is real

Executed against **production** using a UUID belonging to no one (no
impersonation attempted — the identity is a random UUID that exists nowhere):

| Request | Response | Reading |
|---|---|---|
| no credentials | `401` | the genuine "unauthenticated" response |
| `X-User-Id` only, non-member UUID | **`403`** | the header was **accepted as an identity** and processed to the membership lookup |
| `X-User-Id` + forged `X-Organization-Id` | **`403`** | org header read too |
| bogus bearer | `401` | correctly rejected |

A header-only request is treated as *authenticated* (403 "not a member") rather
than *unauthenticated* (401). With a real member's UUID — trivially obtainable
from `organization_members` through the client, which is readable by
co-members — the old code grants that user's full access.

### DEPLOYED AND VERIFIED IN PRODUCTION (2026-09-18)

`main` was fast-forwarded `b728876..e8f5583` and pushed; Vercel deployed
production (`ai-accountant-avcyhccxg-…vercel.app`, READY, commit `e8f5583`,
~3 minutes after push). Post-deploy verification against **production**,
same no-impersonation method as above:

| Request (against `ai-accountant-erp.vercel.app`) | Before deploy | After deploy |
|---|---|---|
| `X-User-Id` only, non-member UUID | `403` (header accepted as identity) | **`401`** ✅ |
| `X-User-Id` + forged `X-Organization-Id` | `403` | **`401`** ✅ |
| no credentials | `401` | `401` (unchanged, correct) |
| bogus bearer | `401` | `401` (unchanged, correct) |
| `GET /api/health` | `200` JSON | `200` JSON `{"status":"ok","config_ok":true}` (no regression) |

The header-auth bypass (audit category 1, Critical) is **closed in production**.
Database hardening (071–074, applied earlier) plus the deployed app fixes close
categories 1, 2, 4, 5, 6, 7, 12 and the anon cross-tenant report read. Migrations
075 (idempotency keys), 076 (atomic invoice posting), 077 (atomic supplier
payments + purchase bills) and 078 (atomic customer receipts) are applied to the
live database. 076 was delivered with the main hardening merge (PR #1); 077/078
and the service wiring for all four atomic RPCs were delivered via PR #3
(`feat/atomic-payments`, CI green on the PR and on `main`, 787 tests passing).

Cosmetic drift note: migrations 070, 075, 076 and 078 each appear **twice** in the
live migration history (file-prefixed name + apply-tool name). All four are
idempotent (`if not exists` / `create or replace`), live schema matches the
repository, and the duplication is bookkeeping-only — no corrective action
required.