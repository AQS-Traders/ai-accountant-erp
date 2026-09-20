"""
AI-native semantic understanding layer (Work Stream S2).

This is the PRIMARY interpretation stage of the ERP. The user's request — in
whatever natural wording — is understood here as structured business meaning,
grounded against the user's own words, and normalized into the ERP's
deterministic vocabulary. The keyword regex extractor in the planner is the
FALLBACK, never the competing intelligence.

Architecture: docs/SEMANTIC_ARCHITECTURE.md.

Pipeline role:
    USER REQUEST -> LLM SEMANTIC UNDERSTANDING -> GROUNDED FACTS
    -> DETERMINISTIC NORMALIZATION -> planner -> validation -> execution.

Grounding contract (python-enforced, never negotiable):
* party/item text must occur VERBATIM in the user's own words;
* numbers must be traceable to a digit token or written number;
* dates must be explicit in the text or a resolvable relative word;
* enums are validated against canonical vocabularies;
* party_role must be consistent with the activity's money direction;
* a value that cannot be traced is DROPPED with its status preserved as
  AMBIGUOUS/MISSING — never repaired, never closest-matched, never executed.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Any, Dict, Optional

import structlog

from app.entity_segregation import (
    _GENERIC_ITEMS,
    _WORD_NUMBERS,
    _clean_candidate,
    _date_grounded,
    _is_grounded,
    _norm,
    _number_grounded,
    _parse_json_response,
)
from app.semantic_contract import (
    ACTIVITIES,
    AMBIGUOUS,
    EXPLICIT,
    MISSING,
    MONEY_IN,
    MONEY_OUT,
    NATURES as CONTRACT_NATURES,
    PARTY_ROLES,
    PAYMENT_METHODS,
    SAFELY_INFERRED,
    SemanticFact,
    SemanticIntent,
    canonical_field,
    information_state_summary,
    normalize_state,
)
from app.semantic_context import (
    SemanticContext,
    build_semantic_context,
    distill_rulebook,
)

log = structlog.get_logger(__name__)

# Canonical vocabularies (mirrors of the deterministic ERP domain).
_NATURES = ("GOODS", "SERVICE", "ASSET_DISPOSAL", "OTHER_INCOME")
_PAYMENT_METHODS = ("CASH", "CREDIT", "BANK_TRANSFER", "CHEQUE", "CARD")
_PARTY_ROLES = ("customer", "supplier")

# Semantic activity vocabulary. These are BUSINESS meanings, not keywords —
# "supplied", "bought from us", "cleared their dues" all land here via the
# LLM, then normalize deterministically into planner intents.
_ACTIVITIES = (
    "sale", "purchase", "expense", "receipt", "payment",
    "invoice", "bill", "credit_note", "purchase_return",
)

# Activities where money flows IN (party = customer) vs OUT (party = supplier).
_MONEY_IN = frozenset({"sale", "receipt", "invoice", "credit_note"})
_MONEY_OUT = frozenset({"purchase", "expense", "payment", "bill", "purchase_return"})

# Deterministic normalization: (activity, payment_terms) -> planner intent.
# The planner's own vocabulary; unknown combos fall back to regex intent.
_INTENT_MAP = {
    ("sale", "CREDIT"): "record_credit_sale",
    ("sale", "CASH"): "record_cash_sale",
    ("sale", None): "record_sale",
    ("purchase", "CREDIT"): "record_credit_purchase",
    ("purchase", "CASH"): "record_cash_purchase",
    ("purchase", None): "record_purchase",
    ("expense", None): "record_expense",
    ("receipt", None): "record_receipt",
    ("payment", None): "record_payment",
    ("invoice", None): "create_invoice",
    ("bill", None): "create_purchase_bill",
    ("credit_note", None): "create_credit_note",
    ("purchase_return", None): "create_purchase_return",
}

_RESPONSE_SCHEMA = """
RESPONSE FORMAT - output ONLY this JSON object, no prose:

