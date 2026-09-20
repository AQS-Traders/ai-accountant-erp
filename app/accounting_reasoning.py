"""
AI-Native ERP — LLM ACCOUNTING REASONING LOOP (Work Stream S3)
==============================================================

This module implements the architectural correction: the LLM is the PRIMARY
accounting reasoning layer, and Python is the controlled execution and
enforcement layer.

    User request
      → authenticated session
      → PRELIMINARY deterministic extraction only (literal, never a verdict)
      → LLM requests the books evidence it needs
      → Python validates + permission-checks + reads the evidence
      → LLM reassesses on LIVE BOOKS EVIDENCE and chooses a next step
          · ask a contextual question
          · explain that a prerequisite record is missing
          · propose a preparatory/correcting transaction
          · propose a settlement/allocation
          · propose a journal/document mutation
          · refuse (unsafe or unsupported)
          · complete
      → Python validates and enforces the proposal
      → execute through the trusted tool router
      → LLM reassesses the ACTUAL result
      → verify against the ledger before reporting success

What Python does here (enforcement, never interpretation):

* identity/organization scoping is injected, never taken from the model;
* evidence kinds, argument names and argument types are a CLOSED set — an
  unknown kind/argument is REJECTED and the rejection is fed back;
* the outcome must be exactly one of ask / request-evidence / propose / refuse
  / complete, with the mandated disclosure fields for a proposal;
* the round budget is bounded;
* nothing here decides what a phrase means, which party is required, whether an
  asset exists, or which workflow applies.  The caller (agent) additionally
  enforces the trusted tool registry, permissions and the confirmation gate
  before anything executes.

Architecture: docs/LLM_PRIMARY_REASONING_ARCHITECTURE.md
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import structlog

from app.books_evidence import (
    EVIDENCE_LABEL,
    EvidenceRequest,
    EvidenceResult,
    evidence_catalog_text,
    gather_evidence,
    parse_evidence_requests,
    render_evidence,
    render_evidence_compact,
    validate_evidence_request,
)

log = structlog.get_logger(__name__)

#: Round budget — evidence round(s), reassessment, result review.
MAX_REASONING_ROUNDS = 3

#: Outcome kinds the caller reacts to.
NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
NEEDS_INPUT = "NEEDS_INPUT"
PROPOSAL = "PROPOSAL"
REFUSAL = "REFUSAL"
COMPLETE = "COMPLETE"
UNSUPPORTED = "UNSUPPORTED"



# ---------------------------------------------------------------------------
# Prompt — what the reasoning layer is told
# ---------------------------------------------------------------------------

_SYSTEM_RULES = """
You are the PRIMARY ACCOUNTING REASONING LAYER of a chartered-accountant
assisted ERP. Python here does ONLY: identity/org checks, tool and schema
validation, permissions, confirmation gates, database constraints, journal
balancing, idempotency, execution, audit. You decide the accounting; Python
enforces and executes.

THE PRELIMINARY PLANNER MAY BE INCOMPLETE OR WRONG.
"PRELIMINARY EXTRACTION — may be corrected after accounting review" is regex/
keyword output, NOT a verdict. Do not follow its intent, workflow, party
requirement or missing-field list blindly; reclassify if the records disagree.

ASSUME NOTHING IS RECORDED UNTIL YOU HAVE CHECKED.
Before proposing a mutation, inspect the areas that could already hold the
event: documents, journal entries, subledgers, open items, the fixed-asset
register, the chart of accounts, the accounting period.

THE SAME PHRASE MEANS DIFFERENT THINGS IN DIFFERENT BOOKS.
"paid ABC 50,000" could be: settlement of a payable; an advance; a loan
repayment; an owner withdrawal; an immediately-paid purchase; payment of an
expense already recorded; a new transaction; a correction. Decide from the
RECORDS, never from the verb.

REASON ACROSS EVERY AREA — not just fixed assets: cash/bank, receivables,
inventory/catalog, prepaid, fixed assets, accumulated depreciation,
intangibles, other assets, payables, accruals, loans, taxes payable, customer
advances, capital, drawings, retained earnings, product/service revenue, other
income, disposal proceeds, discounts/returns, operating expenses, cost of
sales, depreciation, finance costs, tax expense, loss on disposal — and the
statements they feed (balance sheet, income statement, cash flow, trial
balance, general ledger, customer/supplier ledgers, subledgers).

