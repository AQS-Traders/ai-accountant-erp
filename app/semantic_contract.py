"""
AI-Native ERP — Semantic Intent Contract (Work Stream S2)
=========================================================

The formal representation of *business meaning* produced by the LLM semantic
understanding layer (``app/semantic_layer.py``) and consumed by the
deterministic planner.

This module is PURE: no I/O, no LLM, no database. It defines the vocabulary
and the information-state algebra so that every other stage shares one
definition of "what is known".

Architecture: ``docs/SEMANTIC_ARCHITECTURE.md``.

Why a contract instead of a bag of planner fields
-------------------------------------------------
S1 handed the planner loose planner fields (``customer_name``, ``amount``…).
That is a parser, not understanding: it cannot express *which* facts are
merely inferred, *which* are ambiguous, or *what* is genuinely missing — so
the questionnaire had to be reconstructed from the schema and asked for
things the user had already said.

The contract keeps the normalized ERP fields (the planner still needs them)
but wraps every fact in its INFORMATION STATE:

    EXPLICIT         the user stated it            (preserve exactly)
    SAFELY_INFERRED  derived by a documented rule  (carries evidence)
    AMBIGUOUS        several materially different meanings remain
    MISSING          not stated and not derivable

Only a fact that is EXPLICIT or SAFELY_INFERRED may become a prefill value.
AMBIGUOUS and MISSING become *targeted questions*, and they are different
questions: AMBIGUOUS offers the competing candidates as options, MISSING
asks openly for the one thing that is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Information states
# ---------------------------------------------------------------------------

EXPLICIT = "EXPLICIT"
SAFELY_INFERRED = "SAFELY_INFERRED"
AMBIGUOUS = "AMBIGUOUS"
MISSING = "MISSING"

#: States that may be handed to the deterministic pipeline as known values.
RESOLVED_STATES = frozenset({EXPLICIT, SAFELY_INFERRED})

#: States that must produce a targeted question instead of a value.
UNRESOLVED_STATES = frozenset({AMBIGUOUS, MISSING})

#: The canonical semantic fact names. These are the NORMALIZED ERP names
#: (not planner internals) — the vocabulary bridge between meaning and
#: execution. ``activity`` and the plural ``intents`` live outside this set
#: because they describe the act, not a field.
SEMANTIC_FACT_FIELDS = (
    "party_name",
    "party_role",
    "item_description",
    "quantity",
    "amount",
    "currency",
    "payment_terms",
    "transaction_date",
    "transaction_nature",
    "account_name",
    "bank_account",
    "project_name",
    "reference",
)

#: Semantic activity vocabulary — BUSINESS meanings, deliberately broader
#: than the planner's keyword triggers. "supplied", "bought from us",
#: "cleared their dues" all land on one of these.
ACTIVITIES = (
    "sale",
    "purchase",
    "expense",
    "receipt",
    "payment",
    "invoice",
    "bill",
    "quotation",
    "credit_note",
    "purchase_return",
    "fixed_asset_purchase",
    "fixed_asset_disposal",
    "depreciation",
    "bank_transfer",
    "report",
)

#: Money direction per activity — decides the party role.
MONEY_IN = frozenset(
    {"sale", "receipt", "invoice", "credit_note", "fixed_asset_disposal"}
)
MONEY_OUT = frozenset(
    {
        "purchase",
        "expense",
        "payment",
        "bill",
        "purchase_return",
        "fixed_asset_purchase",
        "depreciation",
    }
)

PARTY_ROLES = ("customer", "supplier")
PAYMENT_METHODS = ("CASH", "CREDIT", "BANK_TRANSFER", "CHEQUE", "CARD")
NATURES = ("GOODS", "SERVICE", "ASSET_DISPOSAL", "OTHER_INCOME")


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass
class SemanticFact:
    """One material piece of information with its information state."""

    name: str
    value: Any = None
    state: str = EXPLICIT
    evidence: Optional[str] = None
    candidates: List[Any] = field(default_factory=list)
    required_for: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        self.state = str(self.state or EXPLICIT).upper()
        if self.state == AMBIGUOUS and not self.candidates:
            # An ambiguity with no candidates is really missing information:
            # we know the user named something, but there is nothing to
            # choose between.
            self.state = MISSING if self.value in (None, "") else AMBIGUOUS

    @property
    def is_resolved(self) -> bool:
        return self.state in RESOLVED_STATES and self.value not in (None, "")

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "state": self.state}
        if self.value not in (None, ""):
            out["value"] = self.value
        if self.evidence:
            out["evidence"] = str(self.evidence)[:200]
        if self.candidates:
            out["candidates"] = list(self.candidates)[:8]
        if self.required_for:
            out["required_for"] = str(self.required_for)[:120]
        if self.reason:
            out["reason"] = str(self.reason)[:200]
        return out


# ---------------------------------------------------------------------------
# Semantic intent
# ---------------------------------------------------------------------------


@dataclass
class SemanticIntent:
    """The complete understood business meaning of a user request.

    ``fields`` holds one :class:`SemanticFact` per material fact, keyed by
    the canonical semantic name — REGARDLESS of state, so an AMBIGUOUS party
    and a MISSING amount are equally visible to the information-state
    analysis instead of being silently absent.
    """

    activities: List[str] = field(default_factory=list)
    intents: List[str] = field(default_factory=list)
    fields: Dict[str, SemanticFact] = field(default_factory=dict)
    clause_count: int = 1
    source: str = "semantic_llm"

    def set_fact(
        self,
        name: str,
        value: Any = None,
        *,
        state: str = EXPLICIT,
        evidence: Optional[str] = None,
        candidates: Optional[List[Any]] = None,
        required_for: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> SemanticFact:
        """Record (or upgrade) a fact.

        Precedence is enforced here, in one place: a resolved fact is never
        downgraded by a later weaker statement of the same name, and an
        AMBIGUOUS/MISSING entry is upgraded the moment a resolved value
        arrives (that is how a clarification answer merges in).
        """
        new = SemanticFact(
            name=name,
            value=value,
            state=state,
            evidence=evidence,
            candidates=candidates or [],
            required_for=required_for,
            reason=reason,
        )
        existing = self.fields.get(name)
        if existing is None:
            self.fields[name] = new
            return new
        if existing.is_resolved and not new.is_resolved:
            # A weaker later statement must never erase a known value.
            return existing
        self.fields[name] = new
        return new

    def drop(self, name: str) -> None:
        self.fields.pop(name, None)

    # ---- inspection ------------------------------------------------------

    @property
    def activity(self) -> Optional[str]:
        return self.activities[0] if self.activities else None

    def value(self, name: str) -> Any:
        """The value of a RESOLVED fact, else ``None``."""
        fact = self.fields.get(name)
        if fact is None or not fact.is_resolved:
            return None
        return fact.value

    def fact(self, name: str) -> Optional[SemanticFact]:
        return self.fields.get(name)

    def by_state(self, state: str) -> List[SemanticFact]:
        wanted = str(state or "").upper()
        return [f for f in self.fields.values() if f.state == wanted]

    @property
    def resolved(self) -> Dict[str, Any]:
        """Canonical name -> value for every RESOLVED fact."""
        return {f.name: f.value for f in self.fields.values() if f.is_resolved}

    @property
    def ambiguous(self) -> List[SemanticFact]:
        return self.by_state(AMBIGUOUS)

    @property
    def missing(self) -> List[SemanticFact]:
        return self.by_state(MISSING)

    @property
    def is_empty(self) -> bool:
        return not self.fields and not self.activities

    # ---- merge (clarification continuity) --------------------------------

    def merge(self, other: "SemanticIntent") -> "SemanticIntent":
        """Fold a later understanding into this one WITHOUT losing facts.

        This is the continuity rule: when a clarification answer arrives the
        answer is understood on its own and merged into the ORIGINAL state.
        A fact already established as resolved is never re-opened merely
        because the answer round did not mention it again.
        """
        if other is None:
            return self
        merged = SemanticIntent(
            activities=list(self.activities),
            intents=list(self.intents),
            clause_count=max(self.clause_count, other.clause_count),
            source=self.source,
        )
        merged.fields.update(self.fields)
        for name, fact in (other.fields or {}).items():
            merged.set_fact(
                name,
                fact.value,
                state=fact.state,
                evidence=fact.evidence,
                candidates=fact.candidates,
                required_for=fact.required_for,
                reason=fact.reason,
            )
        for act in other.activities or []:
            if act not in merged.activities:
                merged.activities.append(act)
        for intent in other.intents or []:
            if intent not in merged.intents:
                merged.intents.append(intent)
        return merged

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe form (used for audit steps and evaluation fixtures)."""
        return {
            "activities": list(self.activities),
            "intents": list(self.intents),
            "clause_count": self.clause_count,
            "source": self.source,
            "facts": {n: f.to_dict() for n, f in self.fields.items()},
            "ambiguous": [f.to_dict() for f in self.ambiguous],
            "missing": [f.to_dict() for f in self.missing],
        }