{
  "activities": ["sale"],
  "facts": [
    {"name": "party_name", "value": "<verbatim from the request>",
     "role": "customer", "state": "EXPLICIT"},
    {"name": "item_description", "value": "chairs", "state": "EXPLICIT"},
    {"name": "quantity", "value": 2, "state": "EXPLICIT"},
    {"name": "amount", "value": 23000, "state": "EXPLICIT"},
    {"name": "payment_terms", "value": "CREDIT", "state": "SAFELY_INFERRED",
     "evidence": "\\"they'll pay later\\""},
    {"name": "transaction_date", "value": "2026-09-18",
     "state": "SAFELY_INFERRED", "evidence": "\\"yesterday\\""},
    {"name": "transaction_nature", "value": "GOODS", "state": "SAFELY_INFERRED",
     "evidence": "chairs are stock goods"}
  ],
  "ambiguous": [
    {"field": "party_name", "value": "ABC",
     "candidates": ["ABC Pvt Ltd", "ABC Traders"], "reason": "three similar parties"}
  ],
  "missing": [
    {"field": "amount", "reason": "no money value stated",
     "required_for": "a balanced journal"}
  ],
  "unsupported": ["payroll"]
}

FIELD VOCABULARY (use these names in facts/ambiguous/missing):
  party_name, party_role, item_description, quantity, amount, currency,
  payment_terms, transaction_date, transaction_nature, account_name,
  bank_account, project_name, reference

STATE VOCABULARY: EXPLICIT | SAFELY_INFERRED | AMBIGUOUS | MISSING
  - facts[] carries only EXPLICIT and SAFELY_INFERRED entries.
  - ambiguous[] carries AMBIGUOUS entries (each with its candidates).
  - missing[] carries MISSING entries (each with why it is required).
  - A fact that is EXPLICIT or SAFELY_INFERRED must NOT also appear in
    ambiguous[] or missing[].

OTHER RULES:
  - payment_terms is always one of CASH, CREDIT, BANK_TRANSFER, CHEQUE, CARD
    (inferred meanings count: "will pay later"/"udhaar" -> CREDIT).
  - party_role is customer or supplier, and must agree with the money
    direction of the activity.
  - quantity and amount are NUMBERS, never strings.
  - Give at most 3 entries in ambiguous[] and at most 3 in missing[] -
    only the ones that genuinely block a correct recording.
  - If more than one distinct business act is requested, list every activity
    in activities[] (in the order described) and set "clause_count" to the
    number of distinct acts.
  - If the request needs something this ERP has no capability for, list it in
    unsupported[] and do not invent a field for it.
""".strip()


def _build_prompt(context_block: str, user_message: str) -> str:
    """Compose the semantic-understanding prompt.

    Order matters: the rulebook and ERP context come FIRST (how to think,
    what exists), then the request, then the required output shape.
    """
    return (
        "You are the semantic understanding layer of an AI-native ERP.\n"
        "Read the user's request and express its BUSINESS MEANING as "
        "structured facts, using the rulebook below as your reasoning "
        "framework and the ERP context as the only record set that exists.\n\n"
        f"{context_block}\n\n"
        f"USER REQUEST:\n{user_message}\n\n"
        f"{_RESPONSE_SCHEMA}\n"
    )


def _traceable_number(value: float, normalized: str, raw_lower: str = "") -> bool:
    """Number must trace to the user's own text (digit or written form)."""
    if value <= 0:
        return False
    if value == int(value):
        token = re.escape(str(int(value)))
        if re.search(rf"(?<![\d.,]){token}(?![\d])", normalized):
            return True
        # Comma-formatted digits: "23,000" normalises to "23 000", so the
        # digit check runs against the comma-stripped raw text as well.
        if raw_lower:
            compact = re.sub(r"[,\s]", "", raw_lower)
            if re.search(rf"(?<!\d){token}(?!\d)", compact):
                return True
    return _number_grounded(value, normalized)


def _traceable_date(text: str, raw_lower: str, today: date) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(text or "")):
        return False
    return _date_grounded(str(text), raw_lower, today)


def _ground_party_role(role, activity) -> Optional[str]:
    """Validate the party role against the activity's money direction.

    An inconsistent pairing (e.g. role=customer for a purchase) is treated as
    AMBIGUOUS — the role is dropped rather than guessed.
    """
    r = str(role or "").strip().lower()
    if r not in _PARTY_ROLES:
        return None
    act = str(activity or "").strip().lower()
    if act in _MONEY_IN and r != "customer":
        return None
    if act in _MONEY_OUT and r != "supplier":
        return None
    return r


