"""
ERP AI Agent — Planner
========================
Translates natural language into an execution plan.

The Planner determines:
* Intent
* Extracted entities (amount, supplier, customer, item, date, payment method)
* Required context sources
* Potentially required tools
* Whether validation / accounting engine / confirmation is needed
* Clarification questions (ONLY for genuinely missing material information)

Clarification philosophy (anti-loop guarantees):
* Extract FIRST — every entity format variant is attempted before
  deciding anything is missing.
* Reuse — values answered in prior clarification rounds are merged into
  the entity set and NEVER re-asked.
* Minimal & consolidated — ALL independently-missing material fields are
  gathered into ONE questionnaire per round (dependency-first ordering,
  no fixed question sequence, no drip-feeding one question per turn).
* Trust the LLM — when extraction is inconclusive the request is passed
  to Gemini with all extracted hints rather than blocked here.

The Planner does NOT directly execute database mutations.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import structlog

from app.date_parser import parse_transaction_date, resolve_date_range
from app.reasoning import (
    DATE_REQUIRED_INTENTS,
    NATURE_DECISION_QUESTION,
    EXPENSE_EVENT_DATE_QUESTION,
    SETTLEMENT_POSITION_QUESTION,
    capitalization_question_for,
    capitalization_threshold_from_prefs,
    nature_family_for_intent,
    nature_for_purpose,
    nature_question_for_intent,
    purpose_label,
    purpose_option,
    purpose_question,
    purpose_requires_capitalization_question,
    resolve_capitalization_answer,
    resolve_nature_answer,
    resolve_purpose_answer,
    resolve_settlement_answer,
)
from app.models.schemas import ExecutionPlan

log = structlog.get_logger(__name__)


# Intents whose nature comes from the EXPENSE decision tree
# (FIXED_ASSET / INVENTORY / CONSUMABLE / OPERATING_EXPENSE / SERVICE).
#
# A LEARNED nature preference is only meaningful inside this family.  It must
# never answer a SALE: a sale's nature vocabulary is
# GOODS / SERVICE / ASSET_DISPOSAL / OTHER_INCOME, and applying an expense
# nature to one both mislabels the transaction ("Nature: Operating Expense" on
# a sale) and suppresses the sale-type question, which then lets the revenue
# account be resolved silently by fuzzy name match.
_EXPENSE_NATURE_INTENTS = frozenset({
    "record_expense",
    "record_purchase",
    "record_cash_purchase",
    "record_credit_purchase",
    "register_fixed_asset",
})

# Work Stream B: hard ceiling on batch size - beyond this the request is
# treated as a single (possibly itemised) transaction instead of N
# documents, preventing pathological N-document explosions.
_BATCH_MAX_ITEMS = 10

# ---------------------------------------------------------------------------
# Intent patterns — order matters (more specific first)
# ---------------------------------------------------------------------------
_INTENT_PATTERNS: List[tuple[str, list[str]]] = [
    # Reports (read-only, no mutation)
    ("generate_trial_balance", [r"trial\s*balance"]),
    ("generate_balance_sheet", [r"balance\s*sheet"]),
    ("generate_profit_loss", [r"profit.*loss", r"\bp\s*&?\s*l\b", r"income\s*statement"]),
    ("generate_cash_flow", [r"cash\s*flow"]),
    ("generate_general_ledger", [r"general\s*ledger"]),
    ("customer_balance", [r"owe", r"receivable", r"customer.*ledger", r"how much.*owe"]),
    ("supplier_balance", [r"supplier.*ledger", r"payable.*supplier", r"how much.*owe.*supplier"]),
    ("project_profitability", [r"project.*profit", r"profitability"]),
    ("generate_customer_ledger", [r"customer.*ledger"]),
    ("generate_supplier_ledger", [r"supplier.*ledger"]),
    # Expense LOOKUPS must win over record_expense: the bare "expense"
    # keyword in record_expense used to swallow query phrasings like
    # "provide details of current month expenses" and drag them into the
    # recording clarification ladder (live defect).  Query-shaped
    # phrasings are matched FIRST and routed to the read-only listing.
    ("list_expenses", [
        r"(?:show|list|give|provide|tell|view|display|details?|summary|"
        r"summar\w*|breakdown|what|which|how much|any|all)[^.\n]*expenses?",
        r"expenses?[^.\n]*\b(?:details?|report|summary|list|history|"
        r"breakdown|this month|last month|this week|last month)",
        r"expense\s+report",
        r"(?:how much|what|total)[^.\n]*\bspen[td]\b",
    ]),
    # New document types
    # convert_quotation MUST precede create_quotation: "convert quotation to
    # invoice" also matches the generic quotation pattern below.
    ("convert_quotation", [r"convert.*(?:quotation|quote)", r"(?:quotation|quote).*invoice", r"accept.*(?:quotation|quote).*invoice"]),
    ("create_quotation", [r"quotation", r"quote", r"price\s*quote", r"create.*quote", r"send.*quotation"]),
    ("create_credit_note", [r"credit\s*note", r"refund", r"return.*goods.*customer", r"issue.*credit"]),
    ("create_purchase_return", [r"purchase\s*return", r"return.*goods.*supplier", r"return.*to.*supplier", r"send.*back.*supplier"]),
    ("record_expense_payment", [r"paid.*expense", r"expense.*payment", r"paid.*bill", r"settled.*bill"]),
    # Mutations
    ("create_invoice", [r"create.*invoice", r"invoice.*for", r"bill.*customer"]),
    ("record_credit_sale", [r"sold.*credit", r"credit\s*sale", r"on\s*credit.*sold"]),
    ("record_cash_sale", [r"sold.*cash", r"cash\s*sale", r"received\s*payment.*for"]),
    ("record_credit_purchase", [r"bought.*credit", r"credit\s*purchase", r"on\s*credit.*bought", r"purchased.*credit"]),
    ("record_cash_purchase", [r"bought.*cash", r"cash\s*purchase", r"purchased.*cash", r"bought.*paid"]),
    # record_expense requires a RECORDING verb (or an amount-bearing
    # "expense of/for <figure>" phrasing) — a bare "expense" keyword
    # swallowed query phrasings and sent them into the recording ladder.
    ("record_expense", [
        r"record[^.\n]*expense",
        r"(?:log|book|add|track|enter|submit|file)[^.\n]*expense",
        r"expense\s+(?:of|for)\s+(?:rs\.?|pkr)?\s*[\d,]",
        r"spent",
    ]),
    ("record_receipt", [r"receipt", r"received.*from.*customer", r"customer.*paid", r"payment.*received"]),
    ("record_payment", [r"paid.*supplier", r"supplier.*payment", r"payment.*to.*supplier"]),
    ("record_bank_transfer", [r"transfer.*bank", r"bank.*transfer", r"transfer.*from.*to.*account", r"move.*money.*from", r"transfer.*hbl\b", r"transfer.*meezan\b"]),
    ("create_bank_account", [r"add.*bank.*account", r"create.*bank.*account", r"new.*bank.*account", r"open.*bank.*account"]),
    ("list_bank_accounts", [r"bank.*account", r"list.*bank", r"show.*bank"]),
    # Fixed-asset lifecycle + product catalog (BEFORE generic buy/sell so
    # the specific economic event wins)
    ("dispose_fixed_asset", [r"dispos.*asset", r"sell.*(?:asset|machinery|vehicle|equipment|furniture)", r"sold.*(?:asset|machinery|vehicle|equipment|furniture)", r"write.?off.*asset", r"scrap.*asset"]),
    ("record_asset_depreciation", [r"depreciat"]),
    ("register_fixed_asset", [r"fixed asset", r"register.*asset", r"capitaliz.*asset", r"capitalis.*asset", r"new.*(?:machinery|generator|delivery van|forklift)"]),
    ("create_product", [r"add.*product", r"create.*product", r"new product", r"register.*product"]),
    ("create_service", [r"add.*service", r"create.*service", r"new service", r"register.*service"]),
    ("record_purchase", [r"purchase", r"bought", r"purchased"]),
    ("record_sale", [r"sold", r"sale"]),
]

# Intents for which a missing amount is material enough to clarify.
_TRANSACTION_INTENTS = {
    "record_credit_purchase", "record_cash_purchase", "record_purchase",
    "record_credit_sale", "record_cash_sale", "record_sale",
    "record_expense", "record_receipt", "record_payment",
    "create_invoice", "create_credit_note", "create_purchase_return",
    "record_expense_payment", "record_bank_transfer",
    "register_fixed_asset",
}

# Work Stream S2 — planner intents the SEMANTIC layer may set. This is the
# deterministic whitelist: a semantic intent outside it is ignored and the
# keyword extractor decides (the LLM cannot invent an intent).
_SEMANTIC_INTENTS = frozenset({
    "record_credit_sale", "record_cash_sale", "record_sale",
    "record_credit_purchase", "record_cash_purchase", "record_purchase",
    "record_expense", "record_receipt", "record_payment",
    "create_invoice", "create_purchase_bill", "create_credit_note",
    "create_purchase_return",
})

# Work Stream A - MANDATORY TRANSACTION-DATE PROTOCOL: every mutation
# intent must resolve an explicit accounting date before any tool call.
# Canonical set lives in app.reasoning (DATE_REQUIRED_INTENTS) so the
# dependency graph and the planner can never drift apart.

# ---------------------------------------------------------------------------
# Amount extraction — ordered by confidence (highest first)
# ---------------------------------------------------------------------------
_NUM = r"([\d,]+(?:\.\d+)?)"

_AMOUNT_PATTERNS = [
    # Explicit clarification answer (highest confidence — it IS the answer)
    re.compile(
        r"(?:clarification|answer)\s*:?[^.\n]*?" + _NUM, re.IGNORECASE
    ),
    # Currency BEFORE number: "Rs 20000", "Rs. 20,000", "PKR 20000", "₨ 20000"
    re.compile(r"(?:Rs\.?|PKR|₨)\s*" + _NUM + r"\s*(k|thousand|lac|lakh|million)?\b", re.IGNORECASE),
    # Number BEFORE currency: "20000 rs", "20,000 Rs.", "20000 PKR"
    re.compile(r"\b" + _NUM + r"\s*(?:rs\.?|pkr|₨)\b", re.IGNORECASE),
    # South-Asian suffix: "20000/-"
    re.compile(r"\b" + _NUM + r"\s*/-"),
    # Multiplier suffix: "20k", "1.5 lac"
    re.compile(r"\b" + _NUM + r"\s*(k|lac|lakh|million)\b", re.IGNORECASE),
    # Strong money context words: "total 15000", "worth 20000", "paid 5000",
    # "received payment of 8000", ...  (allows filler like "a payment of")
    re.compile(
        r"\b(?:total(?:ing|led)?|worth|amount(?:ing)?\s+to|paid|paying|"
        r"price(?:\s+of|\s+is)?|cost(?:ing)?|spent|received|receiving|"
        r"deposit(?:ed)?|withdr(?:ew|awn|aw)|transfer(?:red)?)\s+"
        r"(?:an?\s+)?(?:payment\s+)?(?:of\s+)?(?:rs\.?\s*|pkr\s*)?" + _NUM,
        re.IGNORECASE,
    ),
    # R4.9 — labelled amount cue: "AMOUNT 20,000", "amount: 15000",
    # "amount - 8000".  3+ digits so counts ("amount 2 items") never match.
    re.compile(
        r"\bamount\s*[:\-]?\s*(?:rs\.?\s*|pkr\s*)?([\d,]{3,}(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
    # "for <number>" — classic price position ("bought X for 20000").
    # Requires 3+ digits to avoid matching counts ("for 3 items").
    re.compile(r"\bfor\s+(?:rs\.?\s*|pkr\s*)?([\d,]{3,}(?:\.\d+)?)\b", re.IGNORECASE),
    # R3.1 — loose expense phrasings put the figure after "of"
    # ("record an expense of 25000", "an explanation of 25000").
    # Requires 4+ digits so counts ("a batch of 100 items") never match.
    re.compile(r"\bof\s+(?:rs\.?\s*|pkr\s*)?([\d,]{4,}(?:\.\d+)?)\b", re.IGNORECASE),
    # "record expense: 5000" — a bare figure directly after the colon.
    re.compile(r":\s*(?:rs\.?\s*|pkr\s*)?([\d,]{4,}(?:\.\d+)?)\s*$", re.IGNORECASE),
]

_AMOUNT_MULTIPLIERS = {
    "k": 1_000,
    "thousand": 1_000,
    "lac": 100_000,
    "lakh": 100_000,
    "million": 1_000_000,
}

# ---------------------------------------------------------------------------
# Entity (name) extraction — case-insensitive, position-aware
# ---------------------------------------------------------------------------
_NAME = r"([a-zA-Z][\w&.]*(?:\s+[a-zA-Z][\w&.]*)*?)"
_NAME_STOP = (
    r"(?=\s+(?:for|of|on|in|to|via|through|by|worth|total|amount|and|at)\b"
    r"|[,.!?;]|\s*$|\s+\d)"
)

_SUPPLIER_PATTERNS = [
    re.compile(r"\bfrom\s+" + _NAME + _NAME_STOP, re.IGNORECASE),
    re.compile(r"\bsupplier\s+" + _NAME + _NAME_STOP, re.IGNORECASE),
]

# Action verbs, gerunds and document nouns can never START a party name.
# Without this guard, "create an invoice for selling 2 ovens to lkj pvt
# limited" captured "selling" as the customer: _NAME_STOP's `\s+\d`
# lookahead ends the name at the quantity that follows the verb, so the
# regex never reached the real party after "to".
_NON_PARTY_WORDS = {
    "selling", "sold", "sale", "sells", "sell", "buying", "bought", "buy",
    "purchasing", "purchased", "purchase", "procuring", "acquired",
    "invoice", "invoicing", "bill", "billing", "quotation", "quote",
    "creating", "create", "record", "recording", "issue", "issuing",
    "raise", "raising", "make", "making", "add", "adding", "enter",
    "entering", "post", "posting", "be", "is", "are", "was", "were",
    "to", "the", "a", "an", "and", "or",
}


def _is_plausible_party(value: str) -> bool:
    """True when a captured string can plausibly be a party name.

    Rejects pronouns / short filler and any capture whose FIRST word is an
    action verb or document noun (``selling``, ``invoice``, ``bought`` ...).
    That is exactly how "create an invoice for selling 2 ovens to lkj pvt
    limited" used to produce the customer name "selling".
    """
    text = (value or "").strip()
    if len(text) < 2:
        return False
    words = text.split()
    if not words:
        return False
    if text.lower() in ("the", "a", "an", "it", "us", "me", "him", "her", "them"):
        return False
    return words[0].strip(",.").lower() not in _NON_PARTY_WORDS

_CUSTOMER_PATTERNS = [
    # "invoice/quote/bill ... to <party>" — the party is often stated at the
    # END, after the goods being invoiced ("create an invoice for selling
    # 2 ovens to lkj pvt limited").  MUST precede the
    # "invoice ... for <party>" rule below, which would otherwise capture the
    # verb ("selling") that follows "for".
    re.compile(
        r"\b(?:invoice|bill|quotation|quote)\b[^.]*?\bto\s+" + _NAME + _NAME_STOP,
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsold\b[^.]*?\bto\s+" + _NAME + _NAME_STOP, re.IGNORECASE
    ),
    re.compile(r"\bcustomer\s+" + _NAME + _NAME_STOP, re.IGNORECASE),
    re.compile(
        r"\b(?:invoice|bill|quotation|quote)\s+(?:for|to)\s+" + _NAME + _NAME_STOP,
        re.IGNORECASE,
    ),
    # "<Proper Name> owes/ledger/receivable/balance" — every word of the
    # name must be capitalised (Title or ACRONYM) so ordinary sentences
    # like "Show me the trial balance" are not captured as customer names.
    re.compile(
        r"\b([A-Z][a-z&.]*|[A-Z]{2,})(?:\s+(?:[A-Z][a-z&.]*|[A-Z]{2,}))+"
        r"\s+(?:owes?|ledger|receivable|balance)\b"
    ),
]

_ITEM_PATTERNS = [
    re.compile(
        r"\b(?:purchas(?:e|es|ed|ing)|bought|buy(?:ing)?|procured?|acquired?)\s+"
        r"(?:an?\s+|the\s+|some\s+|\d+\s+)?"
        r"([\w][\w\s-]*?)(?=\s+(?:from|for|on|in|at|via)\b|[,.!?;]|\s*$|\s+\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:sold|sells?|selling)\s+(?:an?\s+|the\s+|some\s+|\d+\s+)?"
        r"([\w][\w\s-]*?)(?=\s+(?:to|from|for|on|in|at|via)\b|[,.!?;]|\s*$|\s+\d)",
        re.IGNORECASE,
    ),
]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ---------------------------------------------------------------------------
# Payment method detection
# ---------------------------------------------------------------------------
_PAYMENT_METHOD_PATTERNS = [
    (re.compile(r"\bon\s+credit\b|\bcredit\s+(?:purchase|sale|basis|terms)\b|\bkhata\b|\budhaar\b", re.IGNORECASE), "CREDIT"),
    (re.compile(r"\bin\s+cash\b|\bcash\s+(?:purchase|sale|payment)\b|\bpaid\s+(?:in\s+)?cash\b|\bcash\b", re.IGNORECASE), "CASH"),
    (re.compile(r"\bbank\s+transfer\b|\bwire\s+transfer\b|\bonline\s+banking\b|\btransfer(?:red)?\s+(?:to|from)\b", re.IGNORECASE), "BANK_TRANSFER"),
    (re.compile(r"\bcheque\b|\bcheck\b", re.IGNORECASE), "CHEQUE"),
    (re.compile(r"\b(?:credit|debit)\s+card\b|\bcard\s+payment\b|\bby\s+card\b", re.IGNORECASE), "CARD"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def split_batch_request(user_message: str) -> List[str]:
    """Detect a MULTI-TRANSACTION request and split it into sub-requests.

    Work Stream B. Conservative by design - only confident enumeration
    patterns are split:
      * numbered lists on separate lines ("1) ... 2) ..."),
      * inline enumerators between transactional clauses
        ("...; also ...", "... and also ...", "... then ..."),
        where EVERY segment carries transactional content.

    Returns the list of sub-request strings (>= 2), or [] for a normal
    single-transaction request. Item-quantity phrasing ("buy 3 chairs and
    2 desks") is NOT split here - it stays with the existing per-item
    item-intake protocol.
    """
    msg = (user_message or "").strip()
    if not msg:
        return []

    # (a) Numbered enumeration, one document per line.
    numbered = list(
        re.finditer(r"^\s*\d{1,2}[\)\.]\s*(.+?)\s*$", msg, re.MULTILINE)
    )
    if len(numbered) >= 2:
        segments = [m.group(1).strip(" ;,") for m in numbered]
        segments = [s for s in segments if s]
        if 2 <= len(segments) <= _BATCH_MAX_ITEMS:
            return segments

    # (b) Inline enumerators. Split, then require every segment to carry
    #     transactional content (amount or a mutation keyword) so narrative
    #     prose is never chopped into phantom transactions.
    parts = re.split(
        r";\s*(?:also\s+|then\s+)?"
        r"|\band\s+also\s+"
        r"|\balso,\s+"
        r"|\bthen\s+",
        msg,
        flags=re.IGNORECASE,
    )
    parts = [p.strip(" .;,") for p in parts if p and p.strip(" .;,")]
    if 2 <= len(parts) <= _BATCH_MAX_ITEMS:
        transactional = re.compile(
            r"rs\.?\s*\d|pkr\s*\d|\d{3,}"
            r"|\b(bought|sold|paid|expense|invoice|purchase|sale|receipt|payment|bill|record|create)\b",
            re.IGNORECASE,
        )
        if all(transactional.search(p) for p in parts):
            return parts

    return []


def _plan_batch(
    segments: List[str],
    clarification_history: Optional[List[Dict[str, str]]],
    user_message: str = "",
    org_preferences: Optional[Dict[str, str]] = None,
) -> ExecutionPlan:
    """Plan each enumerated sub-request and merge into ONE batch plan.

    Extends (never forks) the existing pipeline: the merged plan reuses
    the standard consolidated-clarification questionnaire (one box per
    question, numbered per document so ``_explode_multi_answers`` maps
    the answers back) and ONE confirmation gate covering all mutations.
    Per-item entities are prefixed ``item_<n>_`` so sibling documents
    never contaminate each other's values (no cross-item first-hit).

    A segment fragment ("Office supplies Rs.5,000") may carry no verb on
    its own - the HEADER line ("Record these 3 expenses:") declares the
    transaction type, so unknown fragment intents inherit the header's
    intent (genuinely generic: the header names the economic event).
    """
    header = ""
    m = re.search(
        r"^(.*?)(?=^\s*\d{1,2}[\)\.]\s)", user_message, re.MULTILINE | re.DOTALL
    )
    if m:
        header = m.group(1).strip(" :\n\t")
    header_plan = plan(header, org_preferences=org_preferences) if header else None

    sub_plans: List[ExecutionPlan] = []
    for seg in segments:
        sub = plan(seg, clarification_history, org_preferences=org_preferences)
        if (
            sub.intent == "unknown"
            and header_plan is not None
            and header_plan.intent != "unknown"
        ):
            # Genuinely generic inheritance: the HEADER names the event
            # ("record these 3 EXPENSES"); it is never a first-record
            # fallback for accounts/parties - those still resolve per item.
            sub.intent = header_plan.intent
            sub.potential_tools = header_plan.potential_tools or sub.potential_tools
            sub.required_context = header_plan.required_context or sub.required_context
            # The sub-plan was planned as "unknown" and therefore asked
            # nothing - recompute the material gaps (amount, date, ...) for
            # the INHERITED intent so its questions join the merged
            # questionnaire ("For document i: ...").
            sub.missing_fields = _missing_fields(
                sub.intent, sub.extracted_entities
            )
            sub.clarification_questions = _questions_for_fields(
                sub.intent, sub.missing_fields, sub.extracted_entities
            )
            sub.requires_clarification = bool(sub.clarification_questions)
            sub.requires_accounting_engine = (
                sub.requires_accounting_engine
                or header_plan.requires_accounting_engine
            )
            sub.requires_confirmation = (
                sub.requires_confirmation or header_plan.requires_confirmation
            )
            sub.expected_outcome = header_plan.expected_outcome
        sub_plans.append(sub)
    merged = sub_plans[0].model_copy(deep=True)

    batch_items: List[Dict[str, Any]] = []
    missing: List[str] = []
    questions: List[str] = []
    tools: List[str] = []
    context_srcs: List[str] = []
    entities: Dict[str, Any] = {}
    requires_confirmation = False
    requires_validation = False
    requires_accounting = False

    for i, (seg, sub) in enumerate(zip(segments, sub_plans), start=1):
        batch_items.append({
            "position": i,
            "segment": seg,
            "intent": sub.intent,
            "missing_fields": list(sub.missing_fields),
            "requires_confirmation": sub.requires_confirmation,
            "requires_clarification": sub.requires_clarification,
        })
        for f in sub.missing_fields:
            key = f"item_{i}:{f}"
            if key not in missing:
                missing.append(key)
        for q in sub.clarification_questions:
            qq = f"For document {i}: {q}"
            if qq not in questions:
                questions.append(qq)
        for t in sub.potential_tools:
            if t not in tools:
                tools.append(t)
        for c in sub.required_context:
            if c not in context_srcs:
                context_srcs.append(c)
        for k, v in (sub.extracted_entities or {}).items():
            entities[f"item_{i}_{k}"] = v
        requires_confirmation = requires_confirmation or sub.requires_confirmation
        requires_validation = requires_validation or sub.requires_validation
        requires_accounting = requires_accounting or sub.requires_accounting_engine

    merged.batch_items = batch_items
    merged.missing_fields = missing
    merged.clarification_questions = questions
    merged.requires_clarification = bool(questions) or any(
        s.requires_clarification for s in sub_plans
    )
    merged.requires_confirmation = requires_confirmation
    merged.requires_validation = requires_validation
    merged.requires_accounting_engine = requires_accounting
    merged.potential_tools = tools
    merged.required_context = context_srcs
    merged.extracted_entities = entities
    merged.expected_outcome = (
        f"Batch of {len(segments)} documents processed - one result per document "
        "(a failed document never rolls back its successful siblings)"
    )
    return merged


def plan(
    user_message: str,
    clarification_history: Optional[List[Dict[str, str]]] = None,
    org_preferences: Optional[Dict[str, str]] = None,
    prefill_entities: Optional[Dict[str, Any]] = None,
) -> ExecutionPlan:
    """Analyse *user_message* (+ prior Q&A) and return an ``ExecutionPlan``.

    ``clarification_history`` is a list of ``{"question": ..., "answer": ...}``
    dicts from earlier clarification rounds in the same conversation.  Every
    answered value is merged into the extracted entities so it is never
    asked for again.

    ``org_preferences`` (Work Stream F) carries learned organization-level
    defaults; they are treated as answered-for entities UNLESS the user
    explicitly said something different in the message or history.

    ``prefill_entities`` (Work Stream S1) carries grounded values produced by
    the LLM entity-segregation stage. They are inserted with ``setdefault``
    semantics — a value captured by regex from the message, answered in a
    clarification round, or learned as an org preference always wins.
    """
    msg = user_message.strip()
    msg_lower = msg.lower()

    # 0. BATCH DETECTION (Work Stream B): an enumerated multi-transaction
    #    request is planned per sub-document and merged into ONE plan, so
    #    the existing consolidated-clarification (one round for ALL gaps)
    #    and ONE-confirmation machinery handle the batch unchanged.
    segments = split_batch_request(user_message)
    if len(segments) > 1:
        return _plan_batch(
            segments, clarification_history, user_message, org_preferences
        )

    # 1. Identify intent — SEMANTIC FIRST (Work Stream S2).
    #    The LLM semantic layer expresses the business act in ERP vocabulary
    #    ("supplied X with chairs, payment later" -> record_credit_sale).
    #    A whitelist makes any unknown/out-of-vocabulary value fall back to
    #    the keyword extractor, which stays the FALLBACK — never the brain.
    _semantic_intent = (prefill_entities or {}).get("semantic_intent")
    if _semantic_intent in _SEMANTIC_INTENTS:
        intent = _semantic_intent
    else:
        intent = _identify_intent(msg_lower)
    payment_method = _extract_payment_method(msg_lower)
    intent = _refine_intent(intent, payment_method)

    # 2. Extract entities from the raw message
    entities: Dict[str, Any] = _extract_entities(msg)
    amount = _extract_amount(msg)
    if amount is not None:
        entities["amount"] = amount
    if payment_method:
        entities["payment_method"] = payment_method
    txn_date = _extract_date(msg, msg_lower)
    if txn_date:
        entities["transaction_date"] = txn_date
    # Report/list intents accept a relative period ("this month", "last
    # quarter", …) which becomes an explicit date_from/date_to pair so the
    # read-only fast path can filter deterministically ("current month"
    # is normalised to "this month" by the parser).
    _RANGE_INTENTS = {
        "list_expenses", "generate_general_ledger",
    }
    if intent in _RANGE_INTENTS:
        rng = resolve_date_range(msg)
        if rng:
            entities.setdefault("date_from", rng[0])
            entities.setdefault("date_to", rng[1])
    item = _extract_item(msg)
    if item:
        entities["item_description"] = item
        # The count in the item phrasing IS the quantity ("selling 2 ovens").
        # It used to be discarded, which made the agent ask for a quantity
        # the user had already given.
        stated_qty = _extract_item_quantity(msg, item)
        if stated_qty is not None:
            entities.setdefault("item_quantity", stated_qty)
            entities.setdefault("quantity", stated_qty)
    # Work Stream R4.10 — MULTI-LINE ITEMS: several items stated in ONE
    # request ("2 laptops at 5000 each and 3 mice at 500") are parsed into
    # DISTINCT lines; the invoice total is DERIVED from them (Σ qty ×
    # price) so the header can never disagree with its own lines.
    line_items = _extract_line_items(msg)
    # ATTACHED DOCUMENTS: the vision-extracted block already carries the
    # authoritative line-item table and supplier.  Parse it so a multi-line
    # GRN/bill becomes DISTINCT lines with a DERIVED total (its own rows)
    # instead of collapsing into the first line's amount - and so a supplier
    # named on the document is never replaced by table furniture such as
    # "line items".
    if not line_items:
        doc_lines, doc_supplier = _extract_document_lines(msg)
        if doc_lines:
            line_items = doc_lines
        if doc_supplier:
            existing_party = str(
                entities.get("supplier_name") or ""
            ).strip().lower()
            if not existing_party or existing_party in _JUNK_PARTY_NAMES:
                entities["supplier_name"] = doc_supplier
    if line_items:
        entities["line_items"] = line_items
        entities["amount"] = _line_items_total(line_items)
        if not entities.get("item_description"):
            entities["item_description"] = " / ".join(
                l["description"] for l in line_items
            )
    # Asset-lifecycle intents: the asset reference after "for"/"of" is the
    # asset name (currency/amount shapes are explicitly excluded).
    if intent in (
        "register_fixed_asset", "dispose_fixed_asset",
        "record_asset_depreciation",
    ) and not entities.get("item_description"):
        m = re.search(
            r"(?:\bfor\b|\bof\b)\s+"
            r"((?![Rr]s\b|PKR\b|₨|\d)[A-Za-z][\w&.-]*(?:\s+[\w&.-]+)*)\s*$",
            msg, re.IGNORECASE,
        )
        if m:
            entities["asset_name"] = m.group(1).strip()

    # 3. Merge answers from prior clarification rounds (never re-ask)
    nature_source: Optional[str] = None
    # Work Stream R4 — settlement-party hygiene + extraction.  The loose
    # entity extractor can fill supplier_name/customer_name with noise
    # ("payment", "Rs.12"); a garbage name must never reach the party
    # gate — it is dropped BEFORE the clarification merge so a real
    # answered name is never blocked (R4.4).  "from <Name>" / "to <Name>"
    # are the real party signals for settlements.
    _PARTY_NOISE = (
        "supplier", "the supplier", "vendor", "the vendor", "payment",
        "customer", "the customer", "client", "party",
    )
    if intent == "record_payment":
        name = str(entities.get("supplier_name") or "").strip()
        if (
            not name
            or name.lower() in _PARTY_NOISE
            or re.fullmatch(r"(rs\.?|pkr)?\s*[\d,]+(?:\.\d+)?", name, re.IGNORECASE)
        ):
            entities.pop("supplier_name", None)
        _pm = re.search(
            r"\bto\s+([a-z][a-z0-9 .&'-]{1,30}?)"
            r"(?:\s+(?:rs|pkr|for|today|yesterday|tomorrow)\b|\s*[\d,]|\s*$)",
            msg_lower,
        )
        if _pm and not entities.get("supplier_name"):
            candidate = _pm.group(1).strip()
            if candidate.lower() not in _PARTY_NOISE:
                entities["supplier_name"] = candidate
    if intent == "record_receipt":
        name = str(entities.get("customer_name") or "").strip()
        if (
            not name
            or name.lower() in _PARTY_NOISE
            or re.fullmatch(r"(rs\.?|pkr)?\s*[\d,]+(?:\.\d+)?", name, re.IGNORECASE)
        ):
            entities.pop("customer_name", None)
        _cm = re.search(
            r"\bfrom\s+([a-z][a-z0-9 .&'-]{1,30}?)"
            r"(?:\s+(?:rs|pkr|for|today|yesterday|tomorrow)\b|\s*[\d,]|\s*$)",
            msg_lower,
        )
        if _cm and not entities.get("customer_name"):
            candidate = _cm.group(1).strip()
            if candidate.lower() not in _PARTY_NOISE:
                entities["customer_name"] = candidate

    # Work Stream R4.5 — bank-transfer parties: the "from X to Y" bank
    # names are material for the deterministic transfer path (the trusted
    # tool resolves names to ids and refuses unknown banks — accounts are
    # never invented).  Extraction is best-effort; unresolved names keep
    # the transfer on the model path.
    if intent == "record_bank_transfer":
        _tb = re.search(
            r"from\s+([a-z0-9][a-z0-9 .&'-]{0,30}?)\s+to\s+"
            r"([a-z0-9][a-z0-9 .&'-]{0,30}?)"
            r"(?:\s+(?:rs|pkr|for|today|yesterday|tomorrow)\b|\s*[\d,]|\s*$)",
            msg_lower,
        )
        if _tb:
            entities.setdefault("source_bank_name", _tb.group(1).strip())
            entities.setdefault(
                "destination_bank_name", _tb.group(2).strip()
            )

    nature_before_merge = entities.get("transaction_nature")
    if clarification_history:
        entities = _merge_clarification_answers(entities, clarification_history)
        if not nature_before_merge and entities.get("transaction_nature"):
            # Work Stream R: the nature came from an explicit user answer
            # to the consolidated decision tree - logged for the audit trail.
            nature_source = "USER_ANSWER"
        # A clarification answer may have resolved the payment treatment —
        # re-specialise the generic intent so the downstream gates (required
        # fields, confirmation) reflect the now-known treatment.
        if intent in ("record_purchase", "record_sale") and entities.get("payment_method"):
            intent = _refine_intent(intent, entities.get("payment_method"))

    # 2b. GROUNDED LLM PREFILL (Work Stream S2 — AI-first perception).
    #     Facts segregated from the user's OWN sentence by app/semantic_layer.py
    #     + app/entity_segregation.py (verbatim-grounded in Python) fill the
    #     gaps BEFORE any questionnaire is computed, so fields the user
    #     already stated flow to the next stages instead of being re-asked.
    #     APPLICATION POINT: AFTER the clarification-answer merge and BEFORE
    #     org preferences.  Precedence (later stages overwrite): regex capture
    #     <= LLM prefill < clarification answer < org preference — an
    #     ANSWERED value is therefore never clobbered by a grounded LLM guess.
    #     Regex keeps winning on conflicts via setdefault; every prefill value
    #     was already verified to exist in the message before it got here.
    for _pk, _pv in (prefill_entities or {}).items():
        if _pv is None or _pv == "" or _pk == "semantic_intent":
            continue
        if _pk in ("supplier_name", "customer_name"):
            # Work Stream R4.4 stays true whichever stage produced the name:
            # table furniture ("payment") and amount shapes ("Rs.12") must
            # never reach the party gate.
            _prefill_name = str(_pv).strip()
            if (
                not _prefill_name
                or _prefill_name.lower() in _PARTY_NOISE
                or re.fullmatch(
                    r"(rs\.?|pkr)?\s*[\d,]+(?:\.\d+)?",
                    _prefill_name,
                    re.IGNORECASE,
                )
            ):
                continue
        if _pk == "item_quantity":
            # Mirror the regex path, which fills the generic quantity alias
            # alongside the item-scoped one.
            if not entities.get("item_quantity"):
                entities["item_quantity"] = _pv
                if not entities.get("quantity"):
                    entities["quantity"] = _pv
            continue
        if not entities.get(_pk):
            entities[_pk] = _pv

    # 3a. ORG PREFERENCES (Work Stream F) - learned defaults are treated as
    #     answered-for entities; an explicit user value ALWAYS wins (the
    #     extraction above already captured it).  The transaction date is
    #     NEVER defaulted - it is governed by the mandatory date protocol.
    prefs = org_preferences or {}
    if prefs:
        # Work Stream R2 (product decision): the payment TREATMENT is a
        # per-transaction question, NEVER an assumed default.  The user
        # explicitly chooses cash/bank/credit in the first round, and only
        # a CREDIT answer pulls the party question into the next round.
        # A learned payment_method preference is deliberately NOT
        # auto-applied (it stays recorded for reporting only).
        #
        # LIVE DEFECT (fixed): the learned nature default was applied to ANY
        # intent, including SALES.  Its vocabulary is the EXPENSE decision
        # tree (FIXED_ASSET / INVENTORY / CONSUMABLE / OPERATING_EXPENSE /
        # SERVICE), so a sale inherited an expense nature — which is how
        # "Record Sale of Mobile PHONE" was recorded with
        # transaction_nature=OPERATING_EXPENSE and the UI badge read
        # "Nature: Operating Expense" on a SALE.  It also suppressed the
        # sale-type question, so the revenue account was resolved silently.
        # A learned nature may only answer where that taxonomy applies.
        if (
            not entities.get("transaction_nature")
            and prefs.get("transaction_nature")
            and intent in _EXPENSE_NATURE_INTENTS
        ):
            entities["transaction_nature"] = prefs["transaction_nature"]
            # Work Stream R: a learned org default answered the nature.
            nature_source = "PREFERENCE"

    # Work Stream R3 — capitalization threshold + learned decisions:
    # the org-set capitalization threshold governs when the R3.2
    # question fires (small amounts auto-expense), and a learned
    # capitalization decision for THIS purpose (preference key
    # "capitalization:<PURPOSE>") stops the recurring re-ask.
    entities.setdefault(
        "capitalization_threshold",
        capitalization_threshold_from_prefs(prefs),
    )

    # CA-GRADE REASONING (expense path): when the message itself names a
    # KNOWN expense category ("record electricity bill of 20,000"), the
    # agent derives the purpose ITSELF — IFRS presentation gives every
    # expense category its own line item and its own ledger — and never
    # wastes a clarification round re-asking what the request already
    # answered.  Only keyword-matched categories qualify; free text that
    # matches no category still asks the purpose question.
    if intent == "record_expense" and not entities.get("transaction_purpose"):
        _cat_text = " ".join(
            str(x) for x in (
                entities.get("item_description"),
                entities.get("description"),
            ) if x
        )
        if _cat_text:
            _derived = resolve_purpose_answer(purpose_question(), _cat_text)
            _derived_opt = purpose_option(str(_derived)) if _derived else None
            if _derived_opt is not None and _derived_opt.keywords:
                entities["transaction_purpose"] = _derived

    purpose_now = entities.get("transaction_purpose")
    if (
        isinstance(purpose_now, str)
        and purpose_now.upper() not in ("OTHER", "OTHER_DURABLE")
        and not entities.get("capitalization_decision")
        and prefs.get(f"capitalization:{purpose_now.upper()}")
    ):
        entities["capitalization_decision"] = prefs[
            f"capitalization:{purpose_now.upper()}"
        ]

    # Work Stream R3.1 — the purpose answer DERIVES the transaction
    # nature (before routing): ambiguous purposes follow the R3.2
    # decision (CAPITALIZE -> FIXED_ASSET), everything else keeps its
    # deterministic nature.  A purpose-derived nature is an explicit
    # user decision for the audit trail.
    purpose_now = entities.get("transaction_purpose")
    if (
        intent == "record_expense"
        and isinstance(purpose_now, str)
        and not entities.get("transaction_nature")
    ):
        capitalized = (
            str(entities.get("capitalization_decision") or "").upper()
            == "CAPITALIZE"
        )
        entities["transaction_nature"] = nature_for_purpose(
            purpose_now, capitalized=capitalized
        )
        if not nature_source:
            nature_source = "USER_ANSWER"

    # R3.5 — description auto-fill: when the purpose was chosen from the
    # option list the description is rarely needed; fill it from the
    # purpose label (e.g. "Rent — August 2026") unless one was given.
    purpose_now = entities.get("transaction_purpose")
    if (
        intent == "record_expense"
        and isinstance(purpose_now, str)
        and not entities.get("description")
    ):
        try:
            when = entities.get("transaction_date") or ""
            stamp = ""
            if when:
                d = parse_transaction_date(str(when))
                if d.ok and d.date is not None:
                    stamp = f" — {d.date.strftime('%B %Y')}"
            entities["description"] = f"{purpose_label(purpose_now)}{stamp}"
        except Exception:  # noqa: BLE001 — autofill must never break planning
            entities["description"] = purpose_label(purpose_now)

    # 3b. ITEM INTAKE ROUTING — the payment TREATMENT decides the
    # destination (IFRS: treatment depends on the payment terms):
    #   * CREDIT  -> ALWAYS the purchase-bill path: Dr account / Cr PARTY
    #                PAYABLE (never a paid tool; settled later by a payment
    #                entry: Dr party / Cr bank).
    #   * CASH    -> the nature re-routes (capitalisable -> asset
    #                registration; consumable/operating -> paid expense).
    #   * UNKNOWN -> DEFER the route until the treatment is answered (an
    #                unanswered treatment must never pick a paid tool).
    #                Exception: the record_expense tool is by definition a
    #                PAID expense, so its default treatment is cash.
    nature = entities.get("transaction_nature")
    payment = entities.get("payment_method")
    _purchase_family = (
        "record_purchase", "record_cash_purchase", "record_credit_purchase",
    )
    if (
        intent == "record_expense" and payment is None
        and str(entities.get("settlement_position") or "").upper()
        != "SETTLE_EXISTING_PAYABLE"
    ):
        # CA treatment 3 guard: a settle-existing-payable answer must NOT
        # default to a paid expense — the settlement path handles it.
        payment = "CASH"

    if payment == "CREDIT" and intent in (
        "record_purchase", "record_cash_purchase", "record_expense",
    ):
        intent = "record_credit_purchase"
    elif (
        nature == "FIXED_ASSET" and payment == "CASH"
        and intent in (*_purchase_family, "record_expense")
    ):
        intent = "register_fixed_asset"
        if not entities.get("asset_name") and entities.get("item_description"):
            entities["asset_name"] = entities["item_description"]
    elif (
        nature in ("OPERATING_EXPENSE", "CONSUMABLE") and payment == "CASH"
        and intent in _purchase_family
    ):
        intent = "record_expense"

    # Work Stream R: a sale answered FIXED ASSET DISPOSAL is never a
    # revenue sale - it re-routes to the asset-disposal lifecycle (gain/
    # loss journal), with the named item becoming the asset reference.
    if entities.get("transaction_nature") == "ASSET_DISPOSAL" and intent in (
        "record_sale", "record_cash_sale", "record_credit_sale", "create_invoice",
    ):
        intent = "dispose_fixed_asset"
        if not entities.get("asset_name") and entities.get("item_description"):
            entities["asset_name"] = entities["item_description"]

    # 3c. WORK STREAM A - MANDATORY TRANSACTION-DATE PROTOCOL: validate the
    #     extracted/answered date through the deterministic parser (the LLM
    #     never parses dates).  A date that fails validation is dropped -
    #     never guessed - so the standardized date question joins the
    #     consolidated questionnaire below.  "yesterday"/"today" in the
    #     user's message were already resolved silently by _extract_date.
    raw_date = entities.get("transaction_date")
    if raw_date:
        parsed = parse_transaction_date(str(raw_date))
        if parsed.ok:
            entities["transaction_date"] = parsed.iso_date
        else:
            entities.pop("transaction_date", None)
            log.info(
                "planner.transaction_date_invalid",
                raw=str(raw_date)[:64],
                error=parsed.error,
            )

    # 4. Determine requirements
    requires_validation = intent not in (
        "generate_trial_balance", "generate_balance_sheet", "generate_profit_loss",
        "generate_cash_flow", "generate_general_ledger", "customer_balance",
        "supplier_balance", "project_profitability", "generate_customer_ledger",
        "generate_supplier_ledger",
    )
    requires_accounting = intent in _TRANSACTION_INTENTS
    requires_confirmation = intent in (
        "record_credit_purchase", "record_credit_sale", "create_invoice",
        "record_receipt", "record_payment",
        "create_credit_note", "create_purchase_return", "record_expense_payment",
        "record_bank_transfer", "create_bank_account",
        "register_fixed_asset", "dispose_fixed_asset",
        "record_asset_depreciation",
        # Quotation → invoice conversion posts a receivable journal, so the
        # user confirms before the accounting mutation (same as create_invoice).
        "convert_quotation",
    )

    # 5. ECONOMIC EVENT CLASSIFICATION — BEFORE tool selection.
    #    Determine what real-world event this request represents and which
    #    ERP dimensions it can affect.  Tools are then selected for the
    #    EVENT, not for the CRUD keyword.
    from app.reasoning import build_event_profile

    event_profile = build_event_profile(intent)

    # 6. Determine tools + context sources
    tools = _tools_for_intent(intent)
    context = _context_for_intent(intent)

    # 6. Clarification — DYNAMIC, MINIMAL and CONSOLIDATED:
    #    only genuinely-missing material info, gathered into ONE
    #    questionnaire (every independent gap in a single round — no
    #    drip-feeding one question per turn).  The grounded LLM prefill
    #    (step 2b) has already filled everything the user actually stated,
    #    so the questionnaire below contains ONLY genuine gaps.
    missing_fields = _missing_fields(intent, entities)
    clarification_qs = _questions_for_fields(intent, missing_fields, entities)
    clarification_needed = bool(clarification_qs)

    plan_obj = ExecutionPlan(
        intent=intent,
        entity_type=_entity_type(intent),
        entity_name=(
            entities.get("customer_name")
            or entities.get("supplier_name")
            or entities.get("project_name")
        ),
        required_context=context,
        potential_tools=tools,
        requires_validation=requires_validation,
        requires_accounting_engine=requires_accounting,
        requires_confirmation=requires_confirmation,
        requires_clarification=clarification_needed,
        clarification_questions=clarification_qs,
        missing_fields=missing_fields,
        expected_outcome=_outcome(intent),
        extracted_entities=entities,
        transaction_nature=entities.get("transaction_nature"),
        transaction_nature_source=nature_source,
        economic_event=event_profile.event.value,
        impact_map=dict(event_profile.affected),
        prohibited_actions=[p.as_dict() for p in event_profile.prohibited],
    )

    log.info(
        "planner.result",
        intent=intent,
        entity_name=plan_obj.entity_name,
        amount=entities.get("amount"),
        tools=tools,
        clarification=clarification_needed,
    )
    return plan_obj


# ---------------------------------------------------------------------------
# Clarification gating — the anti-loop core
# ---------------------------------------------------------------------------

# Field names that are material enough to clarify, mapped to the question
# asked for them.  A field appears here ONLY if no extraction pattern found
# it anywhere in the request (including prior clarification answers, which
# the Planner merges into the entity set before this runs).
_FIELD_QUESTION = {
    "amount": "What is the transaction amount?",
    "supplier_name": (
        "Who is the supplier? If this is a one-off purchase from a local "
        "vendor, reply LOCAL VENDOR and it will be recorded against a "
        "'Local Vendor' account (created automatically if it does not "
        "exist yet)."
    ),
    "customer_name": "Who is the customer?",
    # Work Stream R4.9 — invoice LINE-DETAIL.  The manual invoice form
    # mandates a line item (description + quantity); the AI path must ask
    # for both in the SAME consolidated round instead of silently
    # defaulting "Goods" x1.  Quantity is asked against the KNOWN total so
    # the unit price is derived, not guessed.
    "item_description": (
        "What item or service is being invoiced? (description for the "
        "invoice line — e.g. 'Web development services', 'HP laptops')"
    ),
    "quantity": (
        "How many units are you invoicing? (quantity — reply with just "
        "the number; the line total you gave is divided across the units)"
    ),
    "payment_type": "Was this paid in cash or on credit?",
    "asset_name": "Which asset is this about (asset name or code)?",
    # Work Stream A - the standardized date question (exact shape per the
    # protocol): joined to the consolidated questionnaire, never a
    # standalone round.
    "transaction_date": (
        "What is the transaction date? Reply TODAY, or the date as "
        "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
    ),
}


def _questions_for_fields(
    intent: str,
    fields: List[str],
    entities: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Per-field question text for the consolidated questionnaire.

    The nature/purpose question is FAMILY-SPECIFIC (Work Stream R): the
    question for a purchase (fixed asset vs inventory vs consumable vs
    service) differs from a sale, expense, settlement, return or
    quotation.  Work Stream R3: the generic expense path asks the
    PURPOSE question (what is it actually for?), the CONDITIONAL
    capitalization question and the settlement question instead of the
    4-way nature tree.  Every other field uses its standard
    `_FIELD_QUESTION` text.
    """
    ents = entities or {}
    questions: List[str] = []
    for f in fields:
        if f == "transaction_nature":
            questions.append(
                nature_question_for_intent(intent) or NATURE_DECISION_QUESTION
            )
        elif f == "transaction_purpose":
            questions.append(purpose_question())
        elif f == "capitalization_decision":
            questions.append(
                capitalization_question_for(str(ents.get("transaction_purpose") or ""))
            )
        elif f == "settlement_position":
            questions.append(SETTLEMENT_POSITION_QUESTION)
        elif f == "payment_type" and intent in (
            "record_receipt", "record_payment",
        ) or (
            f == "payment_type" and intent == "record_expense"
            and str(ents.get("settlement_position") or "").upper()
            == "SETTLE_EXISTING_PAYABLE"
        ):
            # Work Stream R4.4 — the settlement CHANNEL decides which
            # ledger the money hits (cash vs bank), so it is asked.
            if intent == "record_receipt":
                questions.append(
                    "Was this received in cash, or through the bank? "
                    "Reply CASH or BANK."
                )
            else:
                questions.append(
                    "Was this paid in cash, or through the bank? "
                    "Reply CASH or BANK."
                )
        elif f == "transaction_date" and intent == "record_expense":
            # R3.3: the underlying EVENT date, never the payment date.
            questions.append(EXPENSE_EVENT_DATE_QUESTION)
        else:
            questions.append(_FIELD_QUESTION[f])
    return questions


def _missing_fields(intent: str, entities: Dict[str, Any]) -> List[str]:
    """Return the material fields genuinely missing for *intent*.

    Rules (single source of truth for clarification):
    * Nature/purpose comes FIRST (Work Stream R): for EVERY intent that
      creates, moves or classifies value the nature (fixed asset vs
      inventory vs consumable vs expense vs settlement) is a user decision
      - never guessed, never suppressed by other missing fields.  A learned
      ``transaction_nature`` preference (or an explicit answer merged into
      *entities*) answers it.
    * Amount is only missing when NO extraction pattern found it anywhere
      (including prior clarification answers merged into *entities*).
    * Supplier is only required for credit purchases (a cash purchase
      does not need a named supplier).
    * Customer is only required for credit sales / invoices.
    * Cash-vs-credit treatment changes the accounting entries (payable vs
      immediate payment), so when NO payment keyword was found anywhere it
      is material and MUST be asked — never guessed.
    * Ordering is dependency-first (Work Stream R): nature/purpose ->
      cash-vs-credit -> party/asset reference -> amount -> transaction
      date (Work Stream A text unchanged, always last).
    """
    missing: List[str] = []

    # Work Stream R3 — the generic expense ladder (dependency-first):
    #   1. PURPOSE (what is it actually for?) replaces the 4-way nature
    #      tree for record_expense — the purpose answer DERIVES the nature.
    #   2. CAPITALIZE-OR-EXPENSE — only when the purpose is ambiguous
    #      (repairs / software / durable free text) AND the amount is at
    #      or above the org's capitalization threshold (small amounts
    #      auto-expense; never asked below it).
    #   3. SETTLEMENT — paid now (cash/bank) vs outstanding vs prepaid;
    #      the answer fills payment_method, so the step-3b routing and
    #      the party gate see a normal treatment.
    if intent == "record_expense":
        if not entities.get("transaction_purpose"):
            missing.append("transaction_purpose")
        else:
            needs_cap = purpose_requires_capitalization_question(
                str(entities["transaction_purpose"]),
                str(entities.get("item_description") or entities.get("description") or ""),
                entities.get("amount"),
                entities.get("capitalization_threshold")
                if entities.get("capitalization_threshold") is not None
                else capitalization_threshold_from_prefs(None),
            )
            if needs_cap and not entities.get("capitalization_decision"):
                missing.append("capitalization_decision")
        if not entities.get("payment_method"):
            if (
                str(entities.get("settlement_position") or "").upper()
                == "SETTLE_EXISTING_PAYABLE"
            ):
                # The treatment is known (settle a payable) — only the
                # settlement CHANNEL (cash vs bank ledger) is open.
                missing.append("payment_type")
            else:
                missing.append("settlement_position")

    # Work Stream R - the nature/purpose decision comes FIRST: it
    # re-routes the intent (fixed asset -> registration, consumable ->
    # expense, disposal -> asset disposal) and the treatment.  The
    # generic expense path is covered by the R3 ladder above (purpose
    # derives the nature) — never ask both.
    if (
        intent != "record_expense"
        and nature_family_for_intent(intent)
        and not entities.get("transaction_nature")
    ):
        missing.append("transaction_nature")

    if intent in ("record_purchase", "record_sale") and not entities.get("payment_method"):
        # Generic intent means no payment keyword was found anywhere —
        # the cash/credit treatment is genuinely unknown.
        missing.append("payment_type")

    # Work Stream R4.4/R4.6 — receipts/payments: the settlement CHANNEL
    # (cash vs bank) is ASKED, never defaulted — it decides which ledger
    # the money hits (R4.1: cash ledger is separate from the bank
    # ledger).  The PARTY is a NEXT-STAGE question: it is only material
    # when the business operation is an invoice/bill SETTLEMENT (whose
    # receivable/payable is being settled?) — an advance or loan does
    # not name a customer/supplier up front.
    if (
        intent == "record_receipt"
        and str(entities.get("transaction_nature") or "").upper() == "ALLOCATION"
        and not entities.get("customer_name")
    ):
        missing.append("customer_name")
    if (
        intent == "record_payment"
        and str(entities.get("transaction_nature") or "").upper() == "ALLOCATION"
        and not entities.get("supplier_name")
    ):
        missing.append("supplier_name")
    if intent in ("record_receipt", "record_payment") and not entities.get("payment_method"):
        missing.append("payment_type")

    if intent == "record_credit_purchase" and not entities.get("supplier_name"):
        missing.append("supplier_name")

    if intent in ("record_credit_sale", "create_invoice") and not entities.get("customer_name"):
        missing.append("customer_name")

    # Work Stream R4.9/R4.10 — invoice LINE-DETAIL (dependency-first):
    # the description and quantity are MATERIAL for invoices (parity law:
    # the manual form mandates a line item), so both are asked in the
    # same consolidated round — after the party, before amount/date —
    # and NEVER silently defaulted ("Goods" x1 was the old behaviour).
    # R4.10: when DISTINCT lines were already parsed from the request
    # (multi-item phrasing) the singular questions are suppressed —
    # every line already carries its own description/quantity/price.
    if intent in ("create_invoice", "record_credit_sale"):
        if not entities.get("line_items"):
            if not (
                entities.get("item_description") or entities.get("description")
            ):
                missing.append("item_description")
            if entities.get("quantity") is None:
                missing.append("quantity")

    # Asset lifecycle: the asset reference is material (which asset?), and
    # an asset acquisition without any payment keyword cannot assume
    # cash-vs-credit — the journal differs (payable vs immediate payment).
    if intent in (
        "register_fixed_asset", "dispose_fixed_asset",
        "record_asset_depreciation",
    ) and not (
        entities.get("item_description") or entities.get("asset_name")
    ):
        missing.append("asset_name")

    if intent == "register_fixed_asset" and not entities.get("payment_method"):
        missing.append("payment_type")

    if intent in _TRANSACTION_INTENTS and entities.get("amount") is None:
        missing.append("amount")

    # Work Stream A: the transaction date is material for EVERY mutation -
    # an absent or unparseable date is asked (never silently defaulted).
    if intent in DATE_REQUIRED_INTENTS and not entities.get("transaction_date"):
        missing.append("transaction_date")

    return missing


# ---------------------------------------------------------------------------
# Multi-question answer splitting — the consolidated questionnaire may be
# answered in ONE message ("1) cash 2) 150,000" or "cash, 150000").  Each
# part must be routed to ITS OWN question, or the second answer is lost and
# the same question is asked again (live defect).
# ---------------------------------------------------------------------------

_NUMBERED_Q_LINE = re.compile(r"^\s*\d+[.)]\s*(.+)$", re.MULTILINE)


def _split_numbered_questions(text: str) -> List[str]:
    """Return the individual questions of a numbered questionnaire."""
    return [m.strip() for m in _NUMBERED_Q_LINE.findall(text or "")]


def _split_answer_parts(answer: str) -> List[str]:
    """Split a user reply into one part per answered question.

    Handles "1) cash 2) 150000", newline-separated answers and
    comma/and-separated answers.  Digit-grouping commas ("2,000") are
    NEVER treated as separators.
    """
    text = (answer or "").strip()
    if not text:
        return []
    # Numbered parts ("1) cash 2) yes" / "1. cash\n2. yes")
    if re.search(r"(?:^|\s)1\s*[.)]\s*\S", text):
        parts = [
            p.strip(" ,;")
            for p in re.split(r"\s*\d+\s*[.)]\s*", text)
            if p.strip(" ,;")
        ]
        if len(parts) >= 2:
            return parts
    # Newline-separated parts
    if "\n" in text:
        parts = [p.strip(" ,;") for p in text.splitlines() if p.strip(" ,;")]
        if len(parts) >= 2:
            return parts
    # Comma / "and" separated parts ("cash, 150000" / "cash and 150000")
    segments = re.split(r",(?!\d)\s*|\s+and\s+", text)
    parts = [p.strip(" ,;") for p in segments if p.strip(" ,;")]
    return parts if len(parts) >= 2 else [text]


def _explode_multi_answers(
    qa_history: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Pair each part of a multi-question answer with its own question.

    A consolidated questionnaire ("...:\n1. Q1\n2. Q2") answered in one
    line ("a, b" / "1) a 2) b") becomes two (question, answer) pairs so
    the existing per-field matching resolves BOTH answers.
    """
    exploded: List[Dict[str, str]] = []
    for qa in qa_history or []:
        question = qa.get("question") or ""
        answer = (qa.get("answer") or "").strip()
        if not answer:
            continue
        sub_questions = _split_numbered_questions(question)
        parts = _split_answer_parts(answer)
        if len(sub_questions) >= 2 and len(parts) == len(sub_questions):
            for sq, part in zip(sub_questions, parts):
                exploded.append({"question": sq, "answer": part})
        elif (
            len(sub_questions) >= 2
            and len(parts) == len(sub_questions) + 1
            and re.match(r"supplier\s*:", parts[-1], re.IGNORECASE)
        ):
            # Work Stream R2 - the UI's CONDITIONAL inline party answer:
            # choosing CREDIT on the payment question reveals an extra
            # "N) supplier: X" part.  Pair the base parts with their
            # questions and route the trailing part to the supplier field.
            for sq, part in zip(sub_questions, parts[:-1]):
                exploded.append({"question": sq, "answer": part})
            value = re.split(
                r"supplier\s*:", parts[-1], maxsplit=1, flags=re.IGNORECASE
            )[1].strip()
            if value:
                exploded.append({
                    "question": "Who is the supplier?",
                    "answer": value,
                })
        else:
            exploded.append({"question": question, "answer": answer})
    return exploded


def _resolve_same_answers(
    qa_history: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Resolve "SAME" answers to the prior answer for the same question.

    The clarification-memory hint offers "reply SAME to reuse" for
    preference-shaped questions (Work Stream F).  The question text may
    carry the "(previously: ...)" hint suffix, so matching normalises it
    away.  A SAME with no resolvable prior is dropped (never guessed).
    """
    def _norm(q: str) -> str:
        return (q or "").split("(previously")[0].strip().lower()

    resolved: List[Dict[str, str]] = []
    for qa in qa_history or []:
        question = qa.get("question") or ""
        answer = (qa.get("answer") or "").strip()
        if answer.lower() == "same":
            prior = next(
                (
                    p.get("answer") or ""
                    for p in reversed(resolved)
                    if _norm(p.get("question")) == _norm(question)
                    and (p.get("answer") or "").strip().lower() != "same"
                ),
                None,
            )
            if prior:
                resolved.append({"question": question, "answer": prior})
            continue
        resolved.append({"question": question, "answer": answer})
    return resolved


def _merge_clarification_answers(
    entities: Dict[str, Any],
    qa_history: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Fold prior clarification answers into the entity set.

    Each answer fills the field its question asked about (matched by
    keyword), so a question is never repeated for the same conversation.
    Multi-question rounds answered in a single message are split first
    (see ``_explode_multi_answers``) so every part reaches its own field.
    "SAME" answers reuse the prior value for the same question (Work
    Stream F clarification memory).
    """
    merged = dict(entities)
    for qa in _explode_multi_answers(_resolve_same_answers(qa_history)):
        question = (qa.get("question") or "").lower()
        answer = (qa.get("answer") or "").strip()
        if not answer:
            continue

        # Work Stream R3 — PURPOSE ("What is this expense for?"): the
        # purpose answer is authoritative and DERIVES the transaction
        # nature (equipment -> FIXED_ASSET, resale goods -> INVENTORY,
        # everything else -> operating expense; ambiguous purposes wait
        # for the capitalization decision).  The purpose label also
        # becomes the item description so the classifier maps the right
        # expense account family.
        if "what is this expense for" in question:
            resolved_purpose = resolve_purpose_answer(question, answer)
            if resolved_purpose:
                merged["transaction_purpose"] = resolved_purpose
                if not merged.get("item_description"):
                    merged["item_description"] = purpose_label(resolved_purpose)
                continue

        # Work Stream R3.2 — capitalization decision: authoritative for
        # the ambiguous purposes (repairs / software / durable free text).
        if "ordinary expense or capitalized" in question:
            resolved_cap = resolve_capitalization_answer(question, answer)
            if resolved_cap:
                merged["capitalization_decision"] = resolved_cap
                continue

        # Work Stream R3.3 — settlement position: the CA-grade expense
        # treatments (paid now cash/bank · payable now · SETTLEMENT of an
        # expense already recorded as payable · prepaid) and the legacy
        # wording, so history answers always resolve.  Plain payables,
        # accruals, prepaids and payable-settlements stay distinguishable.
        if (
            "paid, or is it outstanding" in question
            or "expense debit, cash credit" in question
        ):
            resolved_settle = resolve_settlement_answer(question, answer)
            if resolved_settle:
                if resolved_settle == "ACCRUAL_PREPAID":
                    merged["settlement_position"] = "ACCRUAL_PREPAID"
                    merged["transaction_nature"] = "PREPAID_EXPENSE"
                    merged["payment_method"] = "CASH"
                elif resolved_settle == "SETTLE_EXISTING_PAYABLE":
                    # Treatment 3 — the SETTLEMENT of a previously
                    # recorded payable.  payment_method stays EMPTY so
                    # no paid tool ever fires; the reasoning path
                    # locates the outstanding payable, confirms it with
                    # the user, then settles it (Dr payables / Cr
                    # cash-bank) — never a second expense.
                    merged["settlement_position"] = "SETTLE_EXISTING_PAYABLE"
                else:
                    merged["payment_method"] = resolved_settle
                    if resolved_settle == "CREDIT":
                        merged["settlement_position"] = "OUTSTANDING"
                continue

        # OWNER POLICY — SETTLEMENT CHECK ("Settle this unpaid payable?"):
        # YES confirms the located payable (the deterministic settlement
        # call allocates the payment to it — Dr trade payables / Cr
        # cash-bank, never a second expense).  NO re-routes to a NEW
        # expense; a picked label (multi-bill list) confirms that bill.
        if "settlement check:" in question:
            low = answer.lower().strip(" .)'\"")
            if low.startswith(("no", "cancel", "record as new", "different")):
                merged["settlement_position"] = "OUTSTANDING"
                merged["payment_method"] = "CREDIT"
            else:
                merged["settle_confirmed"] = True
                if not low.startswith(("yes", "y", "ok")):
                    merged["settle_pick"] = answer.strip()
            continue

        # SETTLEMENT FALLBACK — no unpaid payable exists in the books, so
        # the (d) answer cannot be a settlement; the user re-picks the
        # treatment and the request re-reasons from there.
        if "settlement fallback:" in question:
            low = answer.lower().strip(" .)'\"")
            merged["settlement_position"] = None
            if low.startswith("paid now — cash"):
                merged["payment_method"] = "CASH"
            elif low.startswith("paid now — bank"):
                merged["payment_method"] = "BANK_TRANSFER"
            else:
                merged["payment_method"] = "CREDIT"
                merged["settlement_position"] = "OUTSTANDING"
            continue

        # Work Stream R4.4 — settlement channel for receipts/payments:
        # cash hits the CASH ledger, bank hits the BANK ledger (R4.1) —
        # the answer is authoritative and letter-tappable.
        if "in cash, or through the bank" in question:
            low = answer.lower().strip(" .)'\"")
            if low in ("a", "1", "cash"):
                merged["payment_method"] = "CASH"
                continue
            if low in ("b", "2", "bank", "bank transfer", "online", "transfer", "neft", "card"):
                merged["payment_method"] = "BANK_TRANSFER"
                continue

        # OWNER POLICY — ACCOUNT-CREATION CONFIRMATION ("Should I create
        # it, or do you want to use a specific existing account?"): the
        # answer is authoritative.  YES confirms the classifier's proposed
        # account (extracted from the question text) — execution runs
        # create_account first (free code resolved automatically) and the
        # tool-order guard blocks any default-account recording until it
        # succeeds.  NO keeps the entry open: any extra wording names the
        # existing account to use instead.
        if "should i create" in question:
            low = answer.lower().strip(" .)'\"")
            if low.startswith(("yes", "y", "create", "ok")):
                m = re.search(r"no '(.+?)' account", question)
                if m:
                    merged["create_account"] = m.group(1)
            else:
                named = re.sub(
                    r"^(no|n|use|pick|existing)\b[,.:;! ]*", "", low
                ).strip()
                if named and "existing account" not in named:
                    merged["account_name"] = named
            continue

        # Work Stream R: the nature/purpose decision tree is resolved
        # FIRST and FAMILY-AWARE ("b" means SERVICE in a sale round but
        # INVENTORY in a purchase round; a credit-note question contains
        # the word "credit" and must never be mistaken for the cash-vs-
        # credit question).  An unresolvable answer is left unset so the
        # planner re-asks - the nature is never guessed.
        if not merged.get("transaction_nature"):
            resolved_nature = resolve_nature_answer(question, answer)
            if resolved_nature:
                merged["transaction_nature"] = resolved_nature
                continue

        # Work Stream R2 - PARTY-RESOLUTION round ("Party check: ..."):
        # the answer either PICKS an existing party (kept verbatim so the
        # next search finds the EXACT match) or CONFIRMS creation of a new
        # party ledger.  Authoritative - it overrides any earlier value.
        # R4.5: the gate is party-kind aware (customers for receipts,
        # suppliers for payments/purchases).
        if "party check:" in question:
            low = answer.lower()
            is_customer = "customer" in question
            if low.startswith(("yes", "create", "ok")):
                merged["supplier_create_confirmed"] = True
                if is_customer:
                    merged["customer_create_confirmed"] = True
            elif is_customer:
                merged["customer_name"] = answer
            else:
                merged["supplier_name"] = answer
            continue

        # Work Stream R4.10 - CATALOG-CHECK round ("Catalog check: ..."):
        # one or more invoice lines are not in the product/service
        # catalog.  YES adds them (with the stated unit prices), NO keeps
        # them as one-off free-text lines.  Either way the invoice line
        # itself is never blocked — only the catalog linkage is decided.
        if "catalog check:" in question:
            low = answer.lower()
            if low.startswith(("yes", "add", "create", "ok")):
                merged["catalog_create_confirmed"] = True
            else:
                merged["catalog_skip"] = True
            continue

        # REVENUE-LEDGER round ("Revenue ledger check: ..."): reviews whether the
        # stream that was sold deserves its OWN revenue ledger.  YES creates a
        # dedicated child ledger under the revenue parent; any other answer names
        # the existing revenue account to use instead.  Either way the decision is
        # the user's — a sale is never silently booked to an arbitrary revenue
        # account (the defect that credited a mobile-phone sale to "Software
        # Development Revenue").
        if "revenue ledger check:" in question:
            low = answer.lower()
            if low.startswith(("yes", "create", "ok", "y")):
                merged["revenue_ledger_decision"] = "CREATE"
            else:
                merged["revenue_ledger_decision"] = "USE_EXISTING"
                named = answer.strip()
                if named:
                    merged["revenue_account_name"] = named
            continue

        # Work Stream R4.9/R4.10 — invoice LINE-DETAIL answers: the
        # description and quantity are asked in the consolidated round
        # and routed here.  A multi-item answer ("2 laptops at 5000 and
        # 3 mice at 500") parses into DISTINCT lines instead of one
        # description string.  The combined "quantity and price"
        # question (below) keeps its own parser — the plain-quantity
        # branch excludes it.
        if "item or service" in question and not merged.get("item_description"):
            multi = _extract_line_items(answer)
            if multi:
                merged["line_items"] = multi
                # The per-line answer is the most specific statement of
                # the transaction — it defines the total (Σ qty × price),
                # overriding any earlier round's header amount.
                merged["amount"] = _line_items_total(multi)
            else:
                merged["item_description"] = answer.strip()
        elif (
            "units are you invoicing" in question
            or ("quantity" in question and "price" not in question)
        ) and merged.get("quantity") is None:
            qty_val = _parse_bare_amount(answer)
            if qty_val is not None and qty_val > 0:
                merged["quantity"] = qty_val
        elif "amount" in question and merged.get("amount") is None:
            value = _parse_bare_amount(answer)
            if value is not None:
                merged["amount"] = value
        elif "supplier" in question and not merged.get("supplier_name"):
            low = answer.lower().strip()
            if "local vendor" in low or low in ("local", "no party", "none"):
                # Work Stream R2 - one-off local vendor: fold into the
                # standing 'Local Vendor' account (searched and created
                # on first use by the deterministic executor).
                merged["supplier_name"] = "Local Vendor"
            else:
                merged["supplier_name"] = answer
        elif "customer" in question and not merged.get("customer_name"):
            merged["customer_name"] = answer
        elif "date" in question and not merged.get("transaction_date"):
            # Work Stream A: route the answer through the deterministic
            # parser.  On failure keep the raw answer so the validation
            # in plan() drops it and the date is re-asked (never guessed).
            parsed = parse_transaction_date(answer)
            merged["transaction_date"] = (
                parsed.iso_date if parsed.ok else answer
            )
        elif ("payment" in question or "method" in question) and not merged.get("payment_method"):
            merged["payment_method"] = _normalize_payment_method(answer)
        elif (
            "cash" in question or "credit" in question or "paid" in question
        ) and not merged.get("payment_method"):
            # Cash-vs-credit question: accept ONLY a confident answer.  An
            # ambiguous reply is left unresolved so the planner re-asks —
            # the treatment must never be guessed.
            lower = answer.lower()
            if "credit" in lower or "on account" in lower or "pay later" in lower:
                merged["payment_method"] = "CREDIT"
            elif "cash" in lower:
                merged["payment_method"] = "CASH"
            elif "bank" in lower or "transfer" in lower or "online" in lower:
                merged["payment_method"] = "BANK_TRANSFER"
            elif "cheque" in lower or "check" in lower:
                merged["payment_method"] = "CHEQUE"
            elif "card" in lower:
                merged["payment_method"] = "CARD"
        elif (
            "quantity" in question and "price" in question
        ) and (
            merged.get("item_quantity") is None
            or merged.get("item_unit_price") is None
        ):
            # Line-detail question: parse "5 at 2,000 each" / "qty 3 @ 500".
            qty_m = re.search(
                r"(?:qty|quantity)?\s*(\d+(?:\.\d+)?)\s*(?:x|units?|pcs?|@|at)\s+"
                r"(?:rs\.?|pkr)?\s*([\d,]+(?:\.\d+)?)",
                answer, re.IGNORECASE,
            )
            if qty_m:
                try:
                    merged["item_quantity"] = float(qty_m.group(1))
                    merged["item_unit_price"] = float(
                        qty_m.group(2).replace(",", "")
                    )
                except ValueError:
                    pass
    return merged


def _parse_bare_amount(text: str) -> Optional[float]:
    """Parse a number from a bare clarification answer like '20,000'."""
    m = re.search(_NUM, text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _parse_bare_date(text: str) -> Optional[str]:
    """Parse a date from a bare clarification answer."""
    # Delegate to the full date extractor
    return _extract_date(text, text.lower())


def _normalize_payment_method(text: str) -> str:
    lower = text.lower()
    if "credit" in lower:
        return "CREDIT"
    if "cheque" in lower or "check" in lower:
        return "CHEQUE"
    if "card" in lower:
        return "CARD"
    if "bank" in lower or "transfer" in lower or "online" in lower:
        return "BANK_TRANSFER"
    return "CASH"


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _identify_intent(msg_lower: str) -> str:
    for intent, patterns in _INTENT_PATTERNS:
        for pat in patterns:
            if re.search(pat, msg_lower):
                return intent
    # Work Stream R3.1 — LOOSE expense phrasings.  A typo'd or unusual
    # expense wording ("record an explanation of 25000" — the observed
    # failure) contains no "expense" substring, so the strict patterns
    # miss it and the request used to fall to the generic model path,
    # which skipped the nature/purpose question entirely.  Route any
    # expense-shaped, typo-tolerant phrasing to the purpose-first ladder
    # instead — guarded by a 3+ digit figure so explanations without an
    # amount are never mis-routed.
    if re.search(r"\d{3,}", msg_lower) and re.search(
        r"expens|expence|expenz|explan|expln|spent|spend\b", msg_lower
    ):
        # Query-shaped expense mentions are lookups, never recordings
        # ("details of current month expences" with a typo'd spelling).
        if re.search(
            r"show|list|give|provide|tell|view|display|details?|summary|"
            r"summar\w*|breakdown|report|what|which|how much",
            msg_lower,
        ):
            return "list_expenses"
        return "record_expense"
    # Work Stream R4.4 — "received payment" WITHOUT a sale/purchase verb
    # is a customer receipt ("received payment of Rs.20,000"); a sale
    # phrasing ("sold ... and received payment") belongs to the sale
    # family and is handled by its own patterns above.
    if re.search(r"received\s+payment", msg_lower) and not re.search(
        r"\bsold\b|\bsale\b|\bsell\b|\bselling\b|\binvoice\b", msg_lower
    ):
        return "record_receipt"
    return "unknown"


def _refine_intent(intent: str, payment_method: Optional[str]) -> str:
    """Specialise a generic purchase/sale intent when the payment method is clear."""
    if intent == "record_purchase":
        if payment_method == "CREDIT":
            return "record_credit_purchase"
        if payment_method == "CASH":
            return "record_cash_purchase"
    elif intent == "record_sale":
        if payment_method == "CREDIT":
            return "record_credit_sale"
        if payment_method == "CASH":
            return "record_cash_sale"
    return intent


def _extract_entities(msg: str) -> Dict[str, str]:
    entities: Dict[str, str] = {}
    for field, patterns in (
        ("supplier_name", _SUPPLIER_PATTERNS),
        ("customer_name", _CUSTOMER_PATTERNS),
    ):
        for pat in patterns:
            m = pat.search(msg)
            if m:
                value = m.group(1).strip().rstrip(",.")
                # Ignore obviously non-name captures (pronouns, and any
                # capture led by a verb/document noun such as "selling").
                if _is_plausible_party(value):
                    entities[field] = value
                    break
    return entities


def _extract_item(msg: str) -> Optional[str]:
    for pat in _ITEM_PATTERNS:
        m = pat.search(msg)
        if m:
            item = m.group(1).strip()
            if item and item.lower() not in ("it", "something", "goods", "items", "stuff"):
                return item
    return None


# "<count> <item>" — the count that precedes the item name in the request.
_ITEM_QTY_RE = re.compile(
    r"\b(?P<qty>\d+(?:\.\d+)?)\s*"
    r"(?:x\s+|pcs\b\s*|units?\b\s*|pieces\b\s*|nos\b\s*)?"
    r"(?P<name>[a-zA-Z][\w\s&.\-/]*?)"
    r"(?=\s+(?:to|from|for|on|in|at|via)\b|[,.!?;]|\s*$)",
    re.IGNORECASE,
)


def _extract_item_quantity(msg: str, item: str) -> Optional[float]:
    """Return the count stated with the item phrasing, when there is one.

        "create an invoice for selling 2 ovens to lkj pvt limited" -> 2.0
        "sold 5 chairs"                                          -> 5.0

    The count used to be swallowed by the optional quantity group in
    ``_ITEM_PATTERNS`` and thrown away, so ``item_quantity`` was only ever
    populated from a clarification ANSWER — the agent therefore asked the
    user for a quantity that was already in the original request.

    The count is only accepted when the word following it is the SAME item
    the item extractor found, so an unrelated number in the sentence can
    never be mistaken for the line quantity.
    """
    target = (item or "").strip().lower()
    if not target:
        return None
    for m in _ITEM_QTY_RE.finditer(msg):
        name = (m.group("name") or "").strip().lower()
        if not name:
            continue
        if name == target or target.startswith(name) or name.startswith(target):
            try:
                qty = float(m.group("qty"))
            except (TypeError, ValueError):
                continue
            if qty > 0:
                return qty
    return None


# ---------------------------------------------------------------------------
# Work Stream R4.10 — MULTI-LINE ITEM parsing
# ---------------------------------------------------------------------------
# Users state several line items in ONE request ("2 laptops at 5000 each
# and 3 mice at 500", "1 laptop for 50,000, 2 keyboards for 2,000 each").
# The planner must recognise EACH line — quantity, description and unit
# price — instead of collapsing everything into one item + one amount.

_LINE_SPLIT_RE = re.compile(
    r"\s*(?:\n+|\band\b|,|;|\d+[\.\)]\s+)\s*", re.IGNORECASE
)
# Per segment:  QTY  NAME  (@|at|for|each|:)  UNIT-PRICE   [+ each/per unit]
# Used with .search() — no ^ anchor — so the line can sit mid-sentence.
_LINE_ITEM_RE = re.compile(
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?:x\b|pcs\b|units?\b|pieces\b|nos\b)?\s*"
    r"(?P<name>[a-zA-Z][\w\s&\.\-/]*?)\s*"
    r"(?:@|\bat\b|\bfor\b|\beach\b|:)\s*(?:rs\.?|pkr|₨)?\s*"
    r"(?P<price>[\d,]+(?:\.\d+)?)\s*(?:each|per\s*unit)?\s*$",
    re.IGNORECASE,
)


def _extract_line_items(text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse a request/answer into DISTINCT invoice lines.

    Recognised shapes per segment (case/spacing tolerant):

        2 laptops at 5000 each        3 x mouse @ 500
        1 laptop for 50,000           5 units keyboard: 2,000

    Returns ``None`` unless at least TWO complete lines parse (a single
    item keeps riding the singular item/quantity ladder).  Each line is
    ``{"description", "quantity", "unit_price"}``; line totals are left to
    ``validate_document_items`` so the SAME arithmetic runs everywhere.
    """
    if not text:
        return None
    # Protect thousands separators ("50,000" -> "50000") so the comma
    # separator can never split a number into two broken lines.
    normalized = re.sub(r"(?<=\d),(?=\d)", "", text)
    lines: List[Dict[str, Any]] = []
    for segment in _LINE_SPLIT_RE.split(normalized.strip()):
        segment = segment.strip()
        if not segment:
            continue
        # SEARCH (not match): the item phrase may sit mid-sentence after
        # "create invoice for ABC Traders for ..." — only the tail from
        # the quantity onward is the line.
        m = _LINE_ITEM_RE.search(segment)
        if not m:
            continue
        try:
            qty = float(m.group("qty"))
            price = float(m.group("price").replace(",", ""))
        except (TypeError, ValueError):
            continue
        name = m.group("name").strip(" -–:")
        if qty <= 0 or price < 0 or not name:
            continue
        # A bare "2 at 5000" (no name) is not an item line.
        if len(name) < 2:
            continue
        lines.append({
            "description": name,
            "quantity": qty,
            "unit_price": price,
        })
    if len(lines) < 2:
        return None
    return lines


# ---------------------------------------------------------------------------
# ATTACHED-DOCUMENT LINE ITEMS (vision extraction block)
# ---------------------------------------------------------------------------
# A document upload reaches the planner as FACTUAL CONTEXT appended to the
# message ("[Attached document information]") with an authoritative header
# (Supplier / Subtotal / Discount / Total) and one row per line:
#
#     Line 1: Surf Excel 5 kg | qty 20 | unit 600.0 | amount 12600.0
#
# Without this parser the document collapsed to ONE line carrying whichever
# amount the header patterns happened to match first (Line 1's amount), so a
# 7-line GRN was recorded as a single-line bill for the first row and the
# model had to reconcile the difference in a giant, timeout-prone round trip.
# The printed line TOTAL is preferred over qty x unit_price because the
# document's own rows are summed into its stated subtotal - deriving the unit
# price from the line total keeps the recorded lines tied to the document
# (parity law: the header can never disagree with its own lines).
_DOC_LINE_RE = re.compile(
    r"^[ \t]*line[ \t]*\d+[ \t]*:[ \t]*(?P<desc>.+?)"
    r"(?:[ \t]*\|[ \t]*qty[ \t]*(?P<qty>[\d,]+(?:\.\d+)?))?"
    r"(?:[ \t]*\|[ \t]*unit[ \t]*(?P<unit>[\d,]+(?:\.\d+)?))?"
    r"(?:[ \t]*\|[ \t]*amount[ \t]*(?P<amount>[\d,]+(?:\.\d+)?))?"
    r"[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_DOC_SUPPLIER_RE = re.compile(
    r"^[ \t]*supplier[ \t]*:[ \t]*(?P<name>.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Column headings / table furniture can never be a party ledger name.
_JUNK_PARTY_NAMES = {
    "line", "lines", "line item", "line items", "item", "items",
    "description", "qty", "quantity", "unit", "amount", "total",
    "subtotal", "discount", "tax", "unit price", "price", "particulars",
}


def _doc_number(raw: Optional[str]) -> Optional[float]:
    """Parse a document row/header number, tolerating separators."""
    if not raw:
        return None
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _extract_document_lines(
    text: str,
) -> tuple:
    """Parse the ATTACHED-DOCUMENT block into DISTINCT lines + supplier.

    Returns ``(line_items | None, supplier_name | None)``.  Line items are
    ``{"description", "quantity", "unit_price"}`` - the same shape the
    multi-item phrasing parser produces, so every downstream consumer
    (fast paths, validation, the document services) is unchanged.  Nothing is
    ever invented: a row is only kept when its quantity and total/unit price
    parse into sane numbers.
    """
    if not text or "[Attached document information]" not in text:
        return None, None
    supplier = None
    m = _DOC_SUPPLIER_RE.search(text)
    if m:
        name = (m.group("name") or "").strip(" .;'\"")
        if name and name.lower() not in _JUNK_PARTY_NAMES:
            supplier = name
    lines: List[Dict[str, Any]] = []
    for match in _DOC_LINE_RE.finditer(text):
        desc = (match.group("desc") or "").strip(" -:|.")
        # A description may still carry a trailing column fragment; keep only
        # the leading human-readable part.
        desc = re.split(r"\s*\|\s*", desc)[0].strip()
        if len(desc) < 2:
            continue
        qty = _doc_number(match.group("qty"))
        unit = _doc_number(match.group("unit"))
        amount = _doc_number(match.group("amount"))
        # Printed line total wins (derives a unit price that ties the line to
        # the document); otherwise quantity x unit price.
        if amount is not None and qty and qty > 0:
            unit_price = round(amount / qty, 2)
        elif qty and qty > 0 and unit is not None:
            unit_price = round(unit, 2)
        else:
            continue
        if qty is None or qty <= 0 or unit_price < 0:
            continue
        lines.append({
            "description": desc,
            "quantity": qty,
            "unit_price": unit_price,
        })
    return (lines or None), supplier


def _line_items_total(lines: List[Dict[str, Any]]) -> float:
    """Σ (quantity × unit_price) over parsed line items."""
    return round(
        sum(float(l["quantity"]) * float(l["unit_price"]) for l in lines), 2
    )


def _extract_amount(msg: str) -> Optional[float]:
    """Try every amount format, highest-confidence pattern first."""
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(msg)
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", ""))
        except (ValueError, IndexError):
            continue
        # Apply multiplier suffix if the pattern captured one
        if pat.groups >= 2:
            suffix = m.group(2)
            if suffix:
                value *= _AMOUNT_MULTIPLIERS.get(suffix.lower(), 1)
        if value > 0:
            return value
    return None


def _extract_payment_method(msg_lower: str) -> Optional[str]:
    for pat, method in _PAYMENT_METHOD_PATTERNS:
        if pat.search(msg_lower):
            return method
    return None


def _extract_date(msg: str, msg_lower: str) -> Optional[str]:
    """Extract a transaction date as ISO ``YYYY-MM-DD`` (best effort)."""
    # ISO format
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", msg)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # DD/MM/YYYY or DD/MM/YY (Pakistan convention: day first)
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b", msg)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            if y < 100:
                y += 2000
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # "15 aug", "15th August 2026"
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?\s*(\d{4})?\b", msg_lower
    )
    if m and m.group(2)[:3] in _MONTHS:
        d = int(m.group(1))
        mo = _MONTHS[m.group(2)[:3]]
        y = int(m.group(3)) if m.group(3) else date.today().year
        if 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # "aug 15", "August 15th"
    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", msg_lower)
    if m and m.group(1)[:3] in _MONTHS:
        d = int(m.group(2))
        mo = _MONTHS[m.group(1)[:3]]
        if 1 <= d <= 31:
            return f"{date.today().year:04d}-{mo:02d}-{d:02d}"

    # Relative keywords
    if "day before yesterday" in msg_lower:
        return (date.today() - timedelta(days=2)).isoformat()
    if "yesterday" in msg_lower:
        return (date.today() - timedelta(days=1)).isoformat()
    if "tomorrow" in msg_lower:
        return (date.today() + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b", msg_lower):
        return date.today().isoformat()
    return None


def _entity_type(intent: str) -> Optional[str]:
    if "customer" in intent or "sale" in intent or "invoice" in intent or "receipt" in intent:
        return "customer"
    if "supplier" in intent or "purchase" in intent or "payment" in intent:
        return "supplier"
    if "project" in intent:
        return "project"
    if "account" in intent:
        return "account"
    return None


def _tools_for_intent(intent: str) -> List[str]:
    mapping = {
        "create_quotation": ["search_customer", "get_customer", "create_quotation"],
        "convert_quotation": ["search_customer", "get_customer", "get_invoice", "convert_quotation", "prepare_journal", "validate_journal"],
        "create_credit_note": ["search_customer", "get_customer", "create_credit_note", "prepare_journal", "post_journal"],
        "create_purchase_return": ["search_supplier", "get_supplier", "create_purchase_return", "prepare_journal", "post_journal"],
        "record_expense_payment": ["search_supplier", "record_expense_payment", "prepare_journal", "post_journal"],
        "create_invoice": ["search_customer", "get_customer", "create_invoice", "prepare_journal", "validate_journal"],
        "record_credit_purchase": ["search_supplier", "get_supplier", "create_supplier", "search_account", "create_purchase_bill", "prepare_journal", "validate_journal", "post_journal"],
        "record_cash_purchase": ["search_account", "create_purchase_bill", "prepare_journal", "validate_journal", "post_journal"],
        "record_credit_sale": ["search_customer", "get_customer", "create_customer", "search_account", "create_invoice", "prepare_journal", "validate_journal", "post_journal"],
        "record_cash_sale": ["search_customer", "record_cash_sale"],
        "record_expense": ["search_account", "classify_expense", "create_expense", "prepare_journal", "validate_journal", "post_journal"],
        "record_receipt": ["search_customer", "get_customer", "list_bank_accounts", "record_customer_receipt", "prepare_journal", "post_journal"],
        "record_payment": ["search_supplier", "get_supplier", "list_bank_accounts", "record_supplier_payment", "prepare_journal", "post_journal"],
        "record_bank_transfer": ["list_bank_accounts", "record_bank_transfer", "prepare_journal", "post_journal"],
        "create_bank_account": ["create_bank_account"],
        "list_bank_accounts": ["list_bank_accounts"],
        "record_purchase": ["search_supplier", "search_account", "create_purchase_bill", "prepare_journal"],
        "record_sale": ["search_customer", "search_account", "create_invoice", "prepare_journal"],
        "register_fixed_asset": ["search_fixed_asset", "search_supplier", "search_account", "register_fixed_asset"],
        "dispose_fixed_asset": ["search_fixed_asset", "search_account", "dispose_fixed_asset"],
        "record_asset_depreciation": ["search_fixed_asset", "search_account", "record_asset_depreciation"],
        "create_product": ["search_product", "create_product"],
        "create_service": ["search_service", "create_service"],
        "customer_balance": ["search_customer", "get_customer", "get_customer_ledger"],
        "supplier_balance": ["search_supplier", "get_supplier", "get_supplier_ledger"],
        "generate_trial_balance": ["get_trial_balance"],
        "generate_balance_sheet": ["get_balance_sheet"],
        "generate_profit_loss": ["get_profit_loss"],
        "generate_cash_flow": ["get_cash_flow"],
        "generate_general_ledger": ["get_general_ledger"],
        "generate_customer_ledger": ["search_customer", "get_customer_ledger"],
        "generate_supplier_ledger": ["search_supplier", "get_supplier_ledger"],
        "list_expenses": ["list_expenses"],
        "project_profitability": ["search_project", "get_project_profitability"],
    }
    return mapping.get(intent, [])


def _context_for_intent(intent: str) -> List[str]:
    mapping = {
        "create_quotation": ["customer_master"],
        "convert_quotation": ["customer_master", "chart_of_accounts", "accounting_periods"],
        "create_credit_note": ["customer_master", "chart_of_accounts", "accounting_periods"],
        "create_purchase_return": ["supplier_master", "chart_of_accounts", "accounting_periods"],
        "record_expense_payment": ["supplier_master", "bank_accounts", "accounting_periods"],
        "create_invoice": ["customer_master", "chart_of_accounts", "accounting_periods"],
        "record_credit_purchase": ["supplier_master", "chart_of_accounts", "accounting_periods"],
        "record_cash_purchase": ["chart_of_accounts", "accounting_periods"],
        "record_credit_sale": ["customer_master", "chart_of_accounts", "accounting_periods"],
        "record_cash_sale": ["chart_of_accounts", "accounting_periods"],
        "record_expense": ["chart_of_accounts", "accounting_periods"],
        "record_receipt": ["customer_master", "bank_accounts", "invoices", "accounting_periods"],
        "record_payment": ["supplier_master", "bank_accounts", "purchase_bills", "accounting_periods"],
        "record_bank_transfer": ["bank_accounts", "accounting_periods"],
        "register_fixed_asset": ["chart_of_accounts", "supplier_master", "bank_accounts", "accounting_periods"],
        "dispose_fixed_asset": ["chart_of_accounts", "bank_accounts", "accounting_periods"],
        "record_asset_depreciation": ["chart_of_accounts", "accounting_periods"],
        "create_product": ["chart_of_accounts"],
        "create_service": ["chart_of_accounts"],
        "create_bank_account": ["chart_of_accounts"],
        "list_bank_accounts": ["bank_accounts"],
        "customer_balance": ["customer_master", "customer_ledger", "invoices", "receipts"],
        "supplier_balance": ["supplier_master", "supplier_ledger", "purchase_bills", "payments"],
        "generate_trial_balance": ["trial_balance", "accounting_periods"],
        "generate_balance_sheet": ["balance_sheet", "accounting_periods"],
        "generate_profit_loss": ["income_statement", "accounting_periods"],
        "generate_cash_flow": ["cash_flow", "balance_sheet", "income_statement", "accounting_periods"],
        "project_profitability": ["projects", "general_ledger", "accounting_periods"],
    }
    return mapping.get(intent, [])


def _outcome(intent: str) -> str:
    outcomes = {
        "create_quotation": "Sales quotation created and ready to send",
        "convert_quotation": "Quotation converted to a sales invoice with posted receivable journal",
        "create_credit_note": "Credit note created reducing customer receivable",
        "create_purchase_return": "Purchase return created reducing supplier payable",
        "record_expense_payment": "Expense/bill payment recorded",
        "create_invoice": "Sales invoice created with journal entry",
        "record_credit_purchase": "Purchase bill and payable recorded",
        "record_cash_purchase": "Purchase recorded as cash expense",
        "record_credit_sale": "Credit sale and receivable recorded",
        "record_cash_sale": "Cash sale recorded",
        "record_expense": "Expense recorded with journal entry",
        "record_receipt": "Customer receipt recorded with journal entry and allocation",
        "record_payment": "Supplier payment recorded with journal entry and allocation",
        "record_bank_transfer": "Bank-to-bank transfer recorded with journal entry",
        "register_fixed_asset": "Fixed asset registered and capitalised with journal entry",
        "dispose_fixed_asset": "Fixed asset disposed of with gain/loss journal entry",
        "record_asset_depreciation": "Asset depreciation recorded with journal entry",
        "create_product": "Product added to the catalog",
        "create_service": "Service added to the catalog",
        "create_bank_account": "Bank account created with linked GL account",
        "list_bank_accounts": "Bank account list retrieved",
        "customer_balance": "Customer outstanding balance calculated",
        "supplier_balance": "Supplier outstanding balance calculated",
        "generate_trial_balance": "Trial balance report generated",
        "generate_balance_sheet": "Balance sheet generated",
        "generate_profit_loss": "Profit & Loss statement generated",
        "list_expenses": "Expense details retrieved",
    }
    return outcomes.get(intent, "Operation completed")