# ---------------------------------------------------------------------------
# Key normalization (ERP vocabulary bridging)
# ---------------------------------------------------------------------------

#: The LLM may use either a semantic name or the planner's legacy name; both
#: must land on ONE canonical fact. This mapping is the single place those
#: aliases are resolved — never a per-stage special case.
_ALIASES = {
    "party": "party_name",
    "party_name": "party_name",
    "customer_name": "party_name",
    "supplier_name": "party_name",
    "customer": "party_name",
    "supplier": "party_name",
    "vendor": "party_name",
    "party_role": "party_role",
    "role": "party_role",
    "item": "item_description",
    "item_description": "item_description",
    "description": "item_description",
    "product": "item_description",
    "service": "item_description",
    "item_quantity": "quantity",
    "quantity": "quantity",
    "units": "quantity",
    "amount": "amount",
    "total": "amount",
    "value": "amount",
    "price": "amount",
    "currency": "currency",
    "payment_terms": "payment_terms",
    "payment_method": "payment_terms",
    "terms": "payment_terms",
    "transaction_date": "transaction_date",
    "date": "transaction_date",
    "transaction_nature": "transaction_nature",
    "nature": "transaction_nature",
    "account": "account_name",
    "account_name": "account_name",
    "expense_account": "account_name",
    "bank_account": "bank_account",
    "bank": "bank_account",
    "project": "project_name",
    "project_name": "project_name",
    "reference": "reference",
    "reference_no": "reference",
    "invoice_number": "reference",
    "document_number": "reference",
}