WORK IN THIS ORDER:
1. What real-world economic event is this?
2. What is the user actually trying to accomplish?
3. What do the existing records show? (request evidence before assuming)
4. Which accounts/dimensions are affected — and which are NOT?
5. Which material facts are missing and can only the user supply them?
6. New event, correction, settlement, allocation, transfer, adjustment,
   disposal, reversal, or continuation?
7. What is the smallest correct next step?

HARD PROHIBITIONS:
- Never invent accounts, parties, assets, documents, amounts or dates.
- Never decide a treatment from a keyword.
- Never assume a party ledger is required (a cash-only event may need none).
- Never assume an asset or a document already exists.
- Never ask a generic question when the records could answer it.
- Never claim success: success comes from actual tool results and the verified
  ledger, not from your own text.
""".strip()

_PRELIMINARY_LABEL = (
    "PRELIMINARY EXTRACTION — may be corrected after accounting review"
)
#: The disclosure fields a mutation proposal MUST carry.  A proposal that omits
#: any of them is refused by Python (never executed).
REQUIRED_PROPOSAL_FIELDS = (
    "interpretation",
    "affected_records",
    "accounting_impact",
    "not_affected",
    "unresolved_uncertainty",
)

#: Accepted spellings of each disclosure field.
_PROPOSAL_FIELD_ALIASES = {
    "interpretation": ("interpretation", "business_interpretation"),
    "affected_records": ("affected_records",),
    "accounting_impact": ("accounting_impact",),
    "not_affected": ("not_affected",),
    "unresolved_uncertainty": ("unresolved_uncertainty",),
}


def _stated_disclosures(raw_proposal: Any) -> List[str]:
    """Which mandatory disclosures the model actually PROVIDED.

    Presence — not non-emptiness — is what the enforcement layer checks.  An
    explicit empty list is a statement ("nothing else is affected", "no
    unresolved uncertainty"), whereas an omitted key is silence.  Requiring a
    non-empty value would push the model to invent uncertainty, which this
    architecture forbids.
    """
    if not isinstance(raw_proposal, dict):
        return []
    return [
        name
        for name, aliases in _PROPOSAL_FIELD_ALIASES.items()
        if any(alias in raw_proposal for alias in aliases)
    ]


_RESPONSE_SHAPE = """
RESPONSE FORMAT — output ONLY this JSON object, no prose:

{
  "understanding": {
    "economic_event": "<the real-world event, in accounting terms>",
    "what_user_wants": "<the outcome the user is trying to achieve>",
    "basis": "<which records or wording support this reading>",
    "event_type": "new_event|correction|settlement|allocation|transfer|adjustment|disposal|reversal|continuation|report"
  },
  "evidence_requests": [
    {"kind": "<evidence kind>", "why": "<why the accounting decision needs it>",
     "args": {"terms": ["..."]}}
  ],
  "missing_material_facts": [
    {"fact": "<what is missing>", "why_material": "<why it blocks a correct posting>",
     "question": "<the question to ask the user>"}
  ],
  "question": null,
  "proposal": {
    "interpretation": "<the business interpretation you are proposing>",
    "affected_records": ["<record/ledger that will change>"],
    "accounting_impact": [
      {"account": "<account name or code from the chart of accounts>",
       "debit": 0, "credit": 0, "reason": "<why>"}
    ],
    "not_affected": ["<record/dimension that will deliberately NOT change>"],
    "unresolved_uncertainty": ["<anything still uncertain>"],
    "tools": [{"tool_name": "<registered tool>", "arguments": {}}],
    "confirmation": "<the exact sentence to show the user asking to proceed>"
  },
  "refusal": null,
  "complete": false
}

CHOOSING THE STEP — exactly one of these must be non-null/non-empty:
  a) "evidence_requests" non-empty -> you need the books before deciding.
     Request the lookups you need. Do not guess in the meantime.
  b) "question" non-null -> a material fact only the user can supply, asked in
     the context of the actual records (never a generic template question).
     "missing_material_facts" lists what that question resolves.
  c) "proposal" non-null -> you have enough evidence to propose a concrete next
     step. Every proposal MUST include interpretation, affected_records,
     accounting_impact, not_affected and unresolved_uncertainty.
  d) "refusal" non-null -> the request is unsafe, unsupported, or must not be
     executed; explain what is missing and what the safe alternative is.
  e) "complete": true -> the requested outcome is already achieved and verified
     by the actual records; explain what the records show.

