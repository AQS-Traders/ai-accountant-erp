# LLM-Primary Accounting Reasoning — Architecture

**Status:** implemented (Work Stream S3)
**Modules:** `app/books_evidence.py`, `app/accounting_reasoning.py`, `app/agent.py`,
`app/prompts.py`, `app/config.py`, `app/models/schemas.py`
**Tests:** `app/tests/test_llm_primary_reasoning.py`

---

## 1. The problem this corrects

The earlier design had deterministic Python deciding *accounting meaning* before
the model had seen any books:

```
user phrase → regex intent → fixed question ladder → fixed tool sequence
```

That cannot be safe, because Python cannot anticipate every real-world
accounting situation. A keyword route could

* pick the wrong workflow (`paid ABC 50,000` assumed a supplier payment),
* invent a treatment (assume the party exists, assume the asset is registered),
* demand the wrong party (force a customer ledger on a cash-only disposal),
* suppress a necessary question (the open bill it should have settled),
* or block a legitimate request (an event the fixed path never anticipated).

The same phrase means different things in different books. Only something that
can read the books and reason about them can decide.

## 2. The corrected architecture

```
User request
    │
    ▼
Authenticated agent session  (tenant, user, role — Python)
    │
    ▼
Initial deterministic extraction only            ← PRELIMINARY, never a verdict
    │  preserve original wording
    │  extract obvious literal values
    │  identify possible subject areas
    │  (no final accounting decision)
    ▼
LLM accounting reasoning loop                    ← the primary reasoning layer
    │  · model requests the evidence it needs
    │  · Python validates + permission-checks + reads it
    │  · model reassesses on LIVE BOOKS EVIDENCE
    │  · model chooses exactly one next step
    ▼
Python policy / security / execution layer       ← enforcement only
    │  identity, org scope, tool registry, schema, permissions,
    │  confirmation gate, idempotency, period rules, journal balancing,
    │  database constraints, audit
    ▼
Execution through the trusted tool router
    │
    ▼
Result returned to the model → reassessment → question / next safe action
```

The model never writes anything directly. It can only *propose* tool calls from
the trusted registry; Python decides whether they may run at all.

## 3. Module map

| Module | Responsibility |
| --- | --- |
| `app/books_evidence.py` | The closed, organization-scoped, permission-checked read surface ("evidence catalog") plus its rendering into labelled blocks. Decides what a lookup *may* read; never which lookup is needed. |
| `app/accounting_reasoning.py` | The reasoning loop: prompt assembly, response parsing, the round budget, and the deterministic validation of a decision. Also `preliminary_extraction()` (literals only). |
| `app/agent.py` | Wiring: runs the loop before planning, maps its outcome onto the API response, and routes an accepted proposal through the existing security/confirmation/execution stack. |
| `app/prompts.py` | The system-level statement that the LLM is the reasoning layer, and the labelled context blocks (`PRELIMINARY EXTRACTION`, `LIVE BOOKS EVIDENCE`, `LLM ACCOUNTING REASONING`). |
| `app/config.py` | `accounting_reasoning_enabled`, `accounting_reasoning_timeout`, `accounting_reasoning_max_rounds`. |
| `app/models/schemas.py` | `AgentContext.preliminary_extraction`, `.live_evidence`, `.accounting_reasoning` so every later stage (and the planning call) sees the same evidence. |

## 4. Preliminary extraction — the only thing Python may decide up front

`preliminary_extraction(user_message)` returns, always labelled

> `PRELIMINARY EXTRACTION — may be corrected after accounting review`

* `original_request` — the user's wording, verbatim;
* `literals` — amount, transaction date, item description, payment channel,
  candidate party/item terms, an explicit document reference
  (`INV-…`, `BILL-…`, …);
* `candidate_subject_areas` — hints such as `fixed_assets`, `payables`,
  `receivables`, `revenue`, `expenses`, `purchases`, `loans`, `equity`, `tax`,
  `bank`, `cash`;
* `provisional_intent_hint` — the legacy keyword intent, offered as a hint;
* `note` — "Provisional only. The accounting treatment must be determined by
  reading the live books — correct any of this that the records contradict."

None of it is a treatment, an account, a party requirement, a workflow, or a
tool sequence. Structured values are named `…candidates` precisely because the
model must confirm them against the books.


## 5. The evidence catalog (what the model may inspect)

`EVIDENCE_KINDS` is a **closed registry**. Each entry declares its argument
whitelist (name + type), the read-only tool that gates it, and the loader that
reads the organization's own rows.