def ground_facts(parsed: dict, raw_message: str, today: date) -> dict:
    """Turn the LLM's semantic JSON into grounded, traceable facts.

    A value that cannot be traced to the user's own text is DROPPED — never
    repaired, never closest-matched. Enum facts (activity, payment_terms,
    transaction_nature, party role) are validated inferences, not copies:
    "they'll pay later" is CREDIT even though the word never appears.
    """
    normalized = _norm(raw_message)
    raw_lower = (raw_message or "").lower()
    facts: Dict[str, Any] = {}
    status_in = parsed.get("status") if isinstance(parsed.get("status"), dict) else {}
    evidence_in = (
        parsed.get("evidence") if isinstance(parsed.get("evidence"), dict) else {}
    )

    def _keep(field: str, value) -> None:
        facts[field] = {
            "value": value,
            "status": str(status_in.get(field) or "EXPLICIT").upper(),
            "evidence": str(evidence_in.get(field) or "")[:200] or None,
        }

    # --- text facts (verbatim copies) -------------------------------------
    party = parsed.get("party") if isinstance(parsed.get("party"), dict) else {}
    name = _clean_candidate(party.get("name"))
    if name and len(name) <= 120 and _is_grounded(name, normalized):
        role = _ground_party_role(party.get("role"), parsed.get("activity"))
        if role:
            _keep("party", {"name": name, "role": role})
        else:
            log.info("semantic_rejected", field="party_role", reason="inconsistent")
    elif name:
        log.info("semantic_rejected", field="party", reason="not_in_text")

    item = parsed.get("item") if isinstance(parsed.get("item"), dict) else {}
    desc = _clean_candidate(item.get("description"))
    if desc and desc.lower() not in _GENERIC_ITEMS and _is_grounded(desc, normalized):
        _keep("item", desc)
    elif desc:
        log.info("semantic_rejected", field="item", reason="not_in_text")

    # --- traceable numbers -------------------------------------------------
    for field in ("quantity", "amount"):
        try:
            value = float(str(parsed.get(field)).replace(",", "").strip())
        except (TypeError, ValueError):
            if parsed.get(field) not in (None, ""):
                log.info("semantic_rejected", field=field, reason="not_a_number")
            continue
        if _traceable_number(value, normalized, raw_lower):
            _keep(field, value)
        else:
            log.info("semantic_rejected", field=field, reason="untraceable_number")

    # --- validated inferences ---------------------------------------------
    terms = str(parsed.get("payment_terms") or "").strip().upper()
    if terms in _PAYMENT_METHODS:
        # Inference with evidence, NOT keyword-gated: "they'll pay later"
        # is credit even though the word "credit" never appears.
        _keep("payment_terms", terms)
    elif terms:
        log.info("semantic_rejected", field="payment_terms", reason="unknown_method")

    raw_date = str(parsed.get("transaction_date") or "").strip()
    if raw_date:
        if _traceable_date(raw_date, raw_lower, today):
            _keep("transaction_date", raw_date)
        else:
            log.info(
                "semantic_rejected", field="transaction_date",
                reason="untraceable_date",
            )

    nature = str(parsed.get("transaction_nature") or "").strip().upper()
    if nature in _NATURES:
        _keep("transaction_nature", nature)
    elif nature:
        log.info(
            "semantic_rejected", field="transaction_nature", reason="unknown_nature"
        )

    activity = str(parsed.get("activity") or "").strip().lower()
    if activity in _ACTIVITIES:
        _keep("activity", activity)

    ambiguous = parsed.get("ambiguous")
    if isinstance(ambiguous, list) and ambiguous:
        facts["ambiguous"] = [
            {"field": str(a.get("field", ""))[:60],
             "reason": str(a.get("reason", ""))[:200]}
            for a in ambiguous if isinstance(a, dict)
        ]

    return facts