def canonical_field(name: Any) -> Optional[str]:
    """Map any accepted alias to its canonical semantic fact name."""
    key = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not key:
        return None
    return _ALIASES.get(key)


def normalize_state(state: Any) -> str:
    """Coerce an LLM-reported status into one of the four canonical states."""
    value = str(state or "").strip().upper().replace(" ", "_").replace("-", "_")
    if value in ("EXPLICIT", "STATED", "USER_STATED", "GIVEN"):
        return EXPLICIT
    if value in ("SAFELY_INFERRED", "INFERRED", "DERIVED", "IMPLIED", "SAFE_INFERENCE"):
        return SAFELY_INFERRED
    if value in ("AMBIGUOUS", "UNCLEAR", "MULTIPLE", "CONFLICTING"):
        return AMBIGUOUS
    if value in ("MISSING", "ABSENT", "UNKNOWN", "NOT_STATED", "REQUIRED"):
        return MISSING
    return EXPLICIT


def information_state_summary(intent: SemanticIntent) -> Dict[str, List[str]]:
    """Compact per-state field listing for audit steps and logs."""
    out: Dict[str, List[str]] = {
        EXPLICIT: [],
        SAFELY_INFERRED: [],
        AMBIGUOUS: [],
        MISSING: [],
    }
    for fact in sorted(intent.fields.values(), key=lambda f: f.name):
        out.setdefault(fact.state, []).append(fact.name)
    return out


def is_date_like(value: Any) -> bool:
    """True for an ISO ``YYYY-MM-DD`` string or a ``date`` instance."""
    if isinstance(value, date):
        return True
    text = str(value or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True