RULES FOR PROPOSALS:
  - Tool names must come from the offered tool list; never invent one.
  - The accounting engine computes and enforces the final debits/credits and
    balancing. Your accounting_impact is the PROPOSED impact, stated so the
    user can see the consequence; it is verified, not trusted blindly.
  - List what will NOT be affected whenever a plausible misunderstanding exists
    (e.g. "no customer ledger will be created").
  - State unresolved uncertainty honestly; an empty list is a claim.
  - The confirmation sentence must be specific: which records, which amounts,
    which accounts, and what will not change.

AUDITOR MINDSET FOR EVERY AREA:
  - Cash/bank: which account, which direction, is the counterparty real?
  - Receivables/payables: does an open item exist, and is this settlement,
    advance, refund, or a new document?
  - Inventory/catalog: goods vs service vs asset; does the item exist?
  - Fixed assets: registered? cost, accumulated depreciation, book value,
    already disposed? Is the amount proceeds or acquisition value?
  - Accruals/prepayments: is the expense already recorded, or being recorded
    now, and in which period?
  - Loans/equity/drawings: is the cash movement a liability, capital, or a
    withdrawal?
  - Taxes: is a tax liability or recovery involved, and is a rate known?
  - Periods: is the intended date inside an OPEN period? If not, say so.
  - Corrections: prefer reversal + correct entry over silent edits.