async def _understand(
    user_message: str,
    orchestrator,
    today: Optional[date],
    context: Optional[SemanticContext],
) -> Optional[dict]:
    """ONE bounded, grounded provider call. Returns the parsed JSON or None.

    None means "the AI is unavailable" — provider disabled, missing, down,
    timed out, or unparseable. That is the ONLY signal the caller needs to
    fall back to deterministic behaviour, so it can never raise.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.semantic_understanding_enabled:
        return None

    msg = (user_message or "").strip()
    if not msg:
        return None

    # Mechanical extraction is the FAST TIER's job: it reads text into facts and
    # makes no accounting decision. ``generate_text_light`` uses the measured
    # fast model chain (see settings.accounting_fast_model_chain); test doubles
    # and older orchestrators without it fall back to the standard entry point,
    # so the stage keeps working with any implementation.
    generate = None
    if orchestrator is not None:
        generate = getattr(orchestrator, "generate_text_light", None)
        if not callable(generate):
            generate = getattr(orchestrator, "generate_text", None)
    if not callable(generate):
        return None

    today = today or date.today()

    # The context package carries the rulebook, the org identity, the real
    # records and the session state. When no package was built (unit tests,
    # degraded paths) the rulebook alone is still sent, so the model always
    # reasons with the agent's laws rather than inventing its own.
    if context is None:
        from app.prompts import load_constitution

        context = SemanticContext(
            rulebook=distill_rulebook(load_constitution()),
            today=today.isoformat(),
        )
    if not context.today:
        context.today = today.isoformat()

    prompt = _build_prompt(context.as_prompt_block(), msg)
    budget = settings.semantic_understanding_timeout

    try:
        raw = await asyncio.wait_for(generate(prompt=prompt), timeout=budget)
    except asyncio.TimeoutError:
        log.warning("semantic_failed", reason="timeout", timeout_s=budget)
        return None
    except Exception as exc:  # noqa: BLE001 — the stage must never raise
        log.warning("semantic_failed", reason="provider_error", detail=str(exc)[:200])
        return None

    parsed = _parse_json_response(raw if isinstance(raw, str) else "")
    if not parsed:
        log.info("semantic_failed", reason="unparseable_response")
        return None
    return parsed


async def extract_semantic_facts(
    user_message: str,
    orchestrator=None,
    today: Optional[date] = None,
) -> dict:
    """LLM semantic understanding -> grounded flat facts (compatibility API).

    Kept for callers/tests written against S1. New code uses
    :func:`extract_semantic_intent`, which returns the full contract. Both
    share ONE provider call and ONE grounding implementation.
    """
    parsed = await _understand(user_message, orchestrator, today, None)
    if not parsed:
        return {}
    msg = (user_message or "").strip()
    facts = ground_facts(parsed, msg, today or date.today())
    log.info(
        "semantic_understood",
        fields=sorted(facts.keys()),
        ambiguous=bool(facts.get("ambiguous")),
    )
    return facts


def normalize_to_erp(facts: dict) -> dict:
    """Deterministic normalization: semantic facts -> planner prefill.

    This is where meaning becomes ERP vocabulary — semantic_activity ×
    payment_terms -> planner intent; party role -> customer/supplier field;
    terms -> payment_method; everything else keeps its normalized name.
    The output feeds ``plan(prefill_entities=...)`` and nothing else.

    Accepts EITHER the legacy flat-fact dict or a full
    :class:`~app.semantic_contract.SemanticIntent`, so existing callers keep
    working while the contract becomes the primary representation.
    """
    if isinstance(facts, SemanticIntent):
        return normalize_semantic_intent(facts)
    if not facts:
        return {}

    def _val(field: str):
        entry = facts.get(field)
        if isinstance(entry, dict):
            return entry.get("value")
        return entry

    prefill: Dict[str, Any] = {}

    # Intent: deterministic map from business act + settlement terms.
    activity = _val("activity")
    terms = _val("payment_terms")
    intent = _INTENT_MAP.get((activity, terms if terms in
                              ("CASH", "CREDIT") else None))
    if intent:
        prefill["semantic_intent"] = intent

    # Party: role decides the destination field (semantic, not keyword).
    party = _val("party")
    if isinstance(party, dict) and party.get("name"):
        role = party.get("role")
        if role == "customer":
            prefill["customer_name"] = party["name"]
        elif role == "supplier":
            prefill["supplier_name"] = party["name"]

    item = _val("item")
    if item:
        prefill["item_description"] = item
    quantity = _val("quantity")
    if quantity:
        prefill["item_quantity"] = quantity
        prefill["quantity"] = quantity
    amount = _val("amount")
    if amount:
        prefill["amount"] = amount
    if terms:
        prefill["payment_method"] = terms
    txn_date = _val("transaction_date")
    if txn_date:
        prefill["transaction_date"] = txn_date
    nature = _val("transaction_nature")
    if nature:
        prefill["transaction_nature"] = nature

    return prefill


# ===========================================================================
# PRIMARY API (Work Stream S2) — the semantic understanding layer proper
# ===========================================================================
#
# Everything above (``ground_facts`` / ``normalize_to_erp``) is the S1-era
# flat-fact path, kept working for compatibility and reused as the ONE
# grounding implementation. The functions below are what the agent calls: they
# produce and consume the formal SemanticIntent contract, which carries the
# INFORMATION STATE of every fact (EXPLICIT / SAFELY_INFERRED / AMBIGUOUS /
# MISSING) instead of a bare value.

#: Planner intents reachable from a semantic activity. The planner's own
#: whitelist (``_SEMANTIC_INTENTS``) is the final authority: a value produced
#: here that is not in it falls back to the keyword extractor.
_ACTIVITY_INTENT_MAP = {
    ("sale", "CREDIT"): "record_credit_sale",
    ("sale", "CASH"): "record_cash_sale",
    ("sale", None): "record_sale",
    ("invoice", None): "create_invoice",
    ("purchase", "CREDIT"): "record_credit_purchase",
    ("purchase", "CASH"): "record_cash_purchase",
    ("purchase", None): "record_purchase",
    ("expense", None): "record_expense",
    ("receipt", None): "record_receipt",
    ("payment", None): "record_payment",
    ("bill", None): "create_purchase_bill",
    ("quotation", None): "create_quotation",
    ("credit_note", None): "create_credit_note",
    ("purchase_return", None): "create_purchase_return",
    ("fixed_asset_purchase", None): "register_fixed_asset",
    ("fixed_asset_disposal", None): "dispose_fixed_asset",
    ("depreciation", None): "record_asset_depreciation",
    ("bank_transfer", None): "record_bank_transfer",
}


def _activities_from(parsed: dict) -> List[str]:
    """Every distinct business activity the request expresses (in order)."""
    raw = parsed.get("activities")
    values: List[Any] = []
    if isinstance(raw, list):
        values.extend(raw)
    elif raw:
        values.append(raw)
    single = parsed.get("activity")
    if single and single not in values:
        values.append(single)
    acts: List[str] = []
    for value in values:
        act = str(value or "").strip().lower().replace(" ", "_")
        if act in ACTIVITIES and act not in acts:
            acts.append(act)
    return acts


def intent_for_activity(activity: Optional[str], terms: Optional[str]) -> Optional[str]:
    """Deterministic normalization: activity × settlement terms -> intent."""
    normalized_terms = terms if terms in ("CASH", "CREDIT") else None
    return _ACTIVITY_INTENT_MAP.get((activity, normalized_terms)) or _ACTIVITY_INTENT_MAP.get(
        (activity, None)
    )


def _role_from_activity(activity: Optional[str]) -> Optional[str]:
    """Money direction decides the party role (never a keyword)."""
    act = str(activity or "").strip().lower()
    if act in MONEY_IN:
        return "customer"
    if act in MONEY_OUT:
        return "supplier"
    return None


# Canonical semantic name -> the legacy key the shared grounding reads, and
# the status/evidence keys it expects.
_LEGACY_KEY = {
    "party_name": "party",
    "item_description": "item",
    "quantity": "quantity",
    "amount": "amount",
    "payment_terms": "payment_terms",
    "transaction_date": "transaction_date",
    "transaction_nature": "transaction_nature",
}


def _to_legacy_shape(parsed: dict) -> dict:
    """Adapt the S2 ``facts[]`` schema to the shared grounding's input shape.

    One grounding implementation, two input dialects. An old-shape payload is
    returned untouched, so the S1 path and its tests behave identically.
    """
    entries = parsed.get("facts")
    if not isinstance(entries, list) or not entries:
        return parsed

    converted: Dict[str, Any] = {
        "activity": parsed.get("activity"),
        "activities": parsed.get("activities"),
        "ambiguous": parsed.get("ambiguous"),
        "missing": parsed.get("missing"),
        "unsupported": parsed.get("unsupported"),
        "clause_count": parsed.get("clause_count"),
    }
    status: Dict[str, str] = {}
    evidence: Dict[str, str] = {}
    extras: Dict[str, Any] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        canonical = canonical_field(entry.get("name"))
        if not canonical:
            continue
        value = entry.get("value")
        if value in (None, ""):
            continue
        state = normalize_state(entry.get("state"))
        ev = entry.get("evidence")
        if canonical == "party_name":
            converted["party"] = {
                "name": value,
                "role": entry.get("role") or entry.get("party_role"),
            }
            status["party"] = state
            if ev:
                evidence["party"] = str(ev)
        elif canonical == "party_role":
            role = str(value).strip().lower()
            existing = converted.get("party") or {}
            converted["party"] = {**existing, "role": role}
        elif canonical in _LEGACY_KEY:
            key = _LEGACY_KEY[canonical]
            if canonical == "item_description":
                # The shared grounding reads item as {"description": ...}.
                converted["item"] = {"description": value}
            else:
                converted[key] = value
            status[key] = state
            if ev:
                evidence[key] = str(ev)
        else:
            # Facts outside the legacy vocabulary (currency, accounts,
            # projects, references) are grounded separately below.
            extras[canonical] = {"value": value, "state": state, "evidence": ev}

    if status:
        converted["status"] = status
    if evidence:
        converted["evidence"] = evidence
    converted["_extras"] = extras
    return converted


#: Extra facts that are copied verbatim and therefore need a grounding check.
_VERBATIM_EXTRAS = {
    "account_name": 120,
    "bank_account": 120,
    "project_name": 120,
    "reference": 60,
}


def _ground_extras(converted: dict, normalized: str) -> Dict[str, SemanticFact]:
    """Ground the non-legacy facts (currency, accounts, projects, references)."""
    out: Dict[str, SemanticFact] = {}
    for name, entry in (converted.get("_extras") or {}).items():
        value = entry.get("value")
        state = normalize_state(entry.get("state"))
        if state not in (EXPLICIT, SAFELY_INFERRED):
            state = EXPLICIT
        if name == "currency":
            code = str(value or "").strip().upper()[:3]
            if len(code) == 3 and code.isalpha():
                out[name] = SemanticFact(name=name, value=code, state=state)
            continue
        limit = _VERBATIM_EXTRAS.get(name)
        if limit is None:
            continue
        text = _clean_candidate(value)
        if text and len(text) <= limit and _is_grounded(text, normalized):
            out[name] = SemanticFact(
                name=name,
                value=text,
                state=state,
                evidence=entry.get("evidence"),
            )
        else:
            log.info("semantic_rejected", field=name, reason="not_in_text")
    return out


def _ground_ambiguous(
    parsed: dict, intent: SemanticIntent, context: Optional[SemanticContext]
) -> int:
    """Ground AMBIGUOUS facts — candidates must be REAL records.

    A candidate the user never saw in the context package cannot be offered
    as a choice: accepting it would let the model invent an organization's
    records. Candidates are therefore intersected with the names the context
    actually supplied, and an ambiguity that loses all of them degrades to
    MISSING (we know something was said, nothing can be chosen between).
    """
    entries = parsed.get("ambiguous")
    if not isinstance(entries, list):
        return 0
    known = {n.lower() for n in (context.all_candidate_names() if context else [])}
    kept = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        field = canonical_field(entry.get("field"))
        if not field:
            continue
        existing = intent.fact(field)
        if existing is not None and existing.is_resolved:
            # Already resolved — ambiguity must never overwrite known truth.
            continue
        stated = entry.get("value")
        raw_candidates = entry.get("candidates")
        candidates: List[str] = []
        for candidate in raw_candidates if isinstance(raw_candidates, list) else []:
            name = _clean_candidate(candidate)
            if not name:
                continue
            if context is not None and known and name.lower() not in known:
                log.info(
                    "semantic_rejected", field=field, reason="candidate_not_in_context"
                )
                continue
            if name not in candidates:
                candidates.append(name)
        intent.set_fact(
            field,
            stated,
            state=AMBIGUOUS if len(candidates) >= 2 else MISSING,
            candidates=candidates,
            reason=str(entry.get("reason") or "")[:200] or None,
            required_for=str(entry.get("required_for") or "")[:120] or None,
        )
        kept += 1
    return kept


def _ground_missing(parsed: dict, intent: SemanticIntent) -> int:
    """Ground MISSING facts, refusing to ask for anything already known."""
    entries = parsed.get("missing")
    if not isinstance(entries, list):
        return 0
    kept = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        field = canonical_field(entry.get("field"))
        if not field:
            continue
        existing = intent.fact(field)
        if existing is not None and existing.is_resolved:
            # Rule I — never ask what is already known. This is the guard
            # that makes "the LLM re-asks a stated fact" impossible.
            log.info("semantic_rejected", field=field, reason="already_known")
            continue
        intent.set_fact(
            field,
            None,
            state=MISSING,
            reason=str(entry.get("reason") or "")[:200] or None,
            required_for=str(entry.get("required_for") or "")[:120] or None,
        )
        kept += 1
    return kept


def ground_semantic_intent(
    parsed: dict,
    raw_message: str,
    today: date,
    context: Optional[SemanticContext] = None,
) -> SemanticIntent:
    """Ground an LLM semantic payload into the SemanticIntent contract.

    Grounding is the hallucination gate: verbatim-copied text must occur in
    the user's own words, numbers must be traceable, dates must be resolvable
    from the text, enums must be canonical, and ambiguity candidates must be
    records the model was actually shown. Anything that fails is DROPPED with
    its information state preserved — never repaired, never guessed.
    """
    msg = raw_message or ""
    normalized = _norm(msg)
    converted = _to_legacy_shape(parsed)
    intent = SemanticIntent(source="semantic_llm")

    # ---- activities + clause structure (multi-activity requests) ---------
    intent.activities = _activities_from(parsed)
    try:
        clause_count = int(parsed.get("clause_count"))
    except (TypeError, ValueError):
        clause_count = 0
    intent.clause_count = max(1, clause_count or len(intent.activities) or 1)

    # ---- extras first (they never collide with the legacy fields) --------
    for name, fact in _ground_extras(converted, normalized).items():
        intent.set_fact(name, fact.value, state=fact.state, evidence=fact.evidence)

    # ---- the party: preserve the user's name even when the model's ROLE
    #      contradicts the money direction. Rule E says the direction of the
    #      act decides the role, so an inconsistent role is recorded as
    #      AMBIGUOUS (a targeted question) — the grounded NAME is preserved
    #      because the user genuinely stated it. Dropping the party here
    #      would discard information the user gave us.
    raw_party = converted.get("party")
    if isinstance(raw_party, dict):
        name = _clean_candidate(raw_party.get("name"))
        if name and len(name) <= 120 and _is_grounded(name, normalized):
            intent.set_fact(
                "party_name",
                name,
                state=normalize_state((converted.get("status") or {}).get("party")),
                evidence=(converted.get("evidence") or {}).get("party"),
            )
            stated_role = str(raw_party.get("role") or "").strip().lower()
            expected = _role_from_activity(intent.activity)
            if stated_role in PARTY_ROLES and (
                expected is None or stated_role == expected
            ):
                intent.set_fact(
                    "party_role",
                    stated_role,
                    state=SAFELY_INFERRED,
                    evidence="derived from the money direction of the act",
                )
            elif stated_role in PARTY_ROLES and expected is not None:
                intent.set_fact(
                    "party_role",
                    stated_role,
                    state=AMBIGUOUS,
                    reason=(
                        f"the stated role '{stated_role}' contradicts the money "
                        f"direction of a {intent.activity} (which implies "
                        f"'{expected}')"
                    ),
                )
                log.info("semantic_rejected", field="party_role", reason="inconsistent")
        elif name:
            log.info("semantic_rejected", field="party_name", reason="not_in_text")

    # ---- facts (ONE grounding implementation, shared with the S1 path) ---
    grounded = ground_facts(converted, msg, today)
    activity = intent.activity

    for legacy, name in (
        ("item", "item_description"),
        ("quantity", "quantity"),
        ("amount", "amount"),
        ("payment_terms", "payment_terms"),
        ("transaction_date", "transaction_date"),
        ("transaction_nature", "transaction_nature"),
    ):
        entry = grounded.get(legacy)
        if isinstance(entry, dict):
            value = entry.get("value")
            state = entry.get("status", EXPLICIT)
            evidence = entry.get("evidence")
        else:
            value, state, evidence = entry, EXPLICIT, None
        if value in (None, ""):
            continue
        intent.set_fact(name, value, state=state, evidence=evidence)

    if intent.fact("party_role") is None and activity:
        inferred = _role_from_activity(activity)
        if inferred:
            intent.set_fact(
                "party_role",
                inferred,
                state=SAFELY_INFERRED,
                evidence="money direction of the business act",
            )

    # ---- information-state analysis --------------------------------------
    _ground_ambiguous(parsed, intent, context)
    _ground_missing(parsed, intent)

    # ---- deterministic normalization: activities -> planner intents ------
    terms = intent.value("payment_terms")
    intents: List[str] = []
    for act in intent.activities:
        mapped = intent_for_activity(act, terms)
        if mapped and mapped not in intents:
            intents.append(mapped)
    intent.intents = intents

    log.info(
        "semantic_intent_grounded",
        activities=intent.activities,
        intents=intents,
        states=information_state_summary(intent),
        ambiguous=[f.name for f in intent.ambiguous],
        missing=[f.name for f in intent.missing],
    )
    return intent


async def extract_semantic_intent(
    user_message: str,
    *,
    orchestrator=None,
    context: Optional[SemanticContext] = None,
    today: Optional[date] = None,
) -> SemanticIntent:
    """PRIMARY ENTRY POINT: user request -> grounded SemanticIntent.

    ONE bounded LLM call carrying the reasoning rulebook and the bounded,
    organization-scoped ERP context. Every value is grounded in Python before
    it is returned. When the AI is unavailable the result is an EMPTY intent
    (``source="regex_fallback"``) and the deterministic pipeline behaves
    exactly as it did before this layer existed — the AI can never make a
    request fail.
    """
    parsed = await _understand(user_message, orchestrator, today, context)
    if not parsed:
        log.info("semantic_unavailable", source="regex_fallback")
        return SemanticIntent(source="regex_fallback")

    intent = ground_semantic_intent(
        parsed, user_message or "", today or date.today(), context
    )
    unsupported = parsed.get("unsupported")
    if isinstance(unsupported, list) and unsupported:
        log.info(
            "semantic_unsupported_request",
            items=[str(u)[:60] for u in unsupported[:5]],
        )
    return intent


# ---------------------------------------------------------------------------
# Targeted clarification — questions generated from the information state
# ---------------------------------------------------------------------------
#
# These questions come from what is ACTUALLY unresolved in this request, not
# from a static form. Two different laws apply:
#
#   AMBIGUOUS -> offer the competing real records as options (never guess)
#   MISSING   -> ask openly for exactly the absent value, phrased with the
#                facts that ARE known, so the question is specific
#
# A resolved fact can never produce a question here (the grounding already
# refuses to record MISSING for a known fact), which is what makes "never ask
# for what the user already said" structural rather than a prompt request.

_CUSTOMER_Q = "Who is the customer for this transaction?"
_SUPPLIER_Q = "Who is the supplier for this transaction?"
_DATE_Q = (
    "What is the transaction date? Reply TODAY, or the date as "
    "YYYY-MM-DD or DD/MM/YYYY (for example 2026-09-04 or 04/09/2026)."
)

_NATURE_OPTIONS = {
    "GOODS": "Stock goods I trade in",
    "SERVICE": "A service I provided",
    "ASSET_DISPOSAL": "A fixed asset I am selling",
    "OTHER_INCOME": "Other income",
}


def _subject_phrase(intent: SemanticIntent) -> str:
    """A short, human description of what IS known — used inside questions."""
    bits: List[str] = []
    quantity = intent.value("quantity")
    item = intent.value("item_description")
    if quantity and item:
        try:
            bits.append(f"the {float(quantity):g} {item}")
        except (TypeError, ValueError):
            bits.append(f"the {quantity} {item}")
    elif item:
        bits.append(f"the {item}")
    party = intent.value("party_name")
    if party:
        bits.append(f"with {party}")
    amount = intent.value("amount")
    if amount:
        try:
            bits.append(f"for {float(amount):,.0f}")
        except (TypeError, ValueError):
            pass
    return " ".join(bits).strip() or "this transaction"


def _ambiguous_question(fact: SemanticFact, intent: SemanticIntent) -> Dict[str, Any]:
    """A choice question built from the competing real records."""
    options = list(fact.candidates)
    if fact.name == "party_name":
        role = intent.value("party_role")
        noun = "customer" if role == "customer" else "supplier"
        question = (
            f'You mentioned "{fact.value}" but I found more than one matching '
            f"{noun}. Which one do you mean?"
        )
        options.append(f'Create a new {noun} "{fact.value}"')
    else:
        label = fact.name.replace("_", " ")
        question = (
            f'"{fact.value}" could mean more than one {label}. '
            "Which one do you mean?"
        )
    return {
        "field": fact.name,
        "kind": AMBIGUOUS,
        "question": question,
        "options": options[:5],
        "reason": fact.reason,
    }


def _missing_question(fact: SemanticFact, intent: SemanticIntent) -> Dict[str, Any]:
    """A specific, open question for exactly the absent value."""
    subject = _subject_phrase(intent)
    options: List[str] = []
    if fact.name == "amount":
        question = f"What is the total amount for {subject}?"
    elif fact.name == "party_name":
        role = intent.value("party_role")
        question = _SUPPLIER_Q if role == "supplier" else _CUSTOMER_Q
    elif fact.name == "item_description":
        question = f"What item or service is recorded for {subject}?"
    elif fact.name == "quantity":
        item = intent.value("item_description") or "this item"
        question = f"How many units of {item}?"
    elif fact.name == "transaction_date":
        question = _DATE_Q
    elif fact.name == "payment_terms":
        question = "Was this settled in cash now, or on credit?"
        options = ["CASH", "CREDIT"]
    elif fact.name == "transaction_nature":
        question = "How should this be treated for accounting purposes?"
        options = list(_NATURE_OPTIONS.values())
    elif fact.name == "party_role":
        question = "Is this money coming in (a customer) or going out (a supplier)?"
        options = ["Customer", "Supplier"]
    else:
        label = fact.name.replace("_", " ")
        question = f"What is the {label} for {subject}?"
    if fact.required_for:
        question += f" ({fact.required_for})"
    return {
        "field": fact.name,
        "kind": MISSING,
        "question": question,
        "options": options,
        "reason": fact.reason,
    }


def semantic_questions(
    intent: SemanticIntent, max_questions: int = 3
) -> List[Dict[str, Any]]:
    """Targeted questions derived from the information state of this request.

    Ambiguity is asked FIRST: until a named record is resolved, the missing
    facts cannot be interpreted against the right party. Never returns a
    question for a fact that is EXPLICIT or SAFELY_INFERRED.
    """
    if intent is None or intent.is_empty:
        return []
    questions: List[Dict[str, Any]] = []
    for fact in intent.ambiguous:
        if fact.candidates:
            questions.append(_ambiguous_question(fact, intent))
    for fact in intent.missing:
        if any(q["field"] == fact.name for q in questions):
            continue
        if fact.name == "party_role" and intent.value("party_name") is None:
            # The role is derivable once the party exists; asking for it
            # first would be asking a question about nothing.
            continue
        questions.append(_missing_question(fact, intent))
    return questions[: max(1, int(max_questions or 3))]


def semantic_field_to_planner_field(field: str, intent: Optional[SemanticIntent]) -> str:
    """Map a canonical semantic field to the planner's own missing-field name."""
    if field in ("party_name", "party_role"):
        role = intent.value("party_role") if intent is not None else None
        return "supplier_name" if role == "supplier" else "customer_name"
    if field == "payment_terms":
        return "payment_type"
    return field


