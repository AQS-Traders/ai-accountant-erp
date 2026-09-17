# Chart of Accounts — Legacy (deployed) vs New Catalog

**Generated during verification of the "UI, AI Organization Onboarding, and Chart of
Accounts Enhancement" brief, requirement 11 ("Preserve Accounting Hierarchy and
Reporting Relationships") and requirement 7–9 (COA granularity).**

## Why this document exists

Migration 067 replaced the chart-of-accounts design with a **hierarchical, business-aware
catalog** (`account_catalog` → `account_template_items` → `accounts`). New organizations
receive it correctly — verified end-to-end.

The 15 organizations that already existed were seeded from the **previous** chart design,
which used a *different numbering scheme*. This document reconciles the two so a decision
about back-filling existing organizations can be made on evidence rather than assumption.

## Method

For each business type in use, the chart a **new** organization would receive
(`account_template_items` for the template resolved by `account_template_for_business_type()`,
non-optional items only) was compared code-by-code against the accounts actually deployed
in the existing organizations of that type.

Organizations of the same business type carry an identical chart, so the reconciliation is
presented per business type.

## Organization inventory

| Organization | Business type | Accounts | Journal entries | Invoices |
|---|---|---|---|---|
| **Test Traders (Pvt) Ltd** | OTHER | **32** | **124** | **23** |
| Court Karao | OTHER | 20 | 0 | 0 |
| CM Advisory | OTHER | 20 | 0 | 0 |
| Public Ltd | OTHER | 20 | 0 | 0 |
| Victoria Treasures Limited | OTHER | 20 | 0 | 0 |
| **Zameer Labs PVT Ltd** | SOFTWARE_HOUSE | **25** | **16** | **3** |
| Xyz | SOFTWARE_HOUSE | 24 | 2 | 2 |
| Softy Ltd | SOFTWARE_HOUSE | 24 | 0 | 0 |
| SoftLab | SOFTWARE_HOUSE | 24 | 0 | 0 |
| Enam enterprises | SOFTWARE_HOUSE | 24 | 0 | 0 |
| Accountax | CONSULTING | 20 | 1 | 1 |
| MARAZI MULTISERVICE SOLUTIONS… | CONSULTING | 20 | 0 | 1 |
| PT Subur Makmur | E_COMMERCE | 20 | 0 | 0 |
| Tazij meats and chips | MANUFACTURING | 20 | 0 | 0 |
| Majid Aljassim Advocates & Legal Consultants | SERVICE_BUSINESS | 20 | 1 | 0 |

**Two organizations carry real accounting data**: `Test Traders (Pvt) Ltd` (124 journal
entries, 23 invoices) and `Zameer Labs PVT Ltd` (16 journal entries). Any restructuring
must not disturb their posted history.

## Summary of the reconciliation

| Business type | identical (code **and** name) | **code reused, different meaning** | legacy-only | in new chart, absent today |
|---|---|---|---|---|
| SOFTWARE_HOUSE | 9 | **9** | 7 | 34 |
| OTHER | 10 | **7** | 15 | 33 |
| CONSULTING | 9 | **6** | 5 | 37 |
| E_COMMERCE | 9 | **6** | 5 | 37 |
| MANUFACTURING | 9 | **6** | 5 | 36 |
| SERVICE_BUSINESS | 10 | **6** | 4 | 34 |

Only about **16 of 333** deployed account codes exist in the new catalog for their
business type — and the majority of those mean something **different**.

---

## The blocking problem: codes reused with different meanings

These codes exist in **both** charts but describe **different accounts**. A code-based
back-fill would therefore attach existing accounts to the wrong parent and the wrong
financial-statement classification.

### Conflicts present in every business type

| Code | Deployed meaning | New catalog meaning | New parent |
|---|---|---|---|
| `1500` | Computer Equipment | **Property, Plant & Equipment** | (root) |
| `6100` | Office Supplies / Cloud Infrastructure | **Payroll & Staff Costs** | (root) |
| `6110` | Software Subscriptions | **Salaries** | `6100` |
| `6120` | Internet | **Freelancers & Contractors** | `6100` |
| `6200` | Depreciation Expense | **Premises** | (root) |
| `6300` | Bank Charges | **Communication** | (root) |

### Additional conflicts by business type

| Business type | Code | Deployed meaning | New catalog meaning |
|---|---|---|---|
| OTHER | `1000` | Cash | Cash & Cash Equivalents |
| SOFTWARE_HOUSE | `1300` | Prepaid Expenses | Prepayments & Deposits |
| SOFTWARE_HOUSE | `4020` | Consulting Revenue | Software Development Revenue |
| SOFTWARE_HOUSE | `4030` | Maintenance Revenue | Consulting Revenue |

### Safely identical codes (the only ones a code-based back-fill could trust)

`1010` Bank · `1020` Cash · `1100` Accounts Receivable · `2010` Accounts Payable ·
`2110` Sales Tax Payable · `3010` Owner Capital · `3200` Retained Earnings ·
`4900` Other Income · `6000` General Operating Expense

---

## SOFTWARE_HOUSE — full reconciliation (5 organizations)