""".strip()


@dataclass
class ReasoningFacts:
    """Everything the reasoning layer is allowed to see (already fetched)."""

    user_request: str = ""
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    preliminary: Dict[str, Any] = field(default_factory=dict)
    organization: Dict[str, Any] = field(default_factory=dict)
    accounting_period: Dict[str, Any] = field(default_factory=dict)
    org_policies: Dict[str, str] = field(default_factory=dict)
    economic_hints: Dict[str, Any] = field(default_factory=dict)
    prohibitions: List[str] = field(default_factory=list)
    previous_tool_results: List[Dict[str, Any]] = field(default_factory=list)
    previous_confirmations: List[Dict[str, Any]] = field(default_factory=list)
    today: str = ""


@dataclass
class ReasoningOutcome:
    """The parsed, validated decision of one reasoning round."""

    status: str
    understanding: Dict[str, Any] = field(default_factory=dict)
    evidence_requests: List[EvidenceRequest] = field(default_factory=list)
    evidence_results: List[EvidenceResult] = field(default_factory=list)
    rejected_evidence: List[str] = field(default_factory=list)
    question: Optional[Dict[str, Any]] = None
    missing_facts: List[Dict[str, Any]] = field(default_factory=list)
    proposal: Optional[Dict[str, Any]] = None
    #: Mandatory disclosures the model actually provided (presence, so an
    #: explicit empty list counts as the statement it is).
    stated_disclosures: List[str] = field(default_factory=list)
    refusal: Optional[Dict[str, Any]] = None
    violations: List[str] = field(default_factory=list)
    provider_failed: bool = False
    #: True when the provider was actually CALLED and failed (timeout, error,
    #: unparseable). False when the loop never got that far (no orchestrator,
    #: feature disabled, empty request) — the caller retries a provider only
    #: when it has NOT just failed in the same request.
    provider_attempted: bool = False
    rounds: int = 0

    @property
    def usable(self) -> bool:
        return not self.provider_failed

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "understanding": self.understanding,
            "evidence_requested": [r.kind for r in self.evidence_requests],
            "evidence": render_evidence_compact(self.evidence_results),
            "question": self.question,
            "proposal_tools": [
                t.get("tool_name") for t in (self.proposal or {}).get("tools", [])
            ],
            "violations": self.violations,
            "provider_failed": self.provider_failed,
            "rounds": self.rounds,
        }


# ---------------------------------------------------------------------------
# Prompt assembly — every block is labelled for what it actually is
# ---------------------------------------------------------------------------


def _render_history(history: Sequence[Dict[str, str]]) -> str:
    if not history:
        return "(none)"
    lines = []
    for qa in history[-8:]:
        lines.append(f"  Q: {qa.get('question', '')}")
        lines.append(f"  A: {qa.get('answer', '')}")
    return "\n".join(lines)


def _render_dict_block(data: Dict[str, Any], limit: int = 40) -> str:
    if not data:
        return "(none provided)"
    lines = []
    for idx, (key, value) in enumerate(data.items()):
        if idx >= limit:
            lines.append("  …")
            break
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str, ensure_ascii=False)[:400]
        lines.append(f"  - {key}: {value}")
    return "\n".join(lines)


def build_reasoning_prompt(
    facts: ReasoningFacts,
    *,
    evidence_block: str = "",
    violations: Sequence[str] = (),
    offered_tools: Sequence[str] = (),
) -> str:
    """Assemble the labelled reasoning prompt.

    The labels are contractual: deterministic Python output is always labelled
    PRELIMINARY, and retrieved records are always labelled LIVE BOOKS EVIDENCE.
    """
    parts: List[str] = [_SYSTEM_RULES, ""]

    parts.append(
        "ORIGINAL USER REQUEST (preserve this wording — it is the "
        "authoritative statement of what the user wants):"
    )
    parts.append(f"  {facts.user_request or '(empty)'}")
    parts.append("")

    parts.append(
        "CONVERSATION HISTORY (questions already asked and answers already "
        "given — never ask these again):"
    )
    parts.append(_render_history(facts.conversation_history))
    parts.append("")

    parts.append(f"{_PRELIMINARY_LABEL}:")
    parts.append(_render_dict_block(facts.preliminary or {}))
    parts.append("")

    parts.append("ORGANIZATION PROFILE:")
    parts.append(_render_dict_block(facts.organization or {}))
    parts.append("")

    parts.append("ACCOUNTING PERIOD:")
    parts.append(_render_dict_block(facts.accounting_period or {}))
    parts.append("")

    if facts.org_policies:
        parts.append("ORGANIZATION POLICIES (defaults the user established):")
        parts.append(_render_dict_block(facts.org_policies))
        parts.append("")

    if facts.economic_hints:
        parts.append(
            "TRANSACTION CLASSIFICATION HINTS (preliminary, deterministic — "
            "override them if the records say otherwise):"
        )
        parts.append(_render_dict_block(facts.economic_hints))
        parts.append("")

    if facts.prohibitions:
        parts.append(
            "KNOWN PROHIBITIONS (enforced by the executor — do not propose "
            "these):"
        )
        for item in facts.prohibitions[:10]:
            parts.append(f"  · {item}")
        parts.append("")

    if offered_tools:
        parts.append("OFFERED TOOLS (the only tool names a proposal may use):")
        parts.append("  " + ", ".join(sorted(offered_tools)))
        parts.append("")

    parts.append(evidence_catalog_text())
    parts.append("")

    if evidence_block:
        parts.append(evidence_block)
        parts.append("")
        parts.append(
            "The section above is LIVE BOOKS EVIDENCE retrieved from this "
            "organization's books. Reassess the request against it. If it "
            "contradicts the preliminary extraction, follow the records."
        )
        parts.append("")

    if facts.previous_tool_results:
        parts.append(
            "PREVIOUS TOOL RESULTS (actual outcomes of already-executed "
            "operations — read them before proposing anything new):"
        )
        parts.append(_render_dict_block({"results": facts.previous_tool_results}))
        parts.append("")

    if facts.previous_confirmations:
        parts.append("PREVIOUS CONFIRMATIONS (what the user already approved):")
        parts.append(_render_dict_block({"confirmed": facts.previous_confirmations}))
        parts.append("")

    if violations:
        parts.append(
            "REJECTED BY THE EXECUTION LAYER (your previous answer was refused "
            "by Python; correct it — do not repeat it):"
        )
        for item in violations:
            parts.append(f"  · {item}")
        parts.append("")

    parts.append(f"TODAY: {facts.today or '(unset)'}")
    parts.append("")
    parts.append(_RESPONSE_SHAPE)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Parsing + Python validation of the model's decision
# ---------------------------------------------------------------------------

_JSON_START = re.compile(r"\{", re.S)


def parse_reasoning_response(raw: Any) -> Optional[Dict[str, Any]]:
    """Extract the JSON decision object from a provider response.

    Tolerates code fences and surrounding prose; returns None when no JSON
    object can be found (the caller treats that as a provider failure and
    degrades to the deterministic path rather than inventing a decision).
    """
    text = raw if isinstance(raw, str) else ""
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — fall through to brace scanning
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    match = _JSON_START.search(text)
    if not match:
        return None
    depth = 0
    for idx in range(match.start(), len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    candidate = json.loads(text[match.start(): idx + 1])
                except Exception:  # noqa: BLE001
                    return None
                return candidate if isinstance(candidate, dict) else None
    return None


def _clean_list(value: Any, limit: int = 12, item_limit: int = 300) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            item = (
                item.get("name")
                or item.get("text")
                or json.dumps(item, default=str, ensure_ascii=False)
            )
        text = str(item or "").strip()
        if text:
            out.append(text[:item_limit])
    return out


def _clean_question(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        text = raw.strip()
        return {"text": text, "options": []} if text else None
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or raw.get("question") or "").strip()
    if not text:
        return None
    return {"text": text[:1500], "options": _clean_list(raw.get("options"), 6, 80)}


def _clean_tool_calls(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name") or item.get("name") or "").strip()
        if not name:
            continue
        args = item.get("arguments")
        out.append(
            {"tool_name": name, "arguments": args if isinstance(args, dict) else {}}
        )
    return out


def _clean_impact(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        account = str(item.get("account") or item.get("account_name") or "").strip()
        if not account:
            continue
        entry: Dict[str, Any] = {"account": account[:200]}
        for side in ("debit", "credit"):
            value = item.get(side)
            if isinstance(value, (int, float)):
                entry[side] = float(value)
        reason = str(item.get("reason") or "").strip()
        if reason:
            entry["reason"] = reason[:200]
        out.append(entry)
    return out


def _clean_proposal(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "interpretation": str(
            raw.get("interpretation") or raw.get("business_interpretation") or ""
        ).strip()[:1500],
        "affected_records": _clean_list(raw.get("affected_records")),
        "accounting_impact": _clean_impact(raw.get("accounting_impact")),
        "not_affected": _clean_list(raw.get("not_affected")),
        "unresolved_uncertainty": _clean_list(raw.get("unresolved_uncertainty")),
        "tools": _clean_tool_calls(raw.get("tools") or raw.get("tool_calls")),
        "confirmation": str(
            raw.get("confirmation") or raw.get("confirmation_request") or ""
        ).strip()[:1500],
    }


def validate_outcome(
    outcome: ReasoningOutcome,
    *,
    offered_tools: Sequence[str] = (),
    forbidden_tools: Sequence[str] = (),
) -> List[str]:
    """Deterministic enforcement of a decision.  Returns violation messages.

    A violation means the decision is NOT executed; it is fed back to the model
    (bounded by the round budget) instead of being repaired silently.
    """
    violations: List[str] = []
    proposal = outcome.proposal or {}
    tools = _clean_tool_calls(proposal.get("tools"))

    if outcome.status == NEEDS_EVIDENCE:
        if not outcome.evidence_requests:
            violations.append(
                "Status NEEDS_EVIDENCE requires at least one evidence request."
            )
        return violations

    if outcome.status == NEEDS_INPUT:
        if not outcome.question or not str(outcome.question.get("text") or "").strip():
            violations.append("Status NEEDS_INPUT requires a question for the user.")
        return violations

    if outcome.status == PROPOSAL:
        if not tools:
            violations.append(
                "A proposal must name at least one tool from the offered list."
            )
        allowed = set(offered_tools or ())
        if allowed:
            for call in tools:
                if call["tool_name"] not in allowed:
                    violations.append(
                        f"Tool '{call['tool_name']}' is not in the offered tool "
                        "list — propose only offered tools."
                    )
        forbidden = set(forbidden_tools or ())
        for call in tools:
            if call["tool_name"] in forbidden:
                violations.append(
                    f"Tool '{call['tool_name']}' is prohibited for this economic "
                    "event by the organization's accounting policy."
                )
        stated = set(outcome.stated_disclosures)
        for field_name in REQUIRED_PROPOSAL_FIELDS:
            if field_name not in stated:
                violations.append(
                    f"A proposal must state '{field_name}'. Every proposal "
                    "must disclose its interpretation, the records it affects, "
                    "its proposed accounting impact, what it deliberately does "
                    "NOT affect, and its unresolved uncertainty — an empty "
                    "list is a claim, an omitted field is silence."
                )
        if not str(proposal.get("interpretation") or "").strip():
            violations.append(
                "A proposal must state its business interpretation in words."
            )
        if not str(proposal.get("confirmation") or "").strip():
            violations.append(
                "A proposal that changes records must include the exact "
                "confirmation sentence to show the user."
            )
        return violations

    return violations


def proposed_mutation_tools(outcome: ReasoningOutcome) -> List[Dict[str, Any]]:
    """The proposed tool calls that would CHANGE records.

    Read-only tools are identified from the trusted registry — never from the
    model's own claim about what a tool does.
    """
    from app.tool_execution import is_read_only_tool

    tools = _clean_tool_calls((outcome.proposal or {}).get("tools"))
    return [call for call in tools if not is_read_only_tool(call["tool_name"])]


# ---------------------------------------------------------------------------
# The loop — request evidence, reassess, decide (bounded, never raising)
# ---------------------------------------------------------------------------


def _status_from_decision(parsed: Dict[str, Any]) -> str:
    """Map the model's decision to exactly one outcome kind.

    Precedence is deliberate: an explicit refusal or completion wins, then a
    proposal, then a question, then a request for evidence.  The model cannot
    smuggle two decisions in one payload — the first applicable one is used.
    """
    refusal = parsed.get("refusal")
    if isinstance(refusal, dict) and (
        refusal.get("reason") or refusal.get("explanation")
    ):
        return REFUSAL
    if parsed.get("complete") is True:
        return COMPLETE
    if isinstance(parsed.get("proposal"), dict) and parsed["proposal"]:
        return PROPOSAL
    if parsed.get("question"):
        return NEEDS_INPUT
    if parsed.get("evidence_requests"):
        return NEEDS_EVIDENCE
    if parsed.get("missing_material_facts"):
        return NEEDS_INPUT
    return UNSUPPORTED


def _outcome_from_parsed(parsed: Dict[str, Any], *, rounds: int) -> ReasoningOutcome:
    status = _status_from_decision(parsed)
    understanding = parsed.get("understanding")
    return ReasoningOutcome(
        status=status,
        understanding=understanding if isinstance(understanding, dict) else {},
        evidence_requests=parse_evidence_requests(parsed.get("evidence_requests")),
        question=_clean_question(parsed.get("question")),
        missing_facts=[
            {
                "fact": str(f.get("fact") or "")[:200],
                "why_material": str(f.get("why_material") or f.get("why") or "")[:300],
                "question": str(f.get("question") or "")[:500],
            }
            for f in (parsed.get("missing_material_facts") or [])
            if isinstance(f, dict)
        ][:6],
        proposal=_clean_proposal(parsed.get("proposal")),
        stated_disclosures=_stated_disclosures(parsed.get("proposal")),
        refusal=parsed.get("refusal") if isinstance(parsed.get("refusal"), dict) else None,
        rounds=rounds,
    )


def refusal_text(refusal: Optional[Dict[str, Any]]) -> str:
    """Human-readable refusal/conclusion text from the model's payload."""
    if not refusal:
        return ""
    return str(
        refusal.get("reason")
        or refusal.get("explanation")
        or refusal.get("message")
        or ""
    ).strip()