| Kind | Covers | Gated by |
| --- | --- | --- |
| `chart_of_accounts` | every ledger, its type, normal balance, posting/heading role and financial-statement line | `get_chart_of_accounts` |
| `account_search` | ledger search by the request's own words | `search_account` |
| `parties` | customers **and** suppliers matching the words | `search_customer` |
| `ledgers` | customer/supplier subledger movement with running balance | `get_customer_ledger` |
| `open_receivables` | unsettled sales invoices (optionally one party) | `get_customer_ledger` |
| `open_payables` | unsettled purchase bills (optionally one party) | `get_supplier_ledger` |
| `documents` | existing invoices, purchase bills, expenses | `get_invoice` |
| `journal_entries` | journal entries with line-level debits/credits | `get_general_ledger` |
| `fixed_assets` | register: cost, accumulated depreciation, book value, status, lifecycle rows | `search_fixed_asset` |
| `catalog` | products and services | `search_product` |
| `bank_accounts` | bank/cash accounts available for settlement | `list_bank_accounts` |
| `periods` | financial years / accounting periods with the OPEN one flagged | `search_account` |
| `policies` | organization profile + learned accounting policies | `search_account` |
| `prior_transactions` | receipts, supplier payments, expenses already recorded | `get_customer_ledger` |
| `reports` | trial balance, income statement, balance-sheet lines | `get_trial_balance` |

The catalog spans **every** material accounting object and every
financial-statement area — assets (cash/bank, receivables, inventory/catalog,
prepaid, fixed assets, accumulated depreciation, intangibles), liabilities
(payables, accruals, loans, taxes, advances), equity (capital, drawings,
retained earnings, current-period result), revenue (product, service, other
income, disposal proceeds, discounts/returns), expenses (operating, cost of
sales, depreciation, finance costs, tax, loss on disposal) and the statements
they feed. It is **not** fixed-asset specific.

Execution rules (`gather_evidence`):

* the **organization id is injected by Python** — any attempt to pass a tenant
  selector (`organization_id`, `org_id`, `tenant`, …) is refused;
* an unknown kind, an undeclared argument, or a wrongly-typed argument is
  **rejected with the reason fed back to the model** — never silently repaired
  into a different lookup;
* a kind whose gating read-only tool the role may not use is **denied** (the
  denial is returned as evidence, so the model learns it cannot inspect that
  area);
* loads run in parallel, a loader exception degrades to an `error` on that one
  result, and every result set is bounded (`MAX_RECORDS_PER_KIND = 12`,
  `MAX_TERMS = 5`, `MAX_REQUESTS_PER_ROUND = 6`, `MAX_RECORD_CHARS = 1200`);
* results are rendered under the label
  `LIVE BOOKS EVIDENCE — use this to reassess the request`, with empty results
  marked `EMPTY — no matching record exists` (emptiness is information, not a
  failure).

## 6. The reasoning loop contract

`run_reasoning_loop(facts, …)` is bounded by TWO budgets — a per-round wall
clock (`accounting_reasoning_timeout`, default 30 s) and a whole-stage cap
(`accounting_reasoning_total_timeout`, default 45 s), so three slow rounds can
never add up to a minute of user-visible waiting. Each round the model returns
**exactly one** decision, in this precedence:

### Why the budget must exceed the provider's real latency (measured defect)

The reasoning prompt is the LARGEST call in the pipeline — ~11.7 KB: the rules,
the evidence catalog and the trusted tool vocabulary (59 tool names). Failure
mode observed in production before this was fixed: the per-round cap was 12 s
while the provider needs longer than that for a prompt this size, so **round 1
timed out on every request** (`provider_failed: true, rounds: 1` — the step log
showed a 12.09 s gap), the loop degraded, and the legacy pipeline then decided
the treatment from keywords (for `record sale of fixed asset car on cash for
570000` it proposed `register_fixed_asset`, i.e. an ACQUISITION). The user paid
~35-40 s for a wrong route. A budget below the provider's latency is therefore a
correctness bug, not just a performance one: it silently disables the reasoning
layer.

When the provider IS actually unavailable that fact is recorded
(`provider_attempted=True`) and the same provider chain is **not** retried twice
more in the same request (see §8).

| Status | Meaning | Agent reaction |
| --- | --- | --- |
| `NEEDS_EVIDENCE` | "read these areas before I decide" | validate → read → return evidence → next round |
| `NEEDS_INPUT` | a contextual question only the user can answer | `AWAITING_CLARIFICATION` with that exact question |
| `PROPOSAL` | a concrete, fully-disclosed next step | snapshot for confirmation; execute only after approval |
| `REFUSAL` | unsafe / unsupported / prerequisite missing | `REJECTED` with the model's explanation; nothing runs |
| `COMPLETE` | the records already reflect the request | `COMPLETED`, quoting the records |
| `UNSUPPORTED` | nothing usable was produced | escalate: after the budget, or hand over to the legacy pipeline |

