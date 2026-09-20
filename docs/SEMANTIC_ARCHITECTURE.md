# Semantic Architecture (Work Stream S2 — AI-Native ERP)

## Objective

> The user communicates naturally. The AI understands naturally. The ERP
> validates deterministically. The accounting engine executes safely.

NOT: "the user must unknowingly speak the language of the Python planner."

## Pipeline

```
USER REQUEST
   ↓
LLM SEMANTIC UNDERSTANDING        (app/semantic_layer.py)
   ↓
GROUNDED BUSINESS INTENT / FACTS  (python grounding — hallucination gate)
   ↓
DETERMINISTIC NORMALIZATION       (semantic meaning → ERP vocabulary)
   ↓
PLANNER (intent, tools, gaps)     (app/planner.py)
   ↓
MISSING-INFORMATION ANALYSIS      (planner + reasoning)
   ↓
TARGETED CLARIFICATION IF REQUIRED (only what is genuinely missing/ambiguous)
   ↓
MERGED COMPLETE INTENT            (answers merged, never re-asked)
   ↓
DETERMINISTIC VALIDATION          (tool router / validators)
   ↓
ACCOUNTING REASONING              (nature, accounts, journals)
   ↓
CONFIRMATION                      (immutable approved plan)
   ↓
EXECUTION                         (trusted tools → atomic RPCs)
   ↓
VERIFY + RESPOND                  (journal verified from the database)
```

## Responsibility split

### 1. LLM — semantic understanding (`app/semantic_layer.py`)

Receives: the complete user request, today's date, and the distilled domain
rulebook (verbatim grounding, written numbers, relative dates, the nature
taxonomy, money direction, ERP intent vocabulary). It may also receive org
context and prior answers on resume paths.

Produces a **rich semantic representation**, not planner fields:

* `activity` — the business act in the user's language (supplied, bought from
  us, cleared their dues…), not a trigger keyword;
* `party` + `party_role` — who, and money-in vs money-out;
* `item`, `quantity`, `amount`, `payment_terms`, `transaction_date`,
  `transaction_nature`;
* per-fact `status`: **EXPLICIT** (user stated it — preserve exactly),
  **SAFELY_INFERRED** (derived by documented reasoning, with `evidence`),
  **AMBIGUOUS** (never silently chosen), **MISSING**.

The LLM never produces ERP IDs, never invents names, never executes.

### 2. Grounding + normalization (python, deterministic)

* Text values (party, item) must occur **verbatim** in the user's own words
  (normalised case/punctuation) — hallucinations are dropped, never repaired.
* Numbers must be traceable to a digit token or written number
  ("two" → 2, "23k" → 23000, "2.5 lac" → 250000).
* Dates must be an explicit date in the text or a resolvable relative word
  ("yesterday" → today−1).
* Enums (`payment_terms`, `transaction_nature`, party role) are validated
  against the canonical vocabularies; party role must be consistent with the
  activity's money direction or it is treated as ambiguous.
* Normalization maps semantic meaning to ERP vocabulary:
  `activity × payment_terms → planner intent` (e.g. supplied + pay-later →
  `record_credit_sale`), `party_role → customer_name/supplier_name`,
  `payment_terms → payment_method`.

### 3. Planner (`app/planner.py`)

Consumes the grounded facts as `prefill_entities` (setdefault semantics —
later stages can still be more authoritative). Computes the **complete
normalized intent**, tools, economic event, and — critically — the
**missing-information analysis**: only fields genuinely required for a safe
posting and genuinely absent. Clarification questions are produced from
`missing_fields`, so a fully-stated request asks nothing.

### 4. Validator / tool router

Authorization, org ownership, schema/argument validation, document-item
arithmetic, party existence. Deterministic; unaffected by AI output.

### 5. Accounting engine

Nature resolution, account selection, journal composition, balancing,
verification from the database. Deterministic.

### 6. Trusted tools + database

Idempotency keys, atomic SQL RPCs, unique constraints, RLS, audit trail,
confirmation state. Deterministic.

## Precedence

```
EXPLICIT USER INFORMATION
  > GROUNDED LLM INTERPRETATION     (verbatim-copied, traceable, evidence)
    > DETERMINISTIC NORMALIZATION   (word→digit, relative→ISO, enum mapping)
      > REGEX FALLBACK              (only when the LLM produced nothing)
```

Backend validation, security constraints and the confirmation gate override
ALL AI output.

## Clarification logic

Questions exist only for facts that are (a) required by the deterministic
validation/accounting rules for this intent, and (b) neither stated, safely
inferred, nor resolvable. Ambiguous facts produce a targeted choice question
(never a silent pick). A request like "Sold two chairs to HJK Pvt Limited for
Rs. 23,000 on credit yesterday" yields at most a missing-date question — and
with "yesterday" stated, nothing at all.

## Regex policy

Regex remains for: fallback when the LLM is unavailable, deterministic
normalisation, and high-confidence numeric/date parsing. It must never
override a grounded LLM interpretation because the wording differed, and
**new phrasings are handled by improving the semantic instructions, grounding
or domain model — never by adding keywords.**

## What remains deterministic (never AI)

Authorization, organization ownership, RLS, entity/account existence, journal
balancing, database constraints, idempotency, transaction atomicity,
confirmation state, audit trail, financial integrity.