async def run_reasoning_loop(
    facts: ReasoningFacts,
    *,
    organization_id: uuid.UUID,
    auth: Any = None,
    session_id: Optional[uuid.UUID] = None,
    orchestrator: Any = None,
    offered_tools: Sequence[str] = (),
    forbidden_tools: Sequence[str] = (),
    max_rounds: int = MAX_REASONING_ROUNDS,
    timeout_seconds: Optional[float] = None,
    step_logger: Any = None,
) -> ReasoningOutcome:
    """Run the LLM accounting reasoning loop against the live books.

    Returns a :class:`ReasoningOutcome`.  Provider unavailability (disabled,
    down, timeout, unparseable) yields ``provider_failed=True`` so the caller
    keeps its deterministic behaviour instead of fabricating a decision.
    """
    import asyncio

    from app.config import get_settings

    settings = get_settings()
    budget = (
        timeout_seconds
        if timeout_seconds is not None
        else float(getattr(settings, "accounting_reasoning_timeout", 30.0))
    )
    total_budget = float(
        getattr(settings, "accounting_reasoning_total_timeout", 45.0)
    )
    # Optional dedicated model chain for this one call (see the config field).
    # It is passed ONLY to providers that declare it, so the stage keeps working
    # with any orchestrator implementation (and with test doubles).
    chain = [
        m.strip()
        for m in str(getattr(settings, "accounting_reasoning_model_chain", "") or "").split(",")
        if m.strip()
    ]
    generate = getattr(orchestrator, "generate_text", None) if orchestrator else None
    chain_supported = False
    if chain and callable(generate):
        try:
            import inspect

            params = inspect.signature(generate).parameters
            chain_supported = "model_chain" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            chain_supported = False
    if not callable(generate) or not facts.user_request.strip():
        return ReasoningOutcome(status=UNSUPPORTED, provider_failed=True)

    def _notify(event: str, payload: Dict[str, Any]) -> None:
        """Best-effort audit hook — never allowed to break the loop."""
        if not callable(step_logger):
            return
        try:
            step_logger(event, payload)
        except Exception:  # noqa: BLE001
            log.warning("accounting_reasoning.step_logger_failed", event=event)

    evidence_block = ""
    gathered: List[EvidenceResult] = []
    rejected: List[str] = []
    violations: List[str] = []
    outcome = ReasoningOutcome(status=UNSUPPORTED)
    rounds_used = 0
    # The whole stage is bounded, not just each round: three slow rounds must
    # never add up to a minute of user-visible waiting.
    deadline = asyncio.get_running_loop().time() + total_budget

    for round_index in range(max(1, max_rounds)):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0.5:
            log.warning(
                "accounting_reasoning.failed",
                reason="total_budget_exhausted",
                round=round_index + 1,
            )
            return ReasoningOutcome(
                status=UNSUPPORTED,
                provider_failed=True,
                provider_attempted=rounds_used > 0,
                evidence_results=gathered,
                rounds=rounds_used,
            )
        round_budget = min(budget, remaining)
        rounds_used = round_index + 1
        prompt = build_reasoning_prompt(
            facts,
            evidence_block=evidence_block,
            violations=violations,
            offered_tools=offered_tools,
        )
        try:
            call_kwargs: Dict[str, Any] = {"prompt": prompt}
            if chain and chain_supported:
                call_kwargs["model_chain"] = chain
            raw = await asyncio.wait_for(
                generate(**call_kwargs), timeout=round_budget
            )
        except asyncio.TimeoutError:
            log.warning(
                "accounting_reasoning.failed",
                reason="timeout",
                round=rounds_used,
                budget_s=round(round_budget, 2),
            )
            return ReasoningOutcome(
                status=UNSUPPORTED,
                provider_failed=True,
                provider_attempted=True,
                evidence_results=gathered,
                rounds=rounds_used,
            )
        except Exception as exc:  # noqa: BLE001 — the stage must never raise
            log.warning(
                "accounting_reasoning.failed",
                reason="provider_error",
                detail=str(exc)[:200],
                round=rounds_used,
            )
            return ReasoningOutcome(
                status=UNSUPPORTED,
                provider_failed=True,
                provider_attempted=True,
                evidence_results=gathered,
                rounds=rounds_used,
            )

        parsed = parse_reasoning_response(raw)
        if not parsed:
            log.info("accounting_reasoning.failed", reason="unparseable_response")
            return ReasoningOutcome(
                status=UNSUPPORTED,
                provider_failed=True,
                provider_attempted=True,
                evidence_results=gathered,
                rounds=rounds_used,
            )

        outcome = _outcome_from_parsed(parsed, rounds=rounds_used)
        outcome.evidence_results = list(gathered)

        # (a) The model wants the books before deciding.
        if outcome.status == NEEDS_EVIDENCE:
            valid: List[EvidenceRequest] = []
            for request in outcome.evidence_requests:
                problem = validate_evidence_request(request)
                if problem:
                    rejected.append(problem)
                else:
                    valid.append(request)
            outcome.rejected_evidence = list(rejected)
            if not valid:
                # Every request was refused — tell the model exactly why and let
                # it choose again rather than silently substituting a lookup.
                violations = list(rejected) or [
                    "No usable evidence request was provided."
                ]
                continue
            _notify(
                "EVIDENCE_REQUESTED",
                {"round": rounds_used, "kinds": [r.kind for r in valid]},
            )
            new_results = await gather_evidence(
                valid,
                organization_id=organization_id,
                auth=auth,
                session_id=session_id,
            )
            gathered.extend(new_results)
            evidence_block = render_evidence(gathered)
            _notify(
                "EVIDENCE_RETURNED",
                {
                    "round": rounds_used,
                    "summary": render_evidence_compact(new_results),
                    "label": EVIDENCE_LABEL,
                },
            )
            violations = []
            continue

        # (b) Python enforces the decision before it is used.
        violations = validate_outcome(
            outcome, offered_tools=offered_tools, forbidden_tools=forbidden_tools
        )
        if violations:
            _notify(
                "DECISION_REJECTED",
                {
                    "round": rounds_used,
                    "status": outcome.status,
                    "violations": violations,
                },
            )
            outcome.violations = list(violations)
            if round_index + 1 < max_rounds:
                continue
            outcome.status = UNSUPPORTED
            outcome.evidence_results = list(gathered)
            return outcome

        # (c) Accepted decision.
        outcome.evidence_results = list(gathered)
        _notify(
            "REASONING_DECISION",
            {
                "round": rounds_used,
                "status": outcome.status,
                "understanding": outcome.understanding,
                "evidence": render_evidence_compact(gathered),
            },
        )
        return outcome

    # Round budget exhausted without an accepted decision.  This is a
    # TERMINAL outcome: there are no rounds left to satisfy a dangling
    # NEEDS_EVIDENCE, and — production incident a59b889c-dc26-4ba6-a90a-
    # e0cb6085dacf — falling off the end of the function implicitly returned
    # None, which crashed the caller with AttributeError: 'NoneType' object
    # has no attribute 'as_dict'.  Every reachable terminal path must return
    # a populated ReasoningOutcome.
    outcome.evidence_results = list(gathered)
    outcome.rounds = rounds_used
    if violations:
        outcome.violations = list(violations)
    outcome.status = UNSUPPORTED
    return outcome