def normalize_semantic_intent(intent: Optional[SemanticIntent]) -> Dict[str, Any]:
    """Deterministic normalization: SemanticIntent -> planner prefill.

    This is where meaning becomes ERP vocabulary: activity × terms -> intent,
    party role -> the customer/supplier field, and every RESOLVED fact keeps
    its value. AMBIGUOUS/MISSING facts are deliberately absent — they are
    questions, not values, and a question must never silently become data.
    """
    prefill: Dict[str, Any] = {}
    if intent is None or intent.is_empty:
        return prefill

    terms = intent.value("payment_terms")
    if intent.intents:
        prefill["semantic_intent"] = intent.intents[0]
    else:
        mapped = intent_for_activity(intent.activity, terms)
        if mapped:
            prefill["semantic_intent"] = mapped

    party = intent.value("party_name")
    role = intent.value("party_role")
    if party and role in PARTY_ROLES:
        prefill[f"{role}_name"] = party

    item = intent.value("item_description")
    if item:
        prefill["item_description"] = item
    quantity = intent.value("quantity")
    if quantity:
        prefill["item_quantity"] = quantity
        prefill["quantity"] = quantity
    amount = intent.value("amount")
    if amount:
        prefill["amount"] = amount
    if terms:
        prefill["payment_method"] = terms
    txn_date = intent.value("transaction_date")
    if txn_date:
        prefill["transaction_date"] = txn_date
    nature = intent.value("transaction_nature")
    if nature:
        prefill["transaction_nature"] = nature

    for extra in ("currency", "account_name", "bank_account", "project_name", "reference"):
        value = intent.value(extra)
        if value:
            prefill[extra] = value

    return prefill