A decision that violates the enforcement rules is **not** executed and **not**
repaired: the violation text is fed back into the next round
(`REJECTED BY THE EXECUTION LAYER`), bounded by the round budget. If the budget
runs out with violations, the outcome is `UNSUPPORTED` with `violations`
attached, and the caller falls back rather than guessing.

Provider behaviour: a timeout, a provider exception, or an unparseable answer
returns `provider_failed=True` with the evidence gathered so far. Such an

## 7. What a proposal must disclose (and how Python refuses an incomplete one)

Before any mutation the model must produce all of:

```
interpretation          the proposed business reading, in words
affected_records        which records/ledgers will change
accounting_impact       the proposed debit/credit consequence (verified, never trusted)
not_affected            what will deliberately NOT change
unresolved_uncertainty  what is still uncertain (an empty list is a claim)
tools                   tool calls from the TRUSTED registry only
confirmation            the exact sentence to show the user
```

Enforcement details (`validate_outcome`):

* **presence, not non-emptiness** is what is checked. `unresolved_uncertainty:
  []` and `not_affected: []` are explicit claims the model is entitled to make;
  requiring a non-empty list would push it to *invent* uncertainty, which this
  architecture forbids. An **omitted** key is silence and is refused.
* `tools` must be non-empty, inside the offered set (the trusted registry the
  agent passes in) and outside the prohibited set.
* `interpretation` and `confirmation` must be non-empty.
* a violation never executes and is never quietly patched — it is returned to
  the model with the reason.

`proposed_mutation_tools()` separates read-only calls from mutations using the
registry's own `read_only` flag — never the model's description of a tool.

## 8. Agent wiring

`app/agent.py`, before any planning:

1. `preliminary_extraction(user_message)` runs and is logged as
   `PRELIMINARY_EXTRACTION` (audit-visible, single source of truth).
2. Unless the request is a batch, the reasoning loop runs with the **trusted
   tool registry** as the offered set, the clarification history, the org
   preferences and the request itself.
