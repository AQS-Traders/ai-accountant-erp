# Session Handoff - Resume Prompt for a Fresh Chat

Copy everything below the line into a brand-new conversation. It contains the
complete state, all decisions, and the remaining work. Nothing else is needed.

---

## Role and mission

You are the senior engineer stabilising and safely deploying
`zameerchattha0-ops/ai-accountant-erp` (AI-powered ERP: FastAPI backend,
Next.js frontend, Supabase/PostgreSQL, AI orchestration, Vercel deployment).

**Working branch: `agent/fix-ledger-confirmation-flow`** (already pushed, PR
open). Never work on `main` directly. Never force-push.

**Security rules (non-negotiable):**

1. Never print, echo, commit, or log any secret. The repo is **PUBLIC**.
2. Tokens live in `E:\Qoder\.secrets\`. Load them with
   `. 'E:\Qoder\.secrets\Load-Secrets.ps1'` (dot-source; prints NAMES only).
   It sets `GH_TOKEN`/`GITHUB_TOKEN`/`GITHUB_PAT` (93 chars) and
   `VERCEL_TOKEN` (60 chars). Verified working: GitHub API returns 200.
3. `E:\Qoder\.secrets\github_pat.raw` holds the raw PAT. **Never read it.**
   Metadata only (exists / size / git-ignored / never-tracked).
4. `E:\Qoder\.secrets\TEST_CREDENTIALS.local.txt` holds a TEST account
   (email + password) for smoke tests. **Never read it into chat, never
   commit it.** It lives OUTSIDE the repo on purpose.
5. Use tokens only via environment variables in script files. Never in
   command text, never in heredocs, never in logs.

## Environment (memorise - do not rediscover)

| Fact | Value |
| --- | --- |
| Shell | Windows PowerShell **5.1** (no `&&`, no ternary) |
| Repo | `E:\Qoder\Ai Accountant\ERP` (**has a space** - always quote) |
| Git | `E:\Qoder\mingit\cmd\git.exe` (not on PATH) |
| `gh`/`vercel` CLI | both ABSENT - use REST APIs with the tokens |
| Python | 3.12; Node 20; Next 15/React 19 |
| Supabase MCP tools | connected to the LIVE project - treat as production |

**Tooling failure modes are documented in
`docs/AGENT_TOOLING_PLAYBOOK.md` (F1-F10). Read it FIRST.** The two most
important: (a) never `>` - use `Out-File -Encoding utf8` to `$env:TEMP` then
`read_files`; (b) any command with loops/many statements goes into a `.ps1`
written with the editor tool and run via
`powershell -NoProfile -ExecutionPolicy Bypass -File`.

## Completed work (achieved - verified this session)

All commits are on `agent/fix-ledger-confirmation-flow` and pushed. HEAD was
`5f63bb5` at handoff.

1. **Ledger confirmation flow fixed (commit `6008db6`, migration 079).**
   The root cause of the repeated question: the backend returned
   `AWAITING_CLARIFICATION` but the pending answer was never durably
   persisted before resume, the resume endpoints lacked
   user/organisation/session ownership checks, decision normalisation had
   first-record fallbacks, and there was no idempotency boundary. Fixed with:
   partial unique index `ai.clarifications (execution_session_id) WHERE
   status='WAITING_FOR_USER'`, `ai.confirmations.plan jsonb` (approved-plan
   preservation), answer-before-resume, cross-org/cross-user rejection,
   create-or-reuse with unique-constraint recovery, no silent fallback
   (ambiguous/unknown named accounts re-ask with a targeted error).
   36 backend tests in `app/tests/test_ledger_confirmation_flow.py` pass;
   frontend vitest suite covers chip routing + double-click protection.
2. **CI workflow** (`.github/workflows/ci.yml`): backend pytest + frontend
   tsc/lint/vitest/build on every push and PR. Previously no CI at all.
3. **Backend suite un-ignored** and runs in CI (700+ tests, all passing).
4. **Frontend gates all pass**: tsc=0, lint=0, vitest pass, production
   build=0 (Next 15).
5. **Credential hygiene**: `github.txt.txt` (a live PAT pasted inside the
   repo, previously committable by `git add .`) moved OUT of the repo to
   `E:\Qoder\.secrets\github_pat.raw` and `.gitignore` extended (commit
   `5f63bb5`). PAT verified against GitHub API (200).
6. **Secret loader improved**: `.secrets\Load-Secrets.ps1` now also loads the
   raw PAT drop. Prints names only.
7. **`docs/AGENT_TOOLING_PLAYBOOK.md`** written (F1-F10 failure modes,
   pre-flight checklist, command recipes).
8. **Pre-existing invariants verified in the LIVE database**: no session with
   multiple pending clarifications (0), no duplicate active revenue accounts
   (0), no unbalanced posted journal entries (0).
9. **Work Stream S3 — LLM-primary accounting reasoning (IMPLEMENTED, verified
   this session).** The architectural correction: the LLM is now the primary
   accounting reasoning layer and Python is only the enforcement/execution
   layer. See `docs/LLM_PRIMARY_REASONING_ARCHITECTURE.md` for the full
   contract. Summary:
   * NEW `app/books_evidence.py` — a CLOSED, organization-scoped,
     permission-checked read surface of **15 evidence kinds** covering every
     accounting area (chart of accounts, subledgers, open receivables/
     payables, documents, journals, fixed assets, catalog, bank accounts,
     periods, policies, prior transactions, reports). Unknown kinds,
     undeclared arguments, tenant selectors and wrong argument types are
     refused and fed back; permission denial is returned as evidence;
     loader failure is data; results are bounded.
   * NEW `app/accounting_reasoning.py` — the bounded reasoning loop
     (`accounting_reasoning_max_rounds`, default 3) with exactly one decision
     per round: `NEEDS_EVIDENCE` → evidence returned → reassess →
     `NEEDS_INPUT` / `PROPOSAL` / `REFUSAL` / `COMPLETE` / `UNSUPPORTED`.
     Deterministic validation refuses an incomplete or disallowed decision and
     feeds the reason BACK to the model (never repaired into a hardcoded
     route). Also `preliminary_extraction()` — literals only, labelled
     `PRELIMINARY EXTRACTION — may be corrected after accounting review`.
   * `app/agent.py` — the loop runs BEFORE planning with the trusted tool
     registry as the offered set; `REFUSAL → REJECTED`,
     `COMPLETE → COMPLETED`, `NEEDS_INPUT → AWAITING_CLARIFICATION` (the
     model's own question), an accepted `PROPOSAL` becomes the execution plan
     (snapshot at the confirmation gate, executed only after approval through
     the full security/validation stack). The user sees the MODEL's
     accounting disclosure at confirmation (interpretation, affected records,
     impact, what will NOT change, uncertainty, exact confirmation sentence).
     The step log carries the whole reasoning trail.
   * `app/prompts.py` — system instructions declare the LLM the primary
     reasoning layer; context blocks are labelled `PRELIMINARY EXTRACTION`,
     `LIVE BOOKS EVIDENCE`, `LLM ACCOUNTING REASONING`.
   * `app/planner.py` — reduced to literal/candidate extraction: it reports
     candidates and hints ONLY (no treatment, no account, no party
     requirement, no workflow). 2193 added / 2149 removed lines.
   * `app/reasoning.py`, `app/context_manager.py` — deliberately UNCHANGED:
     they are the documented DEGRADATION path, used only when the provider is
     unavailable, the answer is unparseable, or the request is a batch.
     They no longer decide anything on the primary path.
   * Tests: `app/tests/test_llm_primary_reasoning.py` (33 tests) pins the
     contract, including: the same phrase yields a settlement with an open
     bill and a question without one; an absent fixed asset yields a question,
     never a disposal; an invented tool name never reaches a confirmation;
     provider failure records nothing. **Full backend suite: 911 passed, 0
     failures.**
   * Fixed in this session: the constitution-cache regression
     (`prompts.CONSTITUTION_PATH` now honours an explicit override while still
     resolving the documented `docs/` location); the disclosure rule now
     enforces PRESENCE (an explicit `unresolved_uncertainty: []` is a claim, an
     omitted field is refused) instead of demanding non-empty lists; the
     reasoning prompt now receives the trusted tool vocabulary (its
     "only offered tools" enforcement was previously inert); a provider
     EXCEPTION now degrades like a stall (honest `FAILED`, nothing recorded)
     instead of surfacing a generic error; `docs/LLM_PRIMARY_REASONING_ARCHITECTURE.md`
     created (it was referenced by three modules but did not exist); a dead
     `return outcome` line removed; the stale `app/planner.py.bak_prefill`
     deleted.
   * LATENCY DEFECT FOUND AND FIXED IN THE PREVIEW (measured). The per-round
     reasoning budget was **12s**, but the ~11 KB reasoning prompt needs longer:
     the live step log showed round 1 hitting the cap at **12.09s on every
     request** (`provider_failed: true, rounds: 1`), so the reasoning layer
     silently degraded and the legacy keyword route answered — for
     "record sale of fixed asset car on cash for 570000" it proposed
     `register_fixed_asset` (an ACQUISITION) after ~35-40s. Fixes:
     - `accounting_reasoning_timeout` 12s → **30s**, plus a NEW
       `accounting_reasoning_total_timeout` (45s) bounding all rounds.
     - NEW `accounting_reasoning_model_chain`, default **`qwen-max`**: measured
       against the live provider, `qwen-max` answered the reasoning prompt in
       **12.2s** with the correct decision (`event_type=disposal` + a
       `fixed_assets` evidence request), while the standard chain's first model
       (`qwen3.6-plus`, a thinking model) took **>35s**. Passed only to
       providers that declare the parameter, so nothing else breaks.
     - `provider_attempted` on the reasoning outcome: after a REAL provider
       failure the agent no longer fires the same chain twice more (perception,
       then planning) in one request — it stops honestly (`FAILED`, nothing
       recorded) unless the deterministic path can serve the request.
     - Prompt trimmed to ~10.9 KB and guarded by a test so the budget can never
       be silently outgrown again.
     - `app/tests/test_ledger_confirmation_flow.py` made HERMETIC (it was
       calling the live provider and reading the live DB); the suite is now 3x
       faster (30s vs 108s). **Full backend suite: 917 passed, 0 failures.**
   * STILL OPEN for this work stream: deploy the branch (the same procedure as
     section C of "Remaining work" below) and run the production smoke test for
     one disposal-type and one settlement-type request before declaring it
     live.

## Remaining work (in priority order)

> **STATUS UPDATE (deployment completed this session — keep for history):**
> A, B and C below are DONE. Final state:
> * Migration `080_security_advisory_fixes` was **applied to the live project and
>   verified by SQL query** (search_path pinned; `anon` denied on
>   `create_organization`/`accept_my_team_invite`; `authenticated` intact; the
>   template function still returns correct codes). The two WARN findings are
>   gone; the remaining findings are documented as accepted in
>   `docs/SECURITY_HARDENING_REVIEW.md` (advisor sweep section).
> * Work Stream S1 (grounded LLM entity segregation) is **implemented**:
>   `app/entity_segregation.py` + `prefill_entities` in `planner.plan()` +
>   agent wiring (probe plan → one bounded grounded LLM call → re-plan).
>   16 tests in `app/tests/test_entity_segregation.py`; full backend suite
>   **839 passed**; frontend tsc/lint/vitest/build all exit 0.
> * Deployment: branch pushed at `8bd0d84` → **PR #4 merged into `main`
>   (merge commit `77c4dd6`)** → CI **success** on the merge → Vercel
>   **production READY** (`dpl_7now2j9T7w2YMAWL1WKkqBceXeew`).
>   Smoke: `ai-accountant-erp.vercel.app` `/`, `/api/health`, `/docs` → all 200.
> * What is still open (the ONLY items):
>   1. **Browser-level ledger smoke test** on production with the TEST account
>      (in `E:\Qoder\.secrets\TEST_CREDENTIALS.local.txt`, never in chat):
>      execute one sale that needs a dedicated revenue ledger, answer CREATE,
>      verify exactly one ledger + one balanced journal via SQL, then confirm
>      the question does not repeat. This needs an interactive authenticated
>      session — do it through the UI or a scripted REST flow with the user's
>      own session.
>   2. **Manual dashboard step**: enable "Leaked password protection" in the
>      Supabase Auth settings (cannot be delivered from code).
>   3. Housekeeping: `tokens.env` contains a line named `GITHUB_PATX`
>      (likely a typo of `GITHUB_PAT`); review/remove it. The raw PAT now
>      loads from `.secrets\github_pat.raw` and takes precedence.

### A. Migration 080 - security advisories (DONE - history below)

From `supabase__get_advisors` (security), the actionable items:

1. `public.account_template_for_business_type` - function search_path mutable
   (WARN). Fix: `ALTER FUNCTION ... SET search_path = public, extensions;`
   It is a plain SQL STABLE function; adding a fixed search_path is safe.
2. `public.accept_my_team_invite()` and `public.create_organization(...)` -
   SECURITY DEFINER executable by `anon` (WARN). **Verified safe to revoke
   from anon**: the only callers are the onboarding page
   (`frontend/src/app/onboarding/page.tsx` -> `supabase.rpc(...)`, which runs
   with the user's own JWT as `authenticated`) and `app/main.py:~773`
   (documents the same intent). Revoke anon EXECUTE only; keep authenticated.
3. **DO NOT "fix" these two - they are intentional:**
   - `ai.worker_jobs` RLS enabled, no policies: service-role only by design.
   - `public.financial_operations` RLS enabled, no policies: same.
   Document them as accepted in `docs/SECURITY_HARDENING_REVIEW.md`.
4. `auth_leaked_password_protection` - dashboard setting, cannot be fixed by
   SQL. Report it to the user as a manual step in Supabase Auth settings.

Write as `database/migrations/080_security_advisory_fixes.sql`, forward-only
and idempotent (`IF EXISTS` guards, re-runnable). Apply it via the Supabase
MCP `apply_migration` tool, then **verify by querying** `pg_proc`/
`information_schema.role_function_grants` - do not trust the tool's success
message alone.

### B. Party/item extraction intelligence (the accuracy defect)

**Root cause (confirmed by reading the code):** extraction in
`app/planner.py` (`_SUPPLIER_PATTERNS`, `_CUSTOMER_PATTERNS`,
`_ITEM_PATTERNS`, ~lines 220-320) is pure hard-coded regex over fixed
phrasings. Any phrasing outside the templates yields no party/item, so the
agent re-asks.

**The approved approach (additive, grounded, no rewrite):**
- Build an **LLM entity-segregation stage** that runs only when regex
  extraction returns nothing (or is low-confidence).
- It must be **grounded**: every extracted value must appear verbatim
  (case/space-insensitive) in the user's own request text - reject anything
  else. No hallucinated names, no invented amounts.
- Regex stays authoritative; LLM only fills gaps.
- There is an existing `app/entity_contract.py` - read it first; it defines
  the entity schema. Reuse it.
- LLM client: `app/qwen_client.py` exposes text generation (`generate_text`);
  there is also a Gemini client. Check `app/ai_orchestrator.py` for the
  dispatch convention and follow it.
- Config: keys/flags live in `app/config.py` (QWEN/GEMINI settings).
- Add unit tests: grounded extraction works; ungrounded (hallucinated) values
  are rejected; regex path unaffected; LLM unavailable -> behaviour is
  exactly the old regex behaviour (graceful degradation).

### C. Deployment (after A and B are green)

- Both tokens are in the environment after dot-sourcing the loader.
- `vercel` CLI is absent - use the Vercel REST API with `VERCEL_TOKEN`
  (verified 200; project `ai-accountant-erp`, team `zameerchattha0-ops`).
  Alternatively hand the user the one-line CLI command.
- Deploy backend/database/frontend only after: backend suite green, all four
  frontend gates green, migration 080 applied AND verified by SQL query.
- Smoke test in a NON-production organisation using the TEST credentials
  (see security rule 4). Never smoke-test on live data.
- Do not run destructive commands; do not touch production data directly.

### D. Final report

Follow the report format from the original task (root cause, defects, files
changed, migrations, tests, exact commands + results, deployment checks,
smoke results, remaining risks, commit SHA + URLs).

<!--END-->