# ---------------------------------------------------------------------------
# Preliminary extraction — LITERALS ONLY, never a final accounting decision
# ---------------------------------------------------------------------------


def preliminary_extraction(
    user_message: str,
    *,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract only obviously-literal values from the user's own words.

    This is the ONLY deterministic interpretation the architecture permits
    before the LLM sees the books.  Everything here is:

    * PRESERVED WORDING — the original request is carried verbatim;
    * LITERAL VALUES — an amount, a date, a named party, an item, a payment
      channel, a document reference, taken from the text itself;
    * CANDIDATE SUBJECT AREAS — hints that tell the model which areas of the
      books might be relevant.  They are explicitly NOT a workflow decision.

    The returned ``label`` is contractual: it is rendered into the reasoning
    prompt so the model knows this section is provisional and overridable.
    """
    from datetime import date as _date

    message = (user_message or "").strip()
    literals: Dict[str, Any] = {}
    subject_areas: List[str] = []
    provisional_intent: Optional[str] = None

    try:
        from app.planner import (
            _extract_amount,
            _extract_date,
            _extract_item,
            _extract_payment_method,
            _identify_intent,
        )

        msg_lower = message.lower()
        amount = _extract_amount(message)
        if amount is not None:
            literals["amount"] = amount
        txn_date = _extract_date(message, msg_lower)
        if txn_date:
            literals["transaction_date"] = txn_date
        item = _extract_item(message)
        if item:
            literals["item_description"] = item
        channel = _extract_payment_method(msg_lower)
        if channel:
            literals["payment_channel"] = channel
        provisional_intent = _identify_intent(msg_lower)
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        log.info("accounting_reasoning.preliminary_helpers_unavailable", error=str(exc)[:120])

    # Named parties are reported as CANDIDATES for the model's own lookup, not
    # as a resolved party: the model must confirm them against the books.
    candidates: List[str] = []
    try:
        from app.semantic_context import extract_candidate_terms

        candidates = extract_candidate_terms(message)[:5]
    except Exception as exc:  # noqa: BLE001
        log.info("accounting_reasoning.candidate_terms_unavailable", error=str(exc)[:120])
    if candidates:
        literals["named_parties_or_items_candidates"] = candidates

    reference = None
    for token in re.findall(r"\b(?:INV|BILL|RCPT|PAY|PO|JV)[-/\s]?\d{2,}\b", message, re.I):
        reference = token.strip()
        break
    if reference:
        literals["document_reference"] = reference

    # Candidate subject areas — HINTS ONLY.  They tell the model where to look
    # first; they never decide the treatment, the workflow, or the accounts.
    for token, area in (
        ("asset", "fixed_assets"),
        ("depreciat", "fixed_assets"),
        ("sold", "revenue"),
        ("sale", "revenue"),
        ("supplied", "revenue"),
        ("invoice", "receivables"),
        ("receipt", "receivables"),
        ("received", "receivables"),
        ("paid", "payables"),
        ("payment", "payables"),
        ("bill", "payables"),
        ("expense", "expenses"),
        ("bought", "purchases"),
        ("purchase", "purchases"),
        ("loan", "loans"),
        ("capital", "equity"),
        ("drawing", "equity"),
        ("tax", "tax"),
        ("bank", "bank"),
        ("cash", "cash"),
    ):
        if token in message.lower() and area not in subject_areas:
            subject_areas.append(area)

    return {
        "label": _PRELIMINARY_LABEL,
        "original_request": message,
        "literals": literals,
        "candidate_subject_areas": subject_areas,
        "provisional_intent_hint": provisional_intent,
        "note": (
            "Provisional only. The accounting treatment must be determined by "
            "reading the live books — correct any of this that the records "
            "contradict."
        ),
        "today": today or _date.today().isoformat(),
    }