3. Outcomes map straight onto the API contract: `REFUSAL → REJECTED`,
   `COMPLETE → COMPLETED`, `NEEDS_INPUT → AWAITING_CLARIFICATION`
   (the model's own question, grounded in the records it read),
   `PROPOSAL → _reasoning_calls`.
4. An accepted proposal **is** the plan: the deterministic call builder is
   bypassed for it, the plan is snapshotted for the user's confirmation, and
   execution still passes through the full security/validation stack
   (registry, permissions, period rules, journal balancing, idempotency).
5. Everything the model saw and proposed is written to the step log
   (`ACCOUNTING_REASONING`, `EVIDENCE_REQUESTED`, `EVIDENCE_RETURNED`,
   `DECISION_REJECTED`, `REASONING_PROPOSAL_ACCEPTED`, `REFUSED_BY_REASONING`),
   so the reasoning trail is auditable.
6. The same evidence is attached to the planning context
   (`preliminary_extraction`, `live_evidence`, `accounting_reasoning`) so the
   later planning call reassesses instead of silently replacing the reasoning
   with a keyword route.

### Degradation

| Situation | Behaviour |
| --- | --- |
| Reasoning disabled (`accounting_reasoning_enabled=false`) | the previous deterministic pipeline runs unchanged |
| Provider unavailable / timeout / unparseable | `provider_failed` + `provider_attempted` → the pipeline does NOT fire two more identical provider calls (perception, then planning). It stops honestly: `FAILED`, nothing recorded, the user is told to resend and every fact is kept |
| Provider unavailable on the **planning/execution** call | honest `FAILED` response, nothing recorded, the user is told to resend; earlier work is kept |
| Batch request | handled by the existing batch path (unchanged) |

A dead provider never becomes a guessed accounting route.


## 9. Worked examples

**`record sale of fixed asset motor bike on cash for 56000`**
Python extracts only the literals (`56000`, `CASH`, candidate area
`fixed_assets`) plus the provisional intent hint. The model then requests —
at minimum — `fixed_assets` and `journal_entries`, and reasons from what it
finds:

* *asset registered?* → cost, accumulated depreciation, current book value,
  already-disposed check, proceeds, gain/loss, removal of cost **and** the
  accumulated depreciation account, and the resulting journal. The
  confirmation quotes the real numbers.
* *asset absent?* → it must not pretend the asset exists. It explains that a
  disposal cannot be correctly recorded until recognition and book value are
  established, and asks for the acquisition cost, date, accumulated
  depreciation, vendor/reference, and whether 56,000 is proceeds or
  acquisition value.

No customer is required: a cash disposal has no customer ledger, and Python
does not demand one (`create_customer` is not implied by the word "sale").

**`paid 50,000 to ABC`** — the model inspects `parties`, `open_payables` (or
`open_receivables`), `prior_transactions`, `ledgers` and `journal_entries`, and
decides whether this is settlement of a bill, an advance, a loan repayment, an
owner withdrawal, payment of an already-recorded expense, payment for a new
purchase, or a correction. With an open bill it proposes the settlement; with
none it asks what the money actually was.

**`received 100,000 from XYZ`** — settlement, customer advance, loan received,
capital introduced, other income, or a refund/recovery: decided from the open
receivables, ledger and journal evidence.

**`record internet expense of 5,000`** — already recorded as a payable, being
settled now, prepaid, or new; which expense account; whether tax is relevant;
which period it belongs to.

**`bought equipment for 500,000`** — fixed asset vs inventory vs expense; cash
vs credit; whether a supplier record is required; whether it is already
recorded; whether the amount is cost or payment; whether a capitalization
policy applies.

**`sold goods for 80,000`** — goods vs service vs fixed-asset disposal; cash vs
credit; customer requirement; revenue account; cost of sales; tax; whether the
sale is already invoiced.

**`record an adjustment for 25,000`** — the model must **not** invent debit and
credit accounts: it establishes why the adjustment exists, which account(s) are
affected, whether source records exist, whether the period is open, and whether
it is a correction, accrual, reclassification, reversal or estimate.

## 10. Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `accounting_reasoning_enabled` | `True` | run the loop; `false` restores the previous pipeline exactly |
| `accounting_reasoning_timeout` | `30.0` | per-round wall-clock cap. Must stay **above the provider's real latency** for the ~12 KB prompt, otherwise every round times out and the reasoning layer is silently disabled (measured defect: 12 s → always timed out) |
| `accounting_reasoning_total_timeout` | `45.0` | cap for the whole loop (all rounds) |
| `accounting_reasoning_max_rounds` | `3` | evidence request → reassessment → decision budget |
| `qwen_model_chain` | `qwen3.6-plus,qwen-max` | the reasoning round is the first call of a request, so it pays the cold-start cost; a faster first model shortens the whole stage |


## 11. Tests that pin this contract

`app/tests/test_llm_primary_reasoning.py` (the LLM is a scripted provider —
the CONTRACT is what is pinned, not a live model's behaviour):

* the catalog covers every accounting area and every kind is gated by a
  registered **read-only** tool;
* unknown kinds, undeclared arguments, tenant selectors and wrong argument
  types are refused;
* permission denial is returned as evidence and never reads the books;
* loader failure is data, not an exception; results are bounded; empty is
  information and is rendered as such;
* preliminary extraction is literal-only and labelled;
* the loop requests evidence, receives it labelled, and reassesses on it;
* the same phrase produces a settlement with an open bill and a question
  without one — the differing records, not the verb, decided;
* an absent fixed asset yields a question, never a disposal;
* incomplete proposals, unoffered tools and prohibited tools are refused and
  fed back; an explicit empty disclosure list is accepted while an omitted
  field is refused; an invented tool name never reaches a confirmation;
* provider failure and a missing orchestrator degrade without inventing a
  decision, and the agent-level failure path records nothing;
* the reasoning prompt carries the trusted tool vocabulary plus the labelled
  `PRELIMINARY EXTRACTION` and `LIVE BOOKS EVIDENCE` blocks.

## 12. Rules for future changes

**Do not** add:

* keyword → intent → fixed question → fixed tool sequences for a phrase;
* hardcoded assumptions that a party, asset, document, account, period or
  workflow exists (or is required) for a given wording;
* any Python code that decides an accounting treatment from a keyword;
* any fallback that turns a provider outage into a guessed accounting route.

**Do** add, when a real gap is found:

* a new **evidence kind** (argument whitelist, gating read-only tool, loader)
  so the model can inspect the area — not a new keyword route;
* a sharper disclosure requirement in the proposal contract;
* a new enforcement check in the validation/execution layer;
* a test in `test_llm_primary_reasoning.py` that fails if the reasoning is
  bypassed.

The LLM reasons; Python enforces and executes.

outcome is **never** treated as a decision.
