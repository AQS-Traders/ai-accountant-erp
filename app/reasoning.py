"""
ERP AI Agent — Requirement Reasoning & Dependency Resolution
=============================================================
Generic 360° requirement analysis for the agent's cognitive pipeline:

    REQUEST UNDERSTANDING
    → 360° IMPACT ANALYSIS
    → REQUIREMENT GAP ANALYSIS
    → TARGETED ERP CONTEXT ACQUISITION
    → DEPENDENCY GRAPH
    → RESOLUTION OF EACH DEPENDENCY
    → CONSOLIDATED CLARIFICATION (one questionnaire, not one drip-fed question)
    → RE-EVALUATION → PLAN → CONFIRMATION → EXECUTION

Design principles enforced here (ERP Agent Constitution-compatible):

* An empty database result is INFORMATION ("the record does not exist"),
  never a database failure.  Result states are classified explicitly —
  NOT_QUERIED / EMPTY_RESULT / FOUND / MULTIPLE_MATCHES / QUERY_FAILURE …
* 360° analysis is mandatory; 360° questioning is optional.  Only
  MISSING_AND_REQUIRED user decisions, genuine ambiguity, and
  configuration gaps become questions — everything deterministically
  resolvable is resolved silently.
* No arbitrary fallbacks: an ambiguous party match is surfaced, never
  silently resolved to "first hit".
* All question texts are consolidated into ONE questionnaire per round so
  the user answers every independent gap at once (pending-operation state
  is preserved by the existing clarification-history mechanism).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Requirement resolution states (the complete state vocabulary)
# ---------------------------------------------------------------------------


class FieldResolutionState(str, Enum):
    """How a required dimension of the request was resolved.

    Query-outcome states (a query result is information, never an
    exception — an empty result means "no matching record exists"):

    * NOT_QUERIED            — the ERP was never asked
    * EMPTY_RESULT           — queried; no matching record exists
    * FOUND                  — queried; exactly one suitable record
    * MULTIPLE_MATCHES       — queried; several candidates (possible ambiguity)

    Requirement states:

    * KNOWN_FROM_USER              — provided in the request / a prior answer
    * KNOWN_FROM_CONTEXT           — carried by the session/document context
    * DETERMINISTICALLY_RESOLVABLE — code/config can decide (dates, codes…)
    * RESOLVABLE_FROM_ERP          — an existing ERP record satisfies it
    * MISSING_BUT_OPTIONAL         — absent but NOT required for this transaction
    * MISSING_AND_REQUIRED         — absent and the user must decide
    * AMBIGUOUS                    — present but materially ambiguous
    * INVALID                      — present but fails validation/business rules
    * BLOCKED_BY_CONFIGURATION     — ERP configuration gap (never invent)
    * BLOCKED_BY_DEPENDENCY        — a prerequisite dependency is unresolved
    """

    NOT_QUERIED = "NOT_QUERIED"
    EMPTY_RESULT = "EMPTY_RESULT"
    FOUND = "FOUND"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    QUERY_FAILURE = "QUERY_FAILURE"
    PERMISSION_FAILURE = "PERMISSION_FAILURE"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"

    KNOWN_FROM_USER = "KNOWN_FROM_USER"
    KNOWN_FROM_CONTEXT = "KNOWN_FROM_CONTEXT"
    DETERMINISTICALLY_RESOLVABLE = "DETERMINISTICALLY_RESOLVABLE"
    RESOLVABLE_FROM_ERP = "RESOLVABLE_FROM_ERP"
    MISSING_BUT_OPTIONAL = "MISSING_BUT_OPTIONAL"
    MISSING_AND_REQUIRED = "MISSING_AND_REQUIRED"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"
    BLOCKED_BY_CONFIGURATION = "BLOCKED_BY_CONFIGURATION"
    BLOCKED_BY_DEPENDENCY = "BLOCKED_BY_DEPENDENCY"


# ---------------------------------------------------------------------------
# Item intake protocol — capital-vs-expense-vs-inventory decision tree
# ---------------------------------------------------------------------------

# Configurable capitalisation threshold: an item at or above this amount in
# a purchase/sale request is treated as PLAUSIBLY capital (asking is cheap;
# a wrong capitalisation or expense is not).
CAPITAL_AMOUNT_THRESHOLD = 50_000.0

# Durable/capital-goods indicators (kept in step with the classifier's
# _DURABLE_GOODS vocabulary).
_CAPITAL_ITEM_KEYWORDS = (
    "machinery", "machine", "vehicle", "van", "car", "motorbike", "motorcycle",
    "forklift", "generator", "equipment", "furniture", "desk", "chair",
    "cabinet", "laptop", "notebook", "desktop", "computer", "printer",
    "scanner", "monitor", "server", "camera", "smartphone", "phone",
    "air conditioner", "ac unit", "building",
)

# Intents where a named physical item/service triggers the intake protocol.
_ITEM_INTAKE_INTENTS = {
    "record_purchase", "record_cash_purchase", "record_credit_purchase",
    "record_sale", "record_cash_sale", "record_credit_sale",
    "create_invoice", "create_purchase_bill", "record_expense",
}

# The consolidated four-way nature question (asked, NEVER guessed):
NATURE_DECISION_QUESTION = (
    "Is this item: (a) a FIXED ASSET (long-term use, to be capitalised), "
    "(b) an INVENTORY-STOCKED PRODUCT (resale stock — catalog flag only, "
    "quantities are not tracked), (c) a CONSUMABLE / one-off EXPENSE "
    "(expensed immediately), or (d) a SERVICE? "
    "Please answer with a, b, c or d."
)


# Work Stream R4.2 — receipts/payments ask WHICH BUSINESS OPERATION the
# money belongs to (the CA question: what is this settlement really
# settling?).  Same wire values as before, so routing and preference
# learning are unchanged.
OPERATION_QUESTION = (
    "Which business operation is this for? Reply with a, b or c: "
    "(a) Settlement AGAINST AN INVOICE or BILL — collecting or paying a "
    "specific receivable/payable; "
    "(b) An ADVANCE — money received or paid BEFORE the invoice exists; "
    "(c) A LOAN, OWNER DRAWING or OTHER settlement — not against any "
    "specific invoice."
)


def _is_plausibly_capital(
    item: str, entities: Dict[str, Any]
) -> bool:
    """Deterministic capital-plausibility test (no DB, no LLM)."""
    text = str(item or "").lower()
    if any(k in text for k in _CAPITAL_ITEM_KEYWORDS):
        return True
    # Standalone "ac"/"a/c"/"acs"/"hvac" are everyday durable abbreviations
    # ("I buy 2 ac" is a capital candidate, NOT an assumed expense).
    # Word-boundary regex — the substring keyword list above must never
    # take bare "ac" (it would match "account", "trace", …).
    if re.search(r"\ba/?c\b|\bacs\b|hvac|air.?condition", text, re.I):
        return True
    amount = entities.get("amount")
    try:
        return amount is not None and float(amount) >= CAPITAL_AMOUNT_THRESHOLD
    except (TypeError, ValueError):
        return False

# ---------------------------------------------------------------------------
# Work Stream R - CA-GRADE NATURE/PURPOSE MATRIX (purpose -> nature -> info)
# ---------------------------------------------------------------------------
# A chartered accountant establishes the PURPOSE of a transaction FIRST,
# because the nature (fixed asset vs inventory vs consumable vs expense vs
# settlement) changes the ENTIRE accounting treatment (capitalise vs stock
# vs expense account vs liability allocation).  One deterministic question
# per transaction family, asked in the FIRST consolidated round for EVERY
# intent that creates, moves or classifies value - never suppressed by a
# missing amount/date/party, and NEVER guessed by the LLM.

_NATURE_MATRIX: Dict[str, Dict[str, Any]] = {
    "purchase": {
        "intents": {
            "record_purchase", "record_cash_purchase", "record_credit_purchase",
            "create_purchase_bill",
        },
        "question": NATURE_DECISION_QUESTION,
        "marker": "is this item:",
        "values": ("FIXED_ASSET", "INVENTORY", "OPERATING_EXPENSE", "SERVICE"),
        "options": (
            {"value": "a", "label": "Fixed asset"},
            {"value": "b", "label": "Inventory (resale stock)"},
            {"value": "c", "label": "Consumable / expense"},
            {"value": "d", "label": "Service"},
        ),
        "keywords": (
            (r"fixed\s*asset|capitalis|capitaliz|long.term", "FIXED_ASSET"),
            (r"inventory|stock|resale", "INVENTORY"),
            (r"consumable|expense|expensed", "OPERATING_EXPENSE"),
            (r"service", "SERVICE"),
        ),
    },
    "sale": {
        "intents": {
            "record_sale", "record_cash_sale", "record_credit_sale",
            "create_invoice",
        },
        "question": (
            "What is the nature of this sale: (a) GOODS (a stock item you "
            "sell), (b) a SERVICE, (c) a FIXED ASSET DISPOSAL (selling an "
            "asset you own), or (d) OTHER INCOME? Please answer with a, b, "
            "c or d."
        ),
        "marker": "nature of this sale",
        "values": ("GOODS", "SERVICE", "ASSET_DISPOSAL", "OTHER_INCOME"),
        "options": (
            {"value": "a", "label": "Goods"},
            {"value": "b", "label": "Service"},
            {"value": "c", "label": "Fixed-asset disposal"},
            {"value": "d", "label": "Other income"},
        ),
        "keywords": (
            (r"disposal|disposed|write.?off|scrap|asset", "ASSET_DISPOSAL"),
            (r"other\s*income|income", "OTHER_INCOME"),
            (r"good|stock|product|item", "GOODS"),
            (r"service", "SERVICE"),
        ),
    },
    "expense": {
        "intents": {"record_expense"},
        "question": (
            "What is the nature of this expense: (a) an OPERATING EXPENSE "
            "(day-to-day running cost), (b) a PREPAID EXPENSE (paid in "
            "advance), (c) a CAPITALISABLE ITEM (a fixed asset to be "
            "capitalised), or (d) an OWNER DRAWING (personal, not "
            "business)? Please answer with a, b, c or d."
        ),
        "marker": "nature of this expense",
        "values": (
            "OPERATING_EXPENSE", "PREPAID_EXPENSE", "FIXED_ASSET",
            "OWNER_DRAWING",
        ),
        "keywords": (
            (r"fixed\s*asset|capitalis|capitaliz", "FIXED_ASSET"),
            (r"prepaid|prepay", "PREPAID_EXPENSE"),
            (r"owner|drawing|personal", "OWNER_DRAWING"),
            (r"operating|expense|day.to.day", "OPERATING_EXPENSE"),
        ),
    },
    "settlement": {
        "intents": {"record_receipt", "record_payment"},
        "question": OPERATION_QUESTION,
        "marker": "business operation is this for",
        "values": ("ALLOCATION", "ADVANCE", "LOAN_OR_SETTLEMENT"),
        "keywords": (
            (r"invoice|bill|against|allocation", "ALLOCATION"),
            (r"advance", "ADVANCE"),
            (r"loan|drawing|settlement|other", "LOAN_OR_SETTLEMENT"),
        ),
    },
    "return": {
        "intents": {"create_credit_note", "create_purchase_return"},
        "question": (
            "What is the nature of this credit note or return: (a) a RETURN "
            "OF GOODS, (b) a PRICE ADJUSTMENT (correction or discount, no "
            "goods returned), or (c) a SERVICE REVERSAL? Please answer with "
            "a, b or c."
        ),
        "marker": "nature of this credit note",
        "values": ("RETURN_OF_GOODS", "PRICE_ADJUSTMENT", "SERVICE_REVERSAL"),
        "options": (
            {"value": "a", "label": "Return of goods"},
            {"value": "b", "label": "Price adjustment"},
            {"value": "c", "label": "Service reversal"},
        ),
        "keywords": (
            (r"return of goods|goods|item|damaged|defective", "RETURN_OF_GOODS"),
            (r"price|discount|adjustment|correction", "PRICE_ADJUSTMENT"),
            (r"service", "SERVICE_REVERSAL"),
        ),
    },
    "quotation": {
        "intents": {"create_quotation"},
        "question": (
            "Is this quotation for (a) GOODS (stock items) or (b) a "
            "SERVICE? Please answer with a or b."
        ),
        "marker": "is this quotation for",
        "values": ("GOODS", "SERVICE"),
        "options": (
            {"value": "a", "label": "Goods"},
            {"value": "b", "label": "Service"},
        ),
        "keywords": (
            (r"good|stock|product|item", "GOODS"),
            (r"service", "SERVICE"),
        ),
    },
}

_NATURE_FAMILY_BY_INTENT: Dict[str, str] = {
    intent: family
    for family, spec in _NATURE_MATRIX.items()
    for intent in spec["intents"]
}


def nature_family_for_intent(intent: str) -> Optional[str]:
    """Which nature/purpose family (if any) the intent belongs to."""
    return _NATURE_FAMILY_BY_INTENT.get(intent or "")


def nature_question_for_intent(intent: str) -> Optional[str]:
    """The deterministic nature/purpose question for *intent* (or None)."""
    family = nature_family_for_intent(intent)
    return _NATURE_MATRIX[family]["question"] if family else None


def nature_question_intents() -> set:
    """Every intent whose questionnaire includes the nature/purpose
    question (every intent that creates, moves or classifies value)."""
    return set(_NATURE_FAMILY_BY_INTENT)


def resolve_nature_answer(question: str, answer: str) -> Optional[str]:
    """Deterministically map a nature-question answer to its canonical value.

    Accepts the option letter ("a"-"d") or the option wording ("fixed
    asset", "resale stock", "advance", ...).  Returns None for an answer
    that cannot be confidently resolved - the planner re-asks; the nature
    is NEVER guessed.
    """
    q = (question or "").lower()
    family = next(
        (f for f, spec in _NATURE_MATRIX.items() if spec["marker"] in q),
        None,
    )
    if not family:
        return None
    spec = _NATURE_MATRIX[family]
    text = (answer or "").strip().lower().strip(" .)")
    if text in ("a", "b", "c", "d"):
        idx = "abcd".index(text)
        return spec["values"][idx] if idx < len(spec["values"]) else None
    for pattern, value in spec["keywords"]:
        if re.search(pattern, text):
            return value
    return None





# ---------------------------------------------------------------------------
# Work Stream R3.1 — PURPOSE-FIRST taxonomy for the generic expense path
# ---------------------------------------------------------------------------
# An "expense" utterance ("record an expense of 25000") is the user's
# DESCRIPTION of an event, NOT an accounting classification.  Before any
# account is chosen the agent asks WHAT THE MONEY WAS FOR, from a finite
# tap-to-answer option list.  The purpose answer drives:
#   * the expense account family (the classifier maps the purpose label),
#   * the capitalization question (R3.2 — ambiguous purposes only),
#   * the transaction_nature consumed by the routing in planner step 3b.

@dataclass(frozen=True)
class PurposeOption:
    value: str           # canonical purpose value (entity transaction_purpose)
    label: str           # user-facing label (also the description autofill)
    keywords: str        # regex matching free-text answers to this option
    nature: str          # transaction_nature derived when unambiguous
    capital_class: str   # EXPENSE | INVENTORY | CAPITAL | AMBIGUOUS | DURABLE_ELSE


# Master list (R3.1) — every option ships to the UI as a tappable chip.
_PURPOSE_OPTIONS: List[PurposeOption] = [
    PurposeOption(
        "RENT", "Rent", r"rent|lease",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "ELECTRICITY", "Electricity / utilities",
        r"electric|utilit|gas|water|internet|phone|telecom|bill",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "SALARIES", "Salaries & wages",
        r"salari|wage|payroll|staff pay|bonus",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "ADVERTISING", "Advertising & marketing",
        r"advert|marketing|promotion|banner|social media",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "LEGAL_PROFESSIONAL", "Legal & professional fees",
        r"legal|lawyer|advocate|audit|professional fee|consult",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "REPAIRS_MAINTENANCE", "Repairs & maintenance",
        r"repair|maintenance|overhaul|renovat|upkeep|servicing",
        "OPERATING_EXPENSE", "AMBIGUOUS",
    ),
    PurposeOption(
        "TRAVEL", "Travel",
        r"travel|fare|hotel|lodging|air ?ticket|conveyance",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "INSURANCE", "Insurance",
        r"insur|premium",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "INTEREST_FINANCE", "Interest / finance cost",
        r"interest|finance cost|bank charge|loan charge",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "INVENTORY_PURCHASE", "Inventory — goods purchased for resale",
        r"resale|stock|inventory|merchandise|goods (for|to) (resell|sell)",
        "INVENTORY", "INVENTORY",
    ),
    PurposeOption(
        "SOFTWARE_SUBSCRIPTION", "Software subscription",
        r"software|subscription|saas|licen[cs]e",
        "OPERATING_EXPENSE", "AMBIGUOUS",
    ),
    PurposeOption(
        "EQUIPMENT_PURCHASE", "Equipment / fixed-asset purchase",
        r"equipment|machine|furniture|fixture|vehicle|laptop|computer|"
        r"printer|generator|air ?condition|ac unit|tool",
        "FIXED_ASSET", "CAPITAL",
    ),
    PurposeOption(
        "TAX_LEVY", "Tax / levy",
        r"tax|levy|duty|cess|fine|penalt",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "EMPLOYEE_REIMBURSEMENT", "Employee reimbursement",
        r"reimburs|staff refund|employee (paid|advance)",
        "OPERATING_EXPENSE", "EXPENSE",
    ),
    PurposeOption(
        "OTHER", "Something else", r"",
        "OPERATING_EXPENSE", "DURABLE_ELSE",
    ),
]

_PURPOSE_BY_VALUE = {o.value: o for o in _PURPOSE_OPTIONS}

# Free-text "Something else" answers whose wording is durable-flavoured
# are routed to OTHER_DURABLE — the R3.2 trigger.  It behaves like OTHER
# for labelling but as AMBIGUOUS for capitalization.
_OTHER_DURABLE_OPTION = PurposeOption(
    "OTHER_DURABLE", "Something else (durable item)",
    r"", "OPERATING_EXPENSE", "AMBIGUOUS",
)
_PURPOSE_BY_VALUE["OTHER_DURABLE"] = _OTHER_DURABLE_OPTION

_PURPOSE_MARKER = "what is this expense for"

_LETTER_TO_PURPOSE = {
    letter: opt.value
    for letter, opt in zip("abcdefghijklmno", _PURPOSE_OPTIONS)
}

# Durable-goods vocabulary shared with the classifier's capital rules —
# free-text "Something else" answers containing these trigger the R3.2
# capitalization question instead of silently expensing.
# \ba/?c\b matches standalone "ac"/"a/c"/"acs" (the everyday abbreviation
# for air conditioners) via WORD BOUNDARIES — a plain substring would
# false-positive on "account", "trace", "contract", etc.
_DURABLE_KEYWORDS_RE = re.compile(
    r"machine|equipment|furniture|fixture|vehicle|laptop|computer|printer|"
    r"generator|air ?condition|air.?conditioning|ac unit|\ba/?c\b|\bacs\b|"
    r"hvac|tool|server|capital work|building|renovat|overhaul",
    re.I,
)


def purpose_question() -> str:
    """The purpose-first question for the generic expense path (R3.1)."""
    return (
        "What is this expense for? Reply with the letter or the purpose:\n"
        "(a) Rent\n"
        "(b) Electricity / utilities\n"
        "(c) Salaries & wages\n"
        "(d) Advertising & marketing\n"
        "(e) Legal & professional fees\n"
        "(f) Repairs & maintenance\n"
        "(g) Travel\n"
        "(h) Insurance\n"
        "(i) Interest / finance cost\n"
        "(j) Inventory — goods purchased for resale\n"
        "(k) Software subscription\n"
        "(l) Equipment / fixed-asset purchase\n"
        "(m) Tax / levy\n"
        "(n) Employee reimbursement\n"
        "(o) Something else (type it)"
    )


def purpose_option(value: str) -> Optional[PurposeOption]:
    return _PURPOSE_BY_VALUE.get((value or "").strip().upper())


def purpose_label(value: str) -> str:
    opt = purpose_option(value)
    return opt.label if opt else str(value or "").replace("_", " ").title()


def purpose_for_label(label: str) -> Optional[str]:
    """Map a purpose LABEL (as embedded in a question text) back to its
    canonical value — used by the preference learner for per-purpose
    capitalization keys."""
    low = (label or "").strip().lower()
    # Longest labels first so "Inventory — goods purchased for resale"
    # wins over shorter substrings that happen to appear elsewhere.
    for opt in sorted(
        _PURPOSE_OPTIONS + [_OTHER_DURABLE_OPTION],
        key=lambda o: len(o.label),
        reverse=True,
    ):
        if opt.label.lower() in low:
            return opt.value
    return None


def resolve_purpose_answer(question: str, answer: str) -> Optional[str]:
    """Deterministically map a purpose-question answer to its value.

    Accepts the option letter ("a"-"o"), the option wording, or — for
    "Something else" — genuinely free text (durable keywords upgrade it
    to OTHER_DURABLE, the R3.2 trigger).  Returns None when the answer
    cannot be confidently resolved; the planner re-asks.
    """
    if _PURPOSE_MARKER not in (question or "").lower():
        return None
    text = (answer or "").strip().lower().strip(" .)'\"")
    if not text:
        return None  # nothing said — the planner re-asks
    # Bare option letter ("a" ... "o").
    if text in _LETTER_TO_PURPOSE:
        return _LETTER_TO_PURPOSE[text]
    if re.fullmatch(r"[a-o]\)?", text):
        return _LETTER_TO_PURPOSE[text[0]]
    # Keyword wording.
    for opt in _PURPOSE_OPTIONS:
        if opt.keywords and re.search(opt.keywords, text):
            return opt.value
    # Free text under "something else".
    if _DURABLE_KEYWORDS_RE.search(text):
        return "OTHER_DURABLE"
    return "OTHER"


def nature_for_purpose(purpose_value: str, capitalized: Optional[bool] = None) -> str:
    """The transaction_nature implied by a purpose answer.

    Unambiguous purposes keep their nature; ambiguous ones (repairs,
    software, durable "something else") depend on the R3.2 capitalization
    decision (None → expense, the conservative default the threshold rule
    also uses).
    """
    opt = purpose_option(purpose_value)
    if not opt:
        return "OPERATING_EXPENSE"
    if opt.capital_class == "CAPITAL":
        return "FIXED_ASSET"
    if opt.capital_class == "INVENTORY":
        return "INVENTORY"
    if opt.capital_class in ("AMBIGUOUS", "DURABLE_ELSE"):
        return "FIXED_ASSET" if capitalized else "OPERATING_EXPENSE"
    return "OPERATING_EXPENSE"


# ---------------------------------------------------------------------------
# Work Stream R3.2 — capitalization disambiguation (IAS 16 / IAS 38)
# ---------------------------------------------------------------------------

_CAPITALIZATION_MARKER = "ordinary expense or capitalized"

CAPITALIZATION_DECISION_QUESTION = (
    "This amount could be treated as an ORDINARY EXPENSE or CAPITALIZED "
    "to a fixed asset. Which applies? Reply A or B: "
    "(A) Ordinary expense — consumed this period; "
    "(B) Capitalize — create or update a fixed asset record."
)

CAPITALIZATION_DECISION_QUESTION_SOFTWARE = (
    "This software cost could be treated as an ORDINARY EXPENSE or "
    "CAPITALIZED. Which applies? Reply A, B or C: "
    "(A) Subscription expense — consumed this period; "
    "(B) Intangible asset — identifiable software, capitalized; "
    "(C) Capitalize to a fixed asset record."
)


def capitalization_threshold_from_prefs(prefs: Optional[Dict[str, Any]]) -> float:
    """The org's capitalization threshold (preference key
    ``capitalization_threshold``) or the built-in default (50,000)."""
    raw = (prefs or {}).get("capitalization_threshold")
    if raw is None:
        return 50_000.0
    try:
        value = float(str(raw).replace(",", "").strip())
        return value if value > 0 else 50_000.0
    except (TypeError, ValueError):
        return 50_000.0


def capitalization_question_for(purpose_value: str) -> str:
    """The R3.2 question, flavoured by the purpose.  The purpose label is
    embedded so the answer can be learned as a per-purpose preference
    (preference key ``capitalization:<PURPOSE>``) and recurring purposes
    stop re-asking.  Software additionally offers the intangible-asset
    option (IAS 38)."""
    value = (purpose_value or "").upper()
    if value == "SOFTWARE_SUBSCRIPTION":
        return (
            "You chose 'Software subscription' for this expense. This "
            "cost could be treated as an ORDINARY EXPENSE or CAPITALIZED. "
            "Which applies? Reply A, B or C: "
            "(A) Subscription expense — consumed this period; "
            "(B) Intangible asset — identifiable software, capitalized; "
            "(C) Capitalize to a fixed asset record."
        )
    label = purpose_label(value)
    if value == "OTHER_DURABLE":
        return (
            "You described a durable item. This amount could be treated "
            "as an ORDINARY EXPENSE or CAPITALIZED to a fixed asset. "
            "Which applies? Reply A or B: "
            "(A) Ordinary expense — consumed this period; "
            "(B) Capitalize — create or update a fixed asset record."
        )
    return (
        f"You chose '{label}' for this expense. This amount could be "
        "treated as an ORDINARY EXPENSE or CAPITALIZED to a fixed asset. "
        "Which applies? Reply A or B: "
        "(A) Ordinary expense — consumed this period; "
        "(B) Capitalize — create or update a fixed asset record."
    )


def resolve_capitalization_answer(question: str, answer: str) -> Optional[str]:
    """Map a capitalization answer to EXPENSE or CAPITALIZE (None = re-ask)."""
    if _CAPITALIZATION_MARKER not in (question or "").lower():
        return None
    text = (answer or "").strip().lower().strip(" .)'\"")
    if text in ("a", "1"):
        return "EXPENSE"
    if text in ("b", "c", "2", "3"):
        return "CAPITALIZE"
    if re.search(r"subscription|ordinary|consumed|not capital", text):
        return "EXPENSE"
    if re.search(r"capitali[sz]e|asset|intangible|ppe|deprecia", text):
        return "CAPITALIZE"
    return None


def purpose_requires_capitalization_question(
    purpose_value: str,
    context_text: str = "",
    amount: Optional[float] = None,
    threshold: float = 50_000.0,
) -> bool:
    """R3.2 + SCALE-SAFE policy: the capitalization question is decided by
    the AMOUNT and the option's ambiguity class — never by a keyword list.

    * Known unambiguous treatments NEVER see it: rent/salaries/utilities
      (EXPENSE), resale purchase (INVENTORY), equipment (CAPITAL — the
      class derives the fixed-asset treatment directly).
    * Ambiguous/generic treatments (repairs, software, "something else",
      durable free text) at a MATERIAL amount (>= the org's threshold)
      ALWAYS see it.  A multi-national business deals in thousands of
      item types across hundreds of trades — a keyword vocabulary can
      never be exhaustive, so the AMOUNT decides when to be skeptical.
      Below the threshold the conservative auto-expense applies without
      asking (small items are never quizzed).
    """
    opt = purpose_option(purpose_value)
    if not opt:
        return False
    if opt.capital_class in ("EXPENSE", "INVENTORY", "CAPITAL"):
        return False
    # AMBIGUOUS (repairs/software/other-durable) and DURABLE_ELSE (generic
    # "something else") are materially ambiguous by definition — the
    # amount gate alone decides.  (_DURABLE_KEYWORDS_RE remains in the
    # vocabulary as a descriptive hint, not as a gate.)
    try:
        if amount is not None and float(amount) < float(threshold):
            return False
    except (TypeError, ValueError):
        pass
    return True


# ---------------------------------------------------------------------------
# Work Stream R3.3 — settlement position + underlying-event date
# ---------------------------------------------------------------------------

_SETTLEMENT_MARKER = "paid, or is it outstanding"

# CA-GRADE EXPENSE SETTLEMENT (accrual accounting): one question carrying
# the three journal treatments a chartered accountant enumerates for
# "record the electricity bill" — paid now (which ledger), incurred-now-
# payable (accrual), or the SETTLEMENT of an expense already recorded as
# payable (trade payables debit / cash-bank credit — NO new expense) —
# plus prepaid.  The old "(c) invoice or bill received" framing belonged
# to purchases/sales, not the expense path.
_EXPENSE_SETTLEMENT_MARKER = "expense debit, cash credit"
SETTLEMENT_POSITION_QUESTION = (
    "How should this be recorded — paid now, payable, or settling an "
    "expense already recorded as payable? Reply a, b, c, d or e: "
    "(a) Paid now — expense debit, cash credit; "
    "(b) Paid now — expense debit, bank credit; "
    "(c) Payable — expense debit, trade payables credit (pay later); "
    "(d) Settling an earlier payable — trade payables debit, cash/bank "
    "credit (no new expense); "
    "(e) Prepaid — paid ahead of consumption (prepayment asset)."
)

# The expense path's date question asks about the UNDERLYING EVENT (the
# accrual date), never the payment date — service on 31 Aug paid on
# 10 Sep belongs to August.  Same wire format as the Work Stream A
# date protocol (TODAY / YYYY-MM-DD / DD/MM/YYYY).
EXPENSE_EVENT_DATE_QUESTION = (
    "What date did you receive the goods/service or incur the "
    "obligation? This is the transaction date (the expense date) — "
    "NOT the payment date. Reply TODAY, or the date as YYYY-MM-DD or "
    "DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
)


def resolve_settlement_answer(question: str, answer: str) -> Optional[str]:
    """Map a settlement answer to CASH / BANK_TRANSFER / CREDIT /
    SETTLE_EXISTING_PAYABLE / ACCRUAL_PREPAID (None = re-ask; never
    guessed).  Both the CA-grade expense wording and the legacy wording
    resolve — history answers never break across wording upgrades."""
    q = (question or "").lower()
    if _EXPENSE_SETTLEMENT_MARKER not in q and _SETTLEMENT_MARKER not in q:
        return None
    text = (answer or "").strip().lower().strip(" .)'\"")
    if _EXPENSE_SETTLEMENT_MARKER in q:
        if text in ("a", "1"):
            return "CASH"
        if text in ("b", "2"):
            return "BANK_TRANSFER"
        if text in ("c", "3"):
            return "CREDIT"
        if text in ("d", "4"):
            return "SETTLE_EXISTING_PAYABLE"
        if text in ("e", "5"):
            return "ACCRUAL_PREPAID"
    else:
        # Legacy wording (kept for clarification history continuity).
        if text in ("a", "1"):
            return "CASH"
        if text in ("b", "2"):
            return "BANK_TRANSFER"
        if text in ("c", "3"):
            return "CREDIT"
        if text in ("d", "4"):
            return "ACCRUAL_PREPAID"
    if re.search(r"settl|already recorded|earlier payable|against the payable", text):
        return "SETTLE_EXISTING_PAYABLE"
    if re.search(r"prepaid|advance|ahead", text):
        return "ACCRUAL_PREPAID"
    if re.search(r"outstanding|payable|credit|owe|later", text):
        return "CREDIT"
    if re.search(r"bank|online|transfer|neft|card", text):
        return "BANK_TRANSFER"
    if re.search(r"cash", text):
        return "CASH"
    return None


# ---------------------------------------------------------------------------
# Work Stream R3.4 — backend-emitted tap-to-answer options per question
# ---------------------------------------------------------------------------

def options_for_question(question: str) -> Optional[List[Dict[str, str]]]:
    """Structured ``[{value, label}]`` options for a clarification
    question, or None for genuinely free-text questions.  The frontend
    renders these as chips (label shown, value sent); its hardcoded
    field-name switch remains the offline fallback."""
    q = (question or "").lower()
    # CONFIGURATION GAP — account-creation confirmation: one-tap YES
    # creates the proposed account, NO keeps the entry open for the user
    # to name (or tap) an existing account.
    if "should i create" in q:
        return [
            {"value": "YES", "label": "Create the account"},
            {"value": "NO", "label": "Use an existing account"},
        ]
    if _PURPOSE_MARKER in q:
        return [
            {"value": letter, "label": opt.label}
            for letter, opt in zip("abcdefghijklmno", _PURPOSE_OPTIONS)
        ]
    if _CAPITALIZATION_MARKER in q:
        if "intangible" in q:
            return [
                {"value": "A", "label": "Subscription expense"},
                {"value": "B", "label": "Intangible asset"},
                {"value": "C", "label": "Fixed asset"},
            ]
        return [
            {"value": "A", "label": "Ordinary expense"},
            {"value": "B", "label": "Capitalize to fixed asset"},
        ]
    if _EXPENSE_SETTLEMENT_MARKER in q:
        return [
            {"value": "a", "label": "Paid now — cash (Dr expense / Cr cash)"},
            {"value": "b", "label": "Paid now — bank (Dr expense / Cr bank)"},
            {"value": "c", "label": "Payable — pay later (Cr trade payables)"},
            {"value": "d", "label": "Settle an earlier payable (no new expense)"},
            {"value": "e", "label": "Prepaid — paid ahead (prepayment)"},
        ]
    if _SETTLEMENT_MARKER in q:
        return [
            {"value": "a", "label": "Paid now (cash)"},
            {"value": "b", "label": "Paid now (bank)"},
            {"value": "c", "label": "Outstanding"},
            {"value": "d", "label": "Prepaid / advance"},
        ]
    # Work Stream R4.2 — the business-operation question for receipts
    # and payments (invoice settlement / advance / loan-or-other).
    if "which business operation is this for" in q:
        return [
            {"value": "a", "label": "Invoice / bill settlement"},
            {"value": "b", "label": "Advance"},
            {"value": "c", "label": "Loan / drawing / other"},
        ]
    # Work Stream R4.4 — the settlement channel (cash vs bank) decides
    # which ledger the money hits.
    if "in cash, or through the bank" in q:
        return [
            {"value": "a", "label": "Cash"},
            {"value": "b", "label": "Bank / online"},
        ]
    if "cash or on credit" in q:
        return [
            {"value": "CASH", "label": "Cash"},
            {"value": "BANK_TRANSFER", "label": "Bank / online"},
            {"value": "CREDIT", "label": "Credit (outstanding)"},
        ]
    if "transaction date" in q or "expense date" in q:
        return [
            {"value": "TODAY", "label": "Today"},
            {"value": "YESTERDAY", "label": "Yesterday"},
        ]
    # Nature decision tree (same letters across the deterministic and
    # model paths) — prefer the family's EXPLICIT option labels; the
    # regex extraction is only a fallback (it mangles parenthesised
    # question text, e.g. "a SERVICE", "OTHER INCOME? Please answer…").
    family = next(
        (f for f, spec in _NATURE_MATRIX.items() if spec["marker"] in q),
        None,
    )
    if family:
        spec = _NATURE_MATRIX[family]
        explicit = spec.get("options")
        if explicit:
            return [
                {"value": o["value"], "label": o["label"]} for o in explicit
            ]
        labels = _option_labels_from_question(spec["question"])
        return (
            [
                {"value": letter, "label": label}
                for letter, label in zip("abcd", labels)
            ]
            or None
        )
    return None


def _option_labels_from_question(question: str) -> List[str]:
    """Fallback: extract the "(a) Label" fragments a nature question
    enumerates — stripping articles and trailing question text."""
    labels: List[str] = []
    for letter in "abcd":
        m = re.search(rf"\({letter}\)\s*(?:an?\s+)?([^()\n]+)", question or "", re.I)
        if m:
            label = m.group(1).strip()
            label = re.sub(
                r"\s*\?\s*(please|reply).*$", "", label, flags=re.I
            ).rstrip(" ,.;?")
            labels.append(label)
    return labels


def strip_markdown(text: Optional[str]) -> Optional[str]:
    """Sanitize model-emitted markdown from user-facing question/summary
    text (R3.4a): bold markers, backticks, leading heading hashes and
    bullet asterisks are stripped so the clarification card renders
    plain text."""
    if not text:
        return text
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    cleaned = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", cleaned)
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s+", "", cleaned, flags=re.M)
    cleaned = re.sub(r"^\s{0,3}[-*]\s+", "", cleaned, flags=re.M)
    return cleaned


# ---------------------------------------------------------------------------
# Search-result interpretation
# ---------------------------------------------------------------------------


def interpret_search_rows(data: Any) -> Tuple[FieldResolutionState, List[Any]]:
    """Classify a search/list tool payload WITHOUT treating [] as failure.

    Returns ``(state, rows)`` where *rows* is the list of candidate records
    (empty for non-list payloads).
    """
    if data is None:
        return FieldResolutionState.NOT_QUERIED, []
    if isinstance(data, dict):
        # Some services wrap results — tolerate {"data": [...]} /
        # {"results": [...]} shapes; otherwise it is a single record.
        for key in ("data", "results", "rows", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return _state_from_rows(inner), inner
        return FieldResolutionState.FOUND, [data]
    if isinstance(data, list):
        return _state_from_rows(data), data
    # Any other shape is still a successful single-row-ish payload.
    return FieldResolutionState.FOUND, [data]



# ---------------------------------------------------------------------------
# Party (customer/supplier) match resolution
# ---------------------------------------------------------------------------


class PartyMatchState(str, Enum):
    """Resolution quality of a party search against the named entity.

    * EXACT            — a record whose normalised name equals the requested
                         name: reuse it, creation is forbidden.
    * RESOLVED_PARTIAL — exactly one substring match (e.g. "XYZ Computers"
                         found for "XYZ"): reuse, creation is forbidden.
    * AMBIGUOUS        — several substring matches but no exact one: the
                         transaction must NOT silently pick one — creation is
                         forbidden and the user must confirm which record.
    * NONE             — no record matches: the dependency is genuinely
                         absent; decide create vs proceed-without.
    * UNNAMED          — no party name to resolve (nothing to match).
    """

    EXACT = "EXACT"
    RESOLVED_PARTIAL = "RESOLVED_PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"
    NONE = "NONE"
    UNNAMED = "UNNAMED"


_WS = re.compile(r"[\s\-_,.;:()'\"\[\]]+")


def normalize_entity_name(name: Optional[str]) -> str:
    """Normalise a name for comparison (case/punctuation/space-insensitive)."""
    if not name:
        return ""
    return _WS.sub(" ", str(name)).strip().lower()


def _row_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("name", "display_name", "title", "company_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def classify_party_match(
    rows: Any, entity_name: Optional[str]
) -> Tuple[PartyMatchState, List[Dict[str, Any]]]:
    """Resolve the named party against search rows across ALL states.

    State coverage:
    * rows not searched / malformed  → UNNAMED or NONE (never an exception)
    * []                             → NONE (the party does not exist yet)
    * exact normalised match         → EXACT (even among other partial hits)
    * exactly one substring match    → RESOLVED_PARTIAL
    * several substring matches, no exact → AMBIGUOUS (must not guess)

    Returns ``(state, matching_rows)``.
    """
    normalized = normalize_entity_name(entity_name)
    if not normalized:
        return PartyMatchState.UNNAMED, []

    candidates: List[Dict[str, Any]] = []
    if isinstance(rows, dict):
        for key in ("data", "results", "rows", "items"):
            inner = rows.get(key)
            if isinstance(inner, list):
                candidates = [r for r in inner if isinstance(r, dict)]
                break
        else:
            candidates = [rows] if rows else []
    elif isinstance(rows, list):
        candidates = [r for r in rows if isinstance(r, dict)]

    if not candidates:
        return PartyMatchState.NONE, []

    exact = [r for r in candidates if normalize_entity_name(_row_name(r)) == normalized]
    if exact:
        return PartyMatchState.EXACT, exact

    partial = [
        r for r in candidates
        if normalized in normalize_entity_name(_row_name(r))
        or normalize_entity_name(_row_name(r)) in normalized
    ]
    if not partial:
        return PartyMatchState.NONE, []
    if len(partial) == 1:
        return PartyMatchState.RESOLVED_PARTIAL, partial
    return PartyMatchState.AMBIGUOUS, partial

def _state_from_rows(rows: Sequence[Any]) -> FieldResolutionState:
    if not rows:
        return FieldResolutionState.EMPTY_RESULT  # valid information
    if len(rows) == 1:
        return FieldResolutionState.FOUND
    return FieldResolutionState.MULTIPLE_MATCHES



# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------


@dataclass
class DependencyNode:
    """One node of the transaction-specific dependency graph."""

    key: str                       # stable identifier (e.g. "amount")
    dimension: str                 # human label (e.g. "Transaction amount")
    state: FieldResolutionState
    detail: Optional[str] = None   # resolved value / reason
    question: Optional[str] = None  # only when user input is genuinely needed
    kind: str = "USER_INPUT"       # USER_INPUT | CONFIGURATION_GAP
    # Dependencies that must resolve first (dependency-first ordering):
    requires: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_open(self) -> bool:
        """True when this node still blocks execution and needs a decision."""
        return self.state in (
            FieldResolutionState.MISSING_AND_REQUIRED,
            FieldResolutionState.AMBIGUOUS,
            FieldResolutionState.BLOCKED_BY_CONFIGURATION,
        )

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "key": self.key,
            "dimension": self.dimension,
            "state": self.state.value,
        }
        if self.detail is not None:
            d["detail"] = self.detail
        if self.requires:
            d["requires"] = list(self.requires)
        return d


# Intent families used to decide which dependency nodes even apply.
_CASH_INTENT_HINTS = {"record_cash_purchase", "record_cash_sale"}
_CREDIT_PARTY_INTENTS = {
    "record_credit_purchase": "supplier_name",
    "record_credit_sale": "customer_name",
    "create_invoice": "customer_name",
    "record_payment": "supplier_name",
    "record_receipt": "customer_name",
    "create_quotation": "customer_name",
    "create_credit_note": "customer_name",
    "create_purchase_return": "supplier_name",
}

# Work Stream A - MANDATORY TRANSACTION-DATE PROTOCOL: every mutation
# intent must resolve an explicit accounting date before any tool call.
# Canonical set (imported by app/planner.py); quotations, asset disposal
# and depreciation are included even though they create non-journal-first
# documents.
DATE_REQUIRED_INTENTS = {
    "record_credit_purchase", "record_cash_purchase", "record_purchase",
    "record_credit_sale", "record_cash_sale", "record_sale",
    "record_expense", "record_receipt", "record_payment",
    "create_invoice", "create_credit_note", "create_purchase_return",
    "record_expense_payment", "record_bank_transfer",
    "register_fixed_asset", "create_quotation",
    "dispose_fixed_asset", "record_asset_depreciation",
}


def analyze_requirements(
    *,
    intent: str,
    entities: Dict[str, Any],
    missing_fields: Iterable[str],
    classification: Optional[Any] = None,
    relevant_customers: Optional[List[Dict[str, Any]]] = None,
    relevant_suppliers: Optional[List[Dict[str, Any]]] = None,
    relevant_bank_accounts: Optional[List[Dict[str, Any]]] = None,
) -> List[DependencyNode]:
    """Build the transaction-specific dependency graph with resolved states.

    This is the 360° gap analysis: every node gets an explicit
    :class:`FieldResolutionState`, and ONLY nodes that genuinely need a
    user decision carry a ``question``.  An empty ERP context is modelled
    as ``EMPTY_RESULT``/``MISSING_AND_REQUIRED`` — never as a failure.
    """
    entities = entities or {}
    missing = set(missing_fields or ())
    nodes: List[DependencyNode] = []

    # --- 1. Amount ----------------------------------------------------------
    amount = entities.get("amount")
    if amount is not None:
        nodes.append(DependencyNode(
            key="amount", dimension="Transaction amount",
            state=FieldResolutionState.KNOWN_FROM_USER, detail=str(amount),
        ))
    elif "amount" in missing:
        nodes.append(DependencyNode(
            key="amount", dimension="Transaction amount",
            state=FieldResolutionState.MISSING_AND_REQUIRED,
            question="What is the transaction amount?",
        ))
    else:
        nodes.append(DependencyNode(
            key="amount", dimension="Transaction amount",
            state=FieldResolutionState.NOT_QUERIED,
        ))

    # --- 1b. Transaction date (Work Stream A) --------------------------------
    # The mandatory date protocol: a mutation without a resolved date is a
    # MISSING_AND_REQUIRED user decision (never silently defaulted).
    if intent in DATE_REQUIRED_INTENTS:
        if entities.get("transaction_date"):
            nodes.append(DependencyNode(
                key="transaction_date", dimension="Transaction date",
                state=FieldResolutionState.KNOWN_FROM_USER,
                detail=str(entities["transaction_date"]),
            ))
        elif "transaction_date" in missing:
            nodes.append(DependencyNode(
                key="transaction_date", dimension="Transaction date",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                question=(
                    "What is the transaction date? Reply TODAY, or the date "
                    "as YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or "
                    "04/09/2026)."
                ),
            ))
        else:
            nodes.append(DependencyNode(
                key="transaction_date", dimension="Transaction date",
                state=FieldResolutionState.NOT_QUERIED,
            ))

    # --- 2. Payment treatment (cash vs credit) -------------------------------
    payment = entities.get("payment_method")
    if intent in ("record_purchase", "record_sale", "unknown", "register_fixed_asset"):
        if payment:
            nodes.append(DependencyNode(
                key="payment_treatment", dimension="Payment treatment",
                state=FieldResolutionState.KNOWN_FROM_USER, detail=str(payment),
            ))
        else:
            nodes.append(DependencyNode(
                key="payment_treatment", dimension="Payment treatment",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                question="Was this paid in cash or on credit?",
            ))
    elif payment:
        nodes.append(DependencyNode(
            key="payment_treatment", dimension="Payment treatment",
            state=FieldResolutionState.KNOWN_FROM_USER, detail=str(payment),
        ))

    # --- 3. Counterparty (customer/supplier) ---------------------------------
    party_field = _CREDIT_PARTY_INTENTS.get(intent)
    if intent in _NO_PARTY_INTENTS:
        named = entities.get("supplier_name") or entities.get("customer_name")
        # The party LEDGER dependency is optional for this event regardless
        # of whether a name was mentioned — the name is informational only.
        nodes.append(DependencyNode(
            key="counterparty", dimension="Counterparty ledger",
            state=FieldResolutionState.MISSING_BUT_OPTIONAL,
            detail=(
                f"{named} (informational only — not required for this "
                "transaction)" if named else
                "not required for this transaction — the named party is "
                "informational only"
            ),
        ))
    elif party_field:
        name = entities.get(party_field)
        if name:
            nodes.append(DependencyNode(
                key="counterparty", dimension="Counterparty",
                state=FieldResolutionState.KNOWN_FROM_USER, detail=str(name),
                requires=("party_resolution",),
            ))
        elif party_field in missing:
            nodes.append(DependencyNode(
                key="counterparty", dimension="Counterparty",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                question=(
                    "Who is the supplier?"
                    if "supplier" in party_field else "Who is the customer?"
                ),
            ))
        else:
            nodes.append(DependencyNode(
                key="counterparty", dimension="Counterparty",
                state=FieldResolutionState.NOT_QUERIED,
            ))
    elif intent == "record_expense":
        # CA-GRADE: party is ONLY material when the treatment creates a
        # payable (accrued expense) or settles one. Cash/bank/prepaid
        # treatments carry no party dimension — asking is domain noise.
        settlement = str(entities.get("settlement_position") or "").upper()
        if settlement in ("OUTSTANDING", "SETTLE_EXISTING_PAYABLE"):
            nodes.append(DependencyNode(
                key="counterparty", dimension="Counterparty",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                question="Who is the supplier / payee?",
            ))
        else:
            nodes.append(DependencyNode(
                key="counterparty", dimension="Counterparty",
                state=FieldResolutionState.MISSING_BUT_OPTIONAL,
                detail="not required — this treatment records a direct payment, not a payable",
            ))
    # Intents without any party dimension get no node at all — the
    # dependency graph is transaction-specific, never universal.

    # --- 4. Accounting classification (nature + account mapping) -------------
    if classification is not None:
        nature = getattr(classification, "transaction_nature", None)
        hint_id = getattr(classification, "account_hint_id", None)
        needs_clarification = getattr(
            classification, "requires_clarification", False
        )
        reason = getattr(classification, "clarification_reason", None)
        if nature and hint_id:
            nodes.append(DependencyNode(
                key="account_mapping", dimension="Account mapping",
                state=FieldResolutionState.RESOLVABLE_FROM_ERP,
                detail=getattr(classification, "account_hint_name", None)
                or str(hint_id),
            ))
        elif nature and needs_clarification:
            # The nature is decided but no configured account captures it —
            # a configuration gap.  Never invent an account.  When the
            # classifier PROPOSED the canonical account, name it (and its
            # free code) so the user can confirm creation with one tap.
            proposal_name = getattr(classification, "proposed_account_name", None)
            proposal_code = getattr(classification, "proposed_account_code", None)
            if proposal_name:
                gap_question = (
                    f"There is no '{proposal_name}' account in your chart of "
                    f"accounts for this entry"
                    + (
                        f" (suggested code {proposal_code})"
                        if proposal_code else ""
                    )
                    + ". Should I create it, or do you want to use a specific "
                    "existing account?"
                )
            else:
                gap_question = (
                    f"There is no {str(nature).replace('_', ' ').lower()} "
                    "account in your chart of accounts for this entry. "
                    "Should I create one, or do you want to use a specific "
                    "existing account?"
                )
            nodes.append(DependencyNode(
                key="account_mapping", dimension="Account mapping",
                state=FieldResolutionState.BLOCKED_BY_CONFIGURATION,
                detail=reason,
                question=gap_question,
                kind="CONFIGURATION_GAP",
            ))
        elif needs_clarification:
            nodes.append(DependencyNode(
                key="account_mapping", dimension="Economic classification",
                state=FieldResolutionState.AMBIGUOUS,
                detail=reason,
                # ITEM INTAKE PROTOCOL — the decision tree is asked
                # EXPLICITLY, never guessed (master parity prompt §2):
                question=NATURE_DECISION_QUESTION,
            ))
        elif nature:
            nodes.append(DependencyNode(
                key="account_mapping", dimension="Account mapping",
                state=FieldResolutionState.DETERMINISTICALLY_RESOLVABLE,
                detail=str(nature),
            ))

    # --- 4c. ITEM INTAKE PROTOCOL (capital vs expense vs inventory) ----------
    # Deterministic branch: a named item that is plausibly capital in nature
    # (machinery/vehicle/equipment/furniture/electronics above the
    # configurable threshold) or otherwise unresolved MUST have its nature
    # decided explicitly — never guessed from a catalog name match.
    if intent in _ITEM_INTAKE_INTENTS:
        item = entities.get("item_description") or entities.get("item")
        known_nature = entities.get("transaction_nature") or (
            getattr(classification, "transaction_nature", None)
            if classification is not None else None
        )
        classification_open = (
            getattr(classification, "requires_clarification", False)
            if classification is not None else True
        )
        if item and not known_nature and classification_open:
            already_asked = any(
                n.key == "account_mapping" and n.question for n in nodes
            )
            if not already_asked:
                trigger = "plausibly capital in nature"
                if _is_plausibly_capital(item, entities):
                    trigger = (
                        "matches durable/capital-goods indicators"
                        f" (threshold {CAPITAL_AMOUNT_THRESHOLD:,.0f})"
                    )
                nodes.append(DependencyNode(
                    key="transaction_nature", dimension="Item nature",
                    state=FieldResolutionState.AMBIGUOUS,
                    detail=(
                        f"'{item}' is {trigger} — the capital-vs-expense-vs-"
                        "inventory decision must be explicit"
                    ),
                    question=nature_question_for_intent(intent) or NATURE_DECISION_QUESTION,
                ))
        # Line detail capture: a named item needs quantity + unit price in
        # the SAME consolidated round — a bare amount is not enough.  Only
        # the genuinely MISSING half is asked for: when the quantity was
        # stated in the request ("selling 2 ovens"), asking for it again was
        # the reported bug.  Both wordings keep the words "quantity" and
        # "price" so the answer-merge heuristic (_merge_clarification_answers
        # matches on those keywords) still routes the reply correctly.
        if item and (
            entities.get("item_quantity") is None
            or entities.get("item_unit_price") is None
        ):
            has_qty = entities.get("item_quantity") is not None
            has_price = entities.get("item_unit_price") is not None
            if not has_qty and not has_price:
                detail = f"'{item}' named without complete line detail"
                question = (
                    f"What quantity and unit price apply to '{item}'? "
                    "(include the discount or tax rate too, if any)"
                )
            elif not has_price:
                detail = f"'{item}' named without a unit price"
                question = (
                    f"Unit price for '{item}'? (quantity "
                    f"{entities.get('item_quantity'):g} already noted — just "
                    "give the price, plus any discount or tax rate)"
                )
            else:
                detail = f"'{item}' named without a quantity"
                question = (
                    f"What quantity of '{item}'? (unit price "
                    f"{entities.get('item_unit_price'):g} already noted — "
                    "include any discount or tax rate)"
                )
            nodes.append(DependencyNode(
                key="item_line_details", dimension="Line detail",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                detail=detail,
                question=question,
            ))

    # --- 4d. TRANSACTION NATURE (Work Stream R - purpose-first) --------------
    # Fires for EVERY intent that creates, moves or classifies value,
    # regardless of amount/date/party state.  A missing amount NEVER
    # suppresses the nature node.  When a nature question already exists
    # (item intake or classification above) it is the same decision - it
    # is never asked twice in the same round.  The node is inserted FIRST:
    # purpose -> nature precedes every other open question.
    if intent in nature_question_intents():
        known_nature = entities.get("transaction_nature") or (
            getattr(classification, "transaction_nature", None)
            if classification is not None else None
        )
        already_asked = any(
            n.key in ("transaction_nature", "account_mapping") and n.question
            for n in nodes
        )
        if not known_nature and not already_asked:
            nodes.insert(0, DependencyNode(
                key="transaction_nature", dimension="Transaction nature",
                state=FieldResolutionState.MISSING_AND_REQUIRED,
                detail=(
                    "the purpose/nature decides the whole accounting "
                    "treatment (capitalise vs stock vs expense vs "
                    "settlement)"
                ),
                question=nature_question_for_intent(intent),
            ))

    # --- 5. Bank accounts (configuration for payment intents) ----------------
    if intent in _BANK_INTENTS:
        banks = relevant_bank_accounts or []
        if banks:
            nodes.append(DependencyNode(
                key="bank_accounts", dimension="Bank accounts",
                state=FieldResolutionState.RESOLVABLE_FROM_ERP,
                detail=f"{len(banks)} configured",
            ))
        else:
            nodes.append(DependencyNode(
                key="bank_accounts", dimension="Bank accounts",
                state=FieldResolutionState.BLOCKED_BY_CONFIGURATION,
                detail="no bank accounts configured",
                question=(
                    "No bank accounts are configured for this organization, "
                    "so this payment cannot be recorded yet. Please add them "
                    "under Banking → Bank Accounts (or tell me to create "
                    "them, including the bank name and account details)."
                ),
                kind="CONFIGURATION_GAP",
            ))

    return nodes


# ---------------------------------------------------------------------------
# Consolidated questionnaire
# ---------------------------------------------------------------------------


def collect_open_questions(nodes: Sequence[DependencyNode]) -> List[Dict[str, str]]:
    """Return only the genuinely-open user decisions from the graph.

    Ordering is dependency-first: nodes whose prerequisites are already
    resolved come before nodes that depend on other open nodes.
    """
    open_nodes = [n for n in nodes if n.is_open and n.question]
    resolved_keys = {
        n.key for n in nodes
        if not n.is_open and n.state is not FieldResolutionState.NOT_QUERIED
    }
    independent = [
        n for n in open_nodes
        if not n.requires or all(r in resolved_keys for r in n.requires)
    ]
    dependent = [
        n for n in open_nodes
        if n.requires and not all(r in resolved_keys for r in n.requires)
    ]
    ordered = independent + dependent
    return [
        {
            "field": n.key,
            "question": n.question or "",
            "kind": n.kind,
        }
        for n in ordered
    ]


def format_questionnaire(
    questions: Sequence[Dict[str, str]]
) -> Optional[str]:
    """Format questions into ONE consolidated message.

    * 0 questions  → None (nothing to ask — proceed).
    * 1 question   → the plain question text.
    * N questions  → a numbered questionnaire covering every independent gap,
      so the user answers everything in a single round instead of being
      drip-fed one question per turn.
    """
    cleaned = [q for q in (questions or []) if (q or {}).get("question")]
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return cleaned[0]["question"]
    lines = ["To record this transaction I need a few things:"]
    for i, q in enumerate(cleaned, start=1):
        lines.append(f"{i}. {q['question']}")
    return "\n".join(lines)


def plan_clarification_text(
    clarification_questions: Sequence[str],
    missing_fields: Sequence[str],
) -> Optional[str]:
    """Consolidated clarification from the Planner's per-field questions.

    Replaces the old "first question only" behaviour: every independently
    missing material field is presented in one questionnaire.
    """
    qs = [
        {"field": f, "question": q}
        for f, q in zip(missing_fields, clarification_questions)
    ]
    if not qs and clarification_questions:
        qs = [{"field": "unknown", "question": q} for q in clarification_questions]
    if not qs and missing_fields:
        qs = [
            {"field": f, "question": f"Please provide the {f.replace('_', ' ')}."}
            for f in missing_fields
        ]
    return format_questionnaire(qs)


# ---------------------------------------------------------------------------
# ECONOMIC / BUSINESS EVENT CLASSIFICATION (first-class reasoning stage)
# ---------------------------------------------------------------------------
# The agent's FIRST responsibility is not CRUD.  Before any tool is selected
# or any record is created, the agent determines WHAT REAL-WORLD EVENT is
# happening and what its economic nature is.  This stage is GENERIC: one
# reasoning engine serves every ERP module and every database state.


class EconomicEvent(str, Enum):
    """Generic economic-event taxonomy (module-independent).

    Every planner intent maps onto exactly one event; new modules only add
    a mapping row — the reasoning framework is reused unchanged.
    """

    ACQUISITION = "ACQUISITION"                # purchase / buy / acquire
    DISPOSAL = "DISPOSAL"                      # sale / sell / disposal
    EXPENDITURE = "EXPENDITURE"                # operating expense / consumable
    SETTLEMENT_IN = "SETTLEMENT_IN"            # receipt from customer
    SETTLEMENT_OUT = "SETTLEMENT_OUT"          # payment to supplier
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"    # bank-to-bank movement
    RETURN_OUT = "RETURN_OUT"                  # credit note / sales return
    RETURN_IN = "RETURN_IN"                    # purchase return to supplier
    DOCUMENT = "DOCUMENT"                      # quotation / non-posting doc
    RECORD_CREATION = "RECORD_CREATION"        # master-data creation
    REPORTING = "REPORTING"                    # read-only reporting/queries
    DEPRECIATION = "DEPRECIATION"              # non-cash asset cost allocation
    UNKNOWN = "UNKNOWN"


_EVENT_MAP: Dict[str, EconomicEvent] = {
    # Acquisitions
    "record_cash_purchase": EconomicEvent.ACQUISITION,
    "record_credit_purchase": EconomicEvent.ACQUISITION,
    "record_purchase": EconomicEvent.ACQUISITION,
    "register_fixed_asset": EconomicEvent.ACQUISITION,
    # Disposals
    "record_cash_sale": EconomicEvent.DISPOSAL,
    "record_credit_sale": EconomicEvent.DISPOSAL,
    "record_sale": EconomicEvent.DISPOSAL,
    "dispose_fixed_asset": EconomicEvent.DISPOSAL,
    # Expenditures
    "record_expense": EconomicEvent.EXPENDITURE,
    # Settlements
    "record_receipt": EconomicEvent.SETTLEMENT_IN,
    "record_payment": EconomicEvent.SETTLEMENT_OUT,
    "record_expense_payment": EconomicEvent.SETTLEMENT_OUT,
    # Internal movements
    "record_bank_transfer": EconomicEvent.INTERNAL_TRANSFER,
    # Returns / reversals
    "create_credit_note": EconomicEvent.RETURN_OUT,
    "create_purchase_return": EconomicEvent.RETURN_IN,
    # Non-posting documents
    "create_quotation": EconomicEvent.DOCUMENT,
    "create_invoice": EconomicEvent.DOCUMENT,
    # Quotation → Invoice conversion (Phase 4): creates the posting invoice
    # document (receivable journal is the internal downstream consequence,
    # mirroring create_invoice), then transitions the source quotation.
    "convert_quotation": EconomicEvent.DOCUMENT,
    # Master data
    "create_customer": EconomicEvent.RECORD_CREATION,
    "create_supplier": EconomicEvent.RECORD_CREATION,
    "create_project": EconomicEvent.RECORD_CREATION,
    "create_bank_account": EconomicEvent.RECORD_CREATION,
    "create_account": EconomicEvent.RECORD_CREATION,
    "create_product": EconomicEvent.RECORD_CREATION,
    "create_service": EconomicEvent.RECORD_CREATION,
    "list_bank_accounts": EconomicEvent.REPORTING,
    # Reporting / queries
    "generate_trial_balance": EconomicEvent.REPORTING,
    "generate_balance_sheet": EconomicEvent.REPORTING,
    "generate_profit_loss": EconomicEvent.REPORTING,
    "generate_cash_flow": EconomicEvent.REPORTING,
    "generate_general_ledger": EconomicEvent.REPORTING,
    "generate_customer_ledger": EconomicEvent.REPORTING,
    "generate_supplier_ledger": EconomicEvent.REPORTING,
    "customer_balance": EconomicEvent.REPORTING,
    "supplier_balance": EconomicEvent.REPORTING,
    "project_profitability": EconomicEvent.REPORTING,
    "search_product": EconomicEvent.REPORTING,
    "search_fixed_asset": EconomicEvent.REPORTING,
    "search_service": EconomicEvent.REPORTING,
    # Asset lifecycle
    "record_asset_depreciation": EconomicEvent.DEPRECIATION,
}


def classify_economic_event(intent: str) -> EconomicEvent:
    """Map a planner intent onto the underlying ECONOMIC EVENT.

    The user's wording alone is never sufficient — the intent keyword
    layer only narrows the request; this mapping determines what the
    event actually IS before tools are selected.
    """
    return _EVENT_MAP.get(intent, EconomicEvent.UNKNOWN)


# Dimension keys of the 360° impact map.  Only RELEVANT branches become
# active for a given event — the map is a framework, not a questionnaire.
IMPACT_DIMENSIONS = (
    "party_ledger",      # customer/supplier ledger required?
    "ar_ap",             # receivable/payable tracking required?
    "cash_bank",         # cash/bank settlement involved?
    "inventory",         # stock movement / inventory balance?
    "revenue",           # revenue recognition?
    "expense",           # expense recognition?
    "fixed_asset",       # asset capitalisation?
    "journal",           # GL journal posting?
    "documents",         # ERP documents (invoice/bill/note)?
    "dimensions",        # project/cost-center dimensions?
)


@dataclass
class ProhibitedAction:
    """One explicitly FORBIDDEN mutation for the current event/nature.

    Negative reasoning: correct execution includes correct NON-execution.
    """

    action: str        # stable key, e.g. "party_ledger_creation"
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {"action": self.action, "reason": self.reason}


@dataclass
class EventProfile:
    """Economic event + 360° impact map + prohibited mutations."""

    event: EconomicEvent
    intent: str
    affected: Dict[str, bool]
    prohibited: List[ProhibitedAction] = field(default_factory=list)
    nature: Optional[str] = None  # transaction nature when classified

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event.value,
            "intent": self.intent,
            "nature": self.nature,
            "impact_map": dict(self.affected),
            "prohibited_actions": [p.as_dict() for p in self.prohibited],
        }


# Tool slugs owned by each prohibitable action.  New modules register
# additional slugs here — the enforcement mechanism is generic.
_ACTION_TOOLS: Dict[str, set] = {
    "party_ledger_creation": {"create_supplier", "create_customer"},
    "inventory_movement": set(),   # no inventory tools registered yet
    "fixed_asset_capitalization": set(),
    "mutation": set(),             # filled dynamically for REPORTING events
}


def _nature_effects(nature: Optional[str]) -> Dict[str, bool]:
    """Economic effects of a transaction nature (None = unclassified).

    Inventory and fixed assets BOTH sit on the balance sheet but for
    fundamentally different purposes — the distinction is explicit here,
    never inferred from price or from the mere existence of a physical item.
    """
    if nature == "INVENTORY":
        return {"inventory": True, "fixed_asset": False, "expense": False}
    if nature in ("FIXED_ASSET", "INTANGIBLE_ASSET"):
        return {"inventory": False, "fixed_asset": True, "expense": False}
    if nature in ("OPERATING_EXPENSE", "CONSUMABLE"):
        return {"inventory": False, "fixed_asset": False, "expense": True}
    if nature == "SERVICE":
        return {"inventory": False, "fixed_asset": False, "expense": False}
    if nature in ("PREPAYMENT", "DEPOSIT_ADVANCE"):
        return {"inventory": False, "fixed_asset": False, "expense": False}
    return {"inventory": False, "fixed_asset": False, "expense": False}


# The universal EXISTS × REQUIRED matrix.  For every potentially
# affected ERP object the agent resolves one of these states — never
# "because the model usually creates one".
def resolve_existence_matrix(
    *, exists: bool, required: bool,
    ambiguous: bool = False, invalid: bool = False,
) -> FieldResolutionState:
    """EXISTS / DOES-NOT-EXIST × REQUIRED / NOT-REQUIRED / AMBIGUOUS / INVALID."""
    if invalid:
        return (FieldResolutionState.INVALID if exists
                else FieldResolutionState.BLOCKED_BY_CONFIGURATION)
    if ambiguous:
        return (FieldResolutionState.AMBIGUOUS if exists
                else FieldResolutionState.BLOCKED_BY_CONFIGURATION)
    if required:
        return (FieldResolutionState.RESOLVABLE_FROM_ERP if exists
                else FieldResolutionState.MISSING_AND_REQUIRED)
    return FieldResolutionState.MISSING_BUT_OPTIONAL


# Base journal/document involvement per economic event (before the item
# nature refines it).  This drives tool selection and the impact map.
_EVENT_BASE_IMPACT: Dict[EconomicEvent, Dict[str, bool]] = {
    EconomicEvent.ACQUISITION: {
        "party_ledger": False, "ar_ap": False, "cash_bank": True,
        "inventory": False, "revenue": False, "expense": True,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.DISPOSAL: {
        "party_ledger": False, "ar_ap": False, "cash_bank": True,
        "inventory": False, "revenue": True, "expense": False,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.EXPENDITURE: {
        "party_ledger": False, "ar_ap": False, "cash_bank": True,
        "inventory": False, "revenue": False, "expense": True,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.SETTLEMENT_IN: {
        "party_ledger": True, "ar_ap": True, "cash_bank": True,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.SETTLEMENT_OUT: {
        "party_ledger": True, "ar_ap": True, "cash_bank": True,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.INTERNAL_TRANSFER: {
        "party_ledger": False, "ar_ap": False, "cash_bank": True,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": True, "documents": False,
        "dimensions": False,
    },
    EconomicEvent.RETURN_OUT: {
        "party_ledger": True, "ar_ap": True, "cash_bank": False,
        "inventory": False, "revenue": True, "expense": False,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.RETURN_IN: {
        "party_ledger": True, "ar_ap": True, "cash_bank": False,
        "inventory": False, "revenue": False, "expense": True,
        "fixed_asset": False, "journal": True, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.DOCUMENT: {
        "party_ledger": False, "ar_ap": False, "cash_bank": False,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": False, "documents": True,
        "dimensions": False,
    },
    EconomicEvent.RECORD_CREATION: {
        "party_ledger": False, "ar_ap": False, "cash_bank": False,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": False, "documents": False,
        "dimensions": False,
    },
    EconomicEvent.REPORTING: {
        "party_ledger": False, "ar_ap": False, "cash_bank": False,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": False, "documents": False,
        "dimensions": False,
    },
    EconomicEvent.DEPRECIATION: {
        "party_ledger": False, "ar_ap": False, "cash_bank": False,
        "inventory": False, "revenue": False, "expense": True,
        "fixed_asset": True, "journal": True, "documents": False,
        "dimensions": False,
    },
    EconomicEvent.UNKNOWN: {
        "party_ledger": False, "ar_ap": False, "cash_bank": False,
        "inventory": False, "revenue": False, "expense": False,
        "fixed_asset": False, "journal": False, "documents": False,
        "dimensions": False,
    },
}

# Credit-based intents: party ledger and AR/AP ARE required.
_CREDIT_INTENTS = {
    "record_credit_purchase", "record_credit_sale", "create_invoice",
    "record_payment", "record_receipt", "record_expense_payment",
    "create_credit_note", "create_purchase_return", "create_quotation",
}
_EXPENSE_EVENTS = {EconomicEvent.ACQUISITION, EconomicEvent.EXPENDITURE,
                   EconomicEvent.RETURN_IN}


def build_event_profile(
    intent: str,
    classification: Optional[Any] = None,
) -> EventProfile:
    """Produce the full economic event profile for an intent.

    *classification* (when available) refines the impact map with the
    transaction NATURE (INVENTORY vs FIXED_ASSET vs EXPENSE vs SERVICE…)
    and adds the prohibited-mutation set (negative reasoning).
    """
    event = classify_economic_event(intent)
    affected = dict(_EVENT_BASE_IMPACT[event])

    # Cash-vs-credit refinement — a party ledger and AR/AP exist ONLY for
    # credit semantics; cash events explicitly set them False so no agent
    # layer can mistake a named party for a required ledger dependency.
    if intent in _CREDIT_INTENTS:
        affected["party_ledger"] = True
        affected["ar_ap"] = True
    elif intent in _CASH_INTENT_HINTS:
        affected["party_ledger"] = False
        affected["ar_ap"] = False

    nature = getattr(classification, "transaction_nature", None) if classification else None
    if intent in _ASSET_LIFECYCLE_INTENTS:
        # Economic reality of the asset lifecycle overrides item inference:
        # an asset transaction affects the ASSET dimension and can NEVER
        # create stock or be treated as ordinary inventory.
        affected["fixed_asset"] = True
        affected["inventory"] = False
        if intent == "dispose_fixed_asset":
            affected["revenue"] = False  # proceeds are NOT sales revenue
        if intent == "register_fixed_asset":
            affected["expense"] = False  # capitalised, never expensed
    if nature:
        effects = _nature_effects(nature)
        affected["inventory"] = effects["inventory"]
        affected["fixed_asset"] = effects["fixed_asset"]
        if effects["expense"]:
            affected["expense"] = True
        if (effects["inventory"] or effects["fixed_asset"]) and event in _EXPENSE_EVENTS:
            # Asset-side acquisition: capitalised, NOT expensed at acquisition.
            affected["expense"] = False

    prohibited: List[ProhibitedAction] = []
    if intent in _CASH_INTENT_HINTS:
        prohibited.append(ProhibitedAction(
            action="party_ledger_creation",
            reason=(
                "a supplier/customer ledger is NOT required for a cash "
                "transaction — the named party is informational only"
            ),
        ))
    if intent in _ASSET_LIFECYCLE_INTENTS:
        prohibited.append(ProhibitedAction(
            action="inventory_movement",
            reason=(
                "a fixed-asset lifecycle event is NOT an inventory "
                "operation — no stock item or stock movement may be "
                "created for it"
            ),
        ))
    if nature == "SERVICE":
        prohibited.append(ProhibitedAction(
            action="inventory_movement",
            reason=(
                "a service has NO inventory impact — never create stock "
                "items or stock movements for a service transaction"
            ),
        ))
    if nature in ("OPERATING_EXPENSE", "CONSUMABLE"):
        prohibited.append(ProhibitedAction(
            action="fixed_asset_capitalization",
            reason=(
                "an ordinary operating expense/consumable must NOT be "
                "capitalised as a fixed asset"
            ),
        ))
    if nature == "FIXED_ASSET":
        prohibited.append(ProhibitedAction(
            action="inventory_movement",
            reason=(
                "a fixed asset is NOT inventory — no stock item or stock "
                "movement may be created for it"
            ),
        ))
    if event is EconomicEvent.REPORTING:
        prohibited.append(ProhibitedAction(
            action="mutation",
            reason="a reporting request must not mutate any ERP records",
        ))

    return EventProfile(
        event=event, intent=intent, affected=affected,
        prohibited=prohibited, nature=nature,
    )


def prohibited_tool_names(profile: EventProfile) -> set:
    """Tool slugs the executor must REFUSE for this event profile.

    Generic enforcement: tools are derived from the prohibited action
    keys, never from per-scenario special cases.  For REPORTING events
    every registered mutation tool is prohibited.
    """
    blocked: set = set()
    for p in profile.prohibited:
        if p.action == "mutation":
            from app.tools import get_handler, list_tools

            for slug in list_tools():
                entry = get_handler(slug)
                if entry and not entry.get("read_only", False):
                    blocked.add(slug)
        else:
            blocked |= _ACTION_TOOLS.get(p.action, set())
    return blocked






# Intents where the counterparty ledger is NOT required (informational only).
_NO_PARTY_INTENTS = set(_CASH_INTENT_HINTS) | {
    "record_expense", "record_expense_payment",
    "record_asset_depreciation", "create_product",
    "create_service",
}
# Fixed-asset lifecycle intents: the asset dimension is ALWAYS affected,
# inventory NEVER is — regardless of item nature inference.
_ASSET_LIFECYCLE_INTENTS = {
    "register_fixed_asset", "dispose_fixed_asset",
    "record_asset_depreciation",
}
# Intents that need an existing bank account (configuration, never invented).
_BANK_INTENTS = {"record_receipt", "record_payment", "record_bank_transfer",
                 "record_expense_payment"}