| Code | Deployed (live) | New catalog |
|---|---|---|
| `1010` | Bank | Bank — **match** |
| `1020` | Cash | Cash — **match** |
| `1100` | Accounts Receivable | Accounts Receivable — **match** |
| `2010` | Accounts Payable | Accounts Payable — **match** |
| `2110` | Sales Tax Payable | Sales Tax Payable — **match** |
| `3010` | Owner Capital | Owner Capital — **match** |
| `3200` | Retained Earnings | Retained Earnings — **match** |
| `4900` | Other Income | Other Income — **match** |
| `6000` | General Operating Expense | General Operating Expense — **match** |
| `1300` | Prepaid Expenses | Prepayments & Deposits — **CONFLICT** |
| `1500` | Computer Equipment | Property, Plant & Equipment — **CONFLICT** |
| `4020` | Consulting Revenue | Software Development Revenue — **CONFLICT** |
| `4030` | Maintenance Revenue | Consulting Revenue — **CONFLICT** |
| `6100` | Cloud Infrastructure | Payroll & Staff Costs — **CONFLICT** |
| `6110` | Software Subscriptions | Salaries — **CONFLICT** |
| `6120` | Internet | Freelancers & Contractors — **CONFLICT** |
| `6200` | Depreciation Expense | Premises — **CONFLICT** |
| `6300` | Bank Charges | Communication — **CONFLICT** |
| `1510` | Accumulated Depreciation - Computer Equipment | *(absent)* — legacy-only |
| `2130` | Accrued Salaries | *(absent)* — legacy-only |
| `4010` | Software Development Revenue | *(absent)* — legacy-only |
| `6010` | Salaries | *(absent)* — legacy-only |
| `6020` | Freelancers | *(absent)* — legacy-only |
| `6130` | Office Supplies | *(absent)* — legacy-only |
| `6140` | Utilities | *(absent)* — legacy-only |
| 34 further codes | *(absent)* | new chart only, e.g. `1000`, `1200`, `1230`–`1250`, `1520`–`1580`, `1700`, `2000`, `2100`, `2200`, `2210`, `2220`, `2500`, `3000`, `4000`, `4040`, `6210`–`6230`, `6310`, `6400`, `6410`, `6430`, `6500`–`6540`, `6600`, `6610`, `6700`, `6710`, `6730` |

## OTHER — full reconciliation (5 organizations)

The richest legacy chart — and it contains **user-created accounts**:

| Code | Deployed (live) | New catalog |
|---|---|---|
| `1010` `1020` `1100` `2010` `2110` `3010` `3200` `4900` `6000` | as listed above | **match** |
| `1000` | Cash | Cash & Cash Equivalents — **CONFLICT** |
| `1500` | Computer Equipment | Property, Plant & Equipment — **CONFLICT** |
| `6100` | Office Supplies | Payroll & Staff Costs — **CONFLICT** |
| `6110` | Software Subscriptions | Salaries — **CONFLICT** |
| `6120` | Internet | Freelancers & Contractors — **CONFLICT** |
| `6200` | Depreciation Expense | Premises — **CONFLICT** |
| `6300` | Bank Charges | Communication — **CONFLICT** |
| `1011` | Cash | *(absent)* — legacy-only |
| `1012` | **Test Traders - Bank Account** | *(absent)* — **user data** |
| `1013` | Bank Account | *(absent)* — legacy-only |
| `1014` | Cash Account | *(absent)* — legacy-only |
| `1200` | Inventory | *(absent)* — legacy-only |
| `1510` | Accumulated Depreciation - Computer Equipment | *(absent)* — legacy-only |
| `4020` | Other Operating Revenue | *(absent)* — legacy-only |
| `4050` | Managed Services Revenue | *(absent)* — legacy-only |
| `6010` | Salaries | *(absent)* — legacy-only |
| `6020` | Freelancers | *(absent)* — legacy-only |
| `6130` | Advertising Expense | *(absent)* — legacy-only |
| `6140` | Utilities Expense | *(absent)* — legacy-only |
| `6150` | Hardware Purchases | *(absent)* — legacy-only |
| `6151` | **Rent Expense - Building A** | *(absent)* — **user data** |
| `6152` | Rent Expense | *(absent)* — legacy-only |
| 33 further codes | *(absent)* | new chart only |

### Why this matters

`1012 Test Traders - Bank Account` and `6151 Rent Expense - Building A` were created by the
organization through normal use — bank screens create GL accounts, and users edit expense
accounts. **The deployed charts are no longer pure seed data**; they are live accounting
data carrying posted balances. Rewriting them would rewrite history.

---

## Conclusion

A code-based back-fill is **not safe**:

1. **6–9 codes per organization carry a different meaning** in the two designs. Linking by
   code would silently mis-classify balances and misstate the Balance Sheet and P&L.
2. **Only ~9–10 of ~20–32 codes per organization are safely comparable.**
3. **The charts contain user-created accounts and posted history** — 124 journal entries and
   23 invoices in one organization alone.
4. Requirement 11 states directly: *"Do not rename, delete, or restructure existing accounts
   blindly if other parts of the system depend on their IDs or relationships."*

### Options, in order of risk

| # | Option | Effect | Risk |
|---|---|---|---|
| **A** | **Leave existing organizations as they are** | New organizations get the hierarchy; the 15 keep their working flat charts | **none** |
| **B** | **Additive top-up** — add only the non-colliding new codes and link them; never rename or delete | Existing organizations gain most of the new granularity | medium — the ~6–9 colliding codes must be decided per code first, and those specific accounts cannot be added |
| **C** | **Full re-seed** of existing charts | Uniform hierarchy everywhere | **high** — requires migrating `journal_lines`, `invoices` and every other reference off the old account IDs |

Option A is the default and is what the system does today: organizations created **after**
migration 067 receive the business-aware hierarchy (verified end-to-end), while the 15
pre-existing organizations retain their original charts unreformed. Option B is feasible
with a per-code decision on each conflict. Option C should only be attempted with a tested
data migration and a verified backup.
