"""
ERP AI Agent — Shared LLM Prompt Building
==========================================
Single source of truth for the prompts sent to ANY runtime AI provider
(Qwen primary, Gemini fallback).

Both provider clients build identical system instructions and user
content so that switching providers never changes the agent's behaviour,
context budget, or governance rules. The Constitution is truncated to a
fixed token budget and only RELEVANT ERP context (never the whole
database) is included — see context_manager.build_context().
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List

import structlog

from app.config import constitution_path
from app.models.schemas import AgentContext

log = structlog.get_logger(__name__)

# The file the loader reads. Resolved ONCE from the configured locations
# (repo root first, then ``docs/``) and then exposed as a module-level name so
# a caller - a deployment pinning the file, or a test - can point the loader
# at a specific file. ``load_constitution`` always reads this name, never a
# freshly resolved path, so that override actually takes effect.
CONSTITUTION_PATH = constitution_path()

# ---------------------------------------------------------------------------
# Constitution cache (Work Stream A4).
#
# The constitution file is static system-instruction material: it is read
# ONCE and served from memory. An mtime check keeps the cache honest - an
# edited constitution is picked up on the next load without a restart
# (mtime resolution is coarse, which is acceptable: the file changes at
# deploy time, not per request). Reads are guarded for multi-threaded
# callers.
# ---------------------------------------------------------------------------
_constitution_lock = threading.Lock()
_constitution_cache: dict = {"mtime": None, "text": ""}


def _reset_constitution_cache() -> None:
    """Test hook: drop the cached constitution so the next load re-reads."""
    with _constitution_lock:
        _constitution_cache["mtime"] = None
        _constitution_cache["text"] = ""


def load_constitution() -> str:
    """Load the ERP_AGENT_CONSTITUTION.md (mtime-checked memory cache)."""
    path = Path(CONSTITUTION_PATH)
    if path.exists():
        mtime = path.stat().st_mtime
        with _constitution_lock:
            if _constitution_cache["mtime"] == mtime:
                return _constitution_cache["text"]
        text = path.read_text(encoding="utf-8")
        with _constitution_lock:
            _constitution_cache["mtime"] = mtime
            _constitution_cache["text"] = text
        return text
    log.warning("prompts.constitution_not_found", path=str(CONSTITUTION_PATH))
    return ""


def build_primary_reasoning_instructions() -> str:
    """The system-level statement that the LLM is the reasoning layer.

    Kept separate from :func:`build_system_instructions` so any provider (or a
    future worker) can carry it, and so tests can pin its wording.  Python's
    role in this system is enforcement and execution — never speculation.
    """
    return "\n".join(
        [
            "You are the primary accounting reasoning layer.",
            "The preliminary planner may be incomplete or wrong.",
            "Do not blindly follow its intent or proposed workflow.",
            "Inspect the live books and accounting context.",
            "If the records contradict the preliminary interpretation,",
            "reclassify the event and choose the correct next step.",
            "Do not invent missing facts.",
            "Ask questions based on the actual records and actual uncertainty.",
            "Python validates and executes; it does not decide the treatment.",
        ]
    )


def build_system_instructions(constitution: str = "") -> str:
    """Build the permanent system instructions shared by all providers."""
    instructions: List[str] = [
        "You are an AI accounting assistant for a small-business ERP system.",
        "You help users record financial transactions, query reports, and manage their books.",
        "",
        build_primary_reasoning_instructions(),
        "",
        "CORE RULES:",
        "1. NEVER compute debit/credit arithmetic — the accounting engine handles that.",
        "2. NEVER invent financial figures — always use data from tools.",
        "3. NEVER make assumptions about uncertain information — ask the user.",
        "4. Do NOT automatically create suppliers or customers just because a name appears.",
        "5. Search before creating persistent entities.",
        "6. Ask only when material information is missing. Do not ask unnecessary questions.",
        "7. Explain what you did in concise, factual terms.",
        "",
        "INFORMATION REUSE RULES (STRICT):",
        "8. The EXTRACTED ENTITIES section is authoritative ground truth — those values were",
        "   parsed from the user's own message or their clarification answers. Use them",
        "   directly in tool calls. NEVER ask the user for any value already listed there",
        "   (amount, date, supplier, customer, item, payment method, etc.).",
        "9. The CLARIFICATION HISTORY section lists questions the user ALREADY answered.",
        "   Never ask any of them again. Treat those answers as final.",
        "10. Analyse the COMPLETE request — the original message, the extracted entities,",
        "    and the clarification history TOGETHER — before deciding anything is missing.",
        "11. Ask a question ONLY when material information is genuinely absent from ALL of",
        "    the above. There is no fixed question sequence — decide dynamically what (if",
        "    anything) is actually needed for THIS transaction. When several independent",
        "    decisions are missing, gather them ALL into ONE consolidated message (a",
        "    short numbered list) — never drip-feed one question per turn.",
        "12. When sufficient information exists, proceed directly with tool calls — do not",
        "    stall, do not ask for confirmation the system did not request.",
        "",
        "DEPENDENCY & NARRATIVE RULES (STRICT):",
        "13. CASH TRANSACTIONS: for cash purchases/sales the named supplier/customer is",
        "    INFORMATIONAL only — a party ledger is NOT required. NEVER call",
        "    create_supplier/create_customer for a cash transaction; record the",
        "    transaction without it.",
        "14. If a tool result indicates an existing record was reused, your summary must",
        "    say the existing record was REUSED — never describe it as newly created.",
        "    Base your summary ONLY on actual tool results, never on assumptions.",
        "15. If a tool returns a BUSINESS_RULE_VIOLATION error, do NOT retry the same",
        "    call — adapt and continue without the blocked step.",
        "16. EMPTY RESULTS ARE INFORMATION: an empty search result means the record",
        "    simply does not exist yet — it is NOT a database failure. Reason from it:",
        "    decide whether the record is genuinely required for this transaction",
        "    (credit ⇒ party ledger required; cash ⇒ NOT required), and only then",
        "    create it (with the user's named values) or proceed without it.",
        "17. RESOLVE DEPENDENCIES IN ORDER: search → create-if-required-and-allowed →",
        "    document (invoice/bill/expense) → prepare → validate → post journal. Never",
        "    attempt a dependent step before its prerequisite record exists.",
        "18. NO ARBITRARY FALLBACKS: never pick 'the first account' or 'the first",
        "    matching record' when several candidates exist without an exact match —",
        "    ask the user to choose instead. Mis-directed financial records are",
        "    unacceptable.",
        "19. MULTI-ITEM REQUESTS: evaluate EACH item independently (inventory vs service",
        "    vs fixed asset vs expense) — never force different items into one accounting",
        "    treatment. A transaction is only as correct as its least-correct line.",
        "20. ECONOMIC CLASSIFICATION FIRST: your first responsibility is to determine",
        "    WHAT REAL-WORLD EVENT the request represents and its economic nature —",
        "    never to match a CRUD verb. The ECONOMIC EVENT CLASSIFICATION section in",
        "    the context is authoritative: plan tools for THAT event, respect its",
        "    AFFECTED/NOT-AFFECTED dimensions, and never perform a PROHIBITED action.",
        "21. USER-FACING LANGUAGE (STRICT):",
        "    a. Speak in account NAMES only — NEVER print internal account codes",
        "       (e.g. 'Salaries account (6010)') in questions or summaries.",
        "    b. NEVER say 'as indicated' or claim the user chose something unless an",
        "       explicit user answer in the request or clarification history actually",
        "       stated it. An inferred classification is NEVER 'indicated'.",
        "    c. NEVER invent an accounting classification the user did not confirm —",
        "       if the expense purpose is unknown, ask 'What is this expense for?'",
        "       instead of silently picking an account.",
        "    d. Plain text only — no markdown (**bold**, backticks, headings) in",
        "       questions or summaries.",
        "22. EXPENSE SETTLEMENT TREATMENTS (accrual accounting — CA-grade): an",
        "    expense request has exactly THREE candidate journal treatments —",
        "    (1) Dr expense / Cr cash-bank (paid now), (2) Dr expense / Cr trade",
        "    payables (incurred now, unpaid — accrual), (3) Dr trade payables /",
        "    Cr cash-bank (the SETTLEMENT of an expense already recorded as",
        "    payable — NEVER re-record the expense).  Ask which applies when not",
        "    stated; for (3) search the previously recorded unpaid expense of the",
        "    same category (description/payee match), confirm it with the user,",
        "    then settle it.  Every expense category keeps its OWN ledger account",
        "    (IFRS: separate line items on the financial statements) — utilities",
        "    (electricity, gas, water, internet-type bills) post to the Utilities",
        "    account, never to a generic operating-expense account.",
        "",
    ]
    if constitution:
        instructions.append("GOVERNANCE DOCUMENT (ERP_AGENT_CONSTITUTION):")
        instructions.append(constitution[:8000])  # Token budget
        instructions.append("")

    return "\n".join(instructions)


def build_user_content(message: str, context: AgentContext) -> str:
    """Build the user message with relevant runtime context.

    Only the relevant ERP context resolved by context_manager.build_context()
    is included — never the entire database.
    """
    parts: List[str] = []

    # Organisation context
    org = context.organization
    if org:
        parts.append(f"Organisation: {org.get('name', 'Unknown')} (currency: {org.get('base_currency_code', 'PKR')})")

    # Financial period
    if context.accounting_period:
        p = context.accounting_period
        parts.append(f"Current period: {p.get('name', 'N/A')} ({p.get('start_date', '')} to {p.get('end_date', '')})")

    # Work Stream F: learned organization defaults - authoritative unless
    # the user explicitly overrides them in the current request.
    if context.org_preferences:
        parts.append("")
        parts.append("ORG PREFERENCES (authoritative defaults the user set previously - apply them unless the user explicitly overrides):")
        for key, value in context.org_preferences.items():
            label = key.replace("_", " ")
            parts.append(f"  - {label}: {value}")

    # Relevant entities (compact)
    if context.relevant_customers:
        names = [c.get("name", "?") for c in context.relevant_customers[:5]]
        parts.append(f"Relevant customers: {', '.join(names)}")
    if context.relevant_suppliers:
        names = [s.get("name", "?") for s in context.relevant_suppliers[:5]]
        parts.append(f"Relevant suppliers: {', '.join(names)}")
    if context.relevant_accounts:
        names = [f"{a.get('code', '')} {a.get('name', '')}" for a in context.relevant_accounts[:10]]
        parts.append(f"Relevant accounts: {'; '.join(names)}")

    # Deterministic transaction classification — authoritative accounting
    # context from the classifier (ERP configuration → rules → user answer).
    classification = getattr(context, "classification", None)
    if classification is not None:
        parts.append("")
        parts.append("TRANSACTION CLASSIFICATION (deterministic — authoritative; computed from ERP configuration, business rules and user answers):")
        if classification.transaction_nature:
            parts.append(f"  - Economic nature: {classification.transaction_nature} (confidence: {classification.confidence}, source: {classification.source})")
        else:
            parts.append("  - Economic nature: UNDETERMINED")
        if classification.account_hint_id:
            parts.append(
                f"  - Account hint: {classification.account_hint_code} "
                f"{classification.account_hint_name} (id: {classification.account_hint_id})"
            )
            parts.append("    → Pass this account id as the 'account_id' argument when recording this transaction. The accounting engine will validate it.")
        elif getattr(classification, "create_account_confirmed", False):
            proposed = getattr(classification, "proposed_account_name", None)
            code = getattr(classification, "proposed_account_code", None)
            parts.append(
                f"    → The user CONFIRMED creating the new account '{proposed or 'the proposed account'}'"
                + (
                    f" (base code {code} — a free code in that series is "
                    "resolved automatically)"
                    if code else ""
                )
                + ". Run create_account for it FIRST, then record the "
                "transaction against the newly created account. The "
                "account-creation guard blocks default-account recording "
                "until the account exists."
            )
        elif classification.transaction_nature:
            parts.append(
                "    → No dedicated account exists for this nature. Use the most "
                "appropriate EXISTING account from the chart of accounts "
                "(search_account) and do NOT create new accounts — new accounts "
                "require explicit owner approval."
            )
        if classification.requires_clarification:
            parts.append(
                "    → The treatment is MATERIALLY AMBIGUOUS. Ask the user the "
                "classification question instead of recording anything."
            )
        parts.append(
            "    → NEVER fall back to a generic expense account when a fixed-asset "
            "or inventory treatment applies. Do NOT invent new accounts — reuse "
            "the hint or search the chart of accounts."
        )

    # ECONOMIC EVENT CLASSIFICATION — what real-world event this request
    # represents, what it can affect, and what is explicitly FORBIDDEN.
    # The model plans tools from economic meaning, never CRUD keywords.
    economic_event = getattr(context, "economic_event", None)
    if economic_event:
        parts.append("")
        parts.append("ECONOMIC EVENT CLASSIFICATION (deterministic — authoritative):")
        parts.append(f"  - Underlying event: {economic_event}")
        impact_map = getattr(context, "impact_map", {}) or {}
        active = [k for k, v in impact_map.items() if v]
        inactive = [k for k, v in impact_map.items() if not v]
        if active:
            parts.append(f"  - AFFECTED dimensions: {', '.join(active)}")
        if inactive:
            parts.append(
                "  - NOT affected dimensions: "
                + ", ".join(inactive)
                + " — do not create records for these; they are irrelevant "
                "to this event."
            )
        prohibited = getattr(context, "prohibited_actions", []) or []
        if prohibited:
            parts.append("  - PROHIBITED ACTIONS (STRICT — the executor will refuse them):")
            for p in prohibited:
                parts.append(f"    · {p.get('action', '')}: {p.get('reason', '')}")
        parts.append(
            "  → Plan tool calls for the ECONOMIC EVENT above. Before creating any "
            "record, ask WHY it needs to exist: is it required by the business "
            "event, by accounting, by a workflow, or by a database dependency? "
            "If it is only 'usually created', do NOT create it."
        )

    # Work Stream S3 — LIVE BOOKS EVIDENCE retrieved by the LLM reasoning
    # layer.  Labelled as evidence (not as truth): the model must reassess the
    # request against it and may reclassify.
    if getattr(context, "live_evidence", None):
        parts.append("")
        parts.append(
            "LIVE BOOKS EVIDENCE — use this to reassess the request "
            "(retrieved from this organization's own records; empty means the "
            "record does not exist, not that a lookup failed):"
        )
        for item in context.live_evidence[:12]:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind", "evidence")
            if item.get("error"):
                parts.append(f"  - {kind}: REFUSED/FAILED — {item.get('error')}")
                continue
            if not item.get("records"):
                parts.append(f"  - {kind}: EMPTY — no matching record exists")
                continue
            rendered = str(item.get("records"))[:1200]
            parts.append(f"  - {kind} ({len(item.get('records') or [])} row(s)): {rendered}")

    # Work Stream S3 — the model's own prior accounting reasoning for this
    # request (interpretation + proposed treatment), so the planning stage
    # reassesses instead of silently replacing it with a keyword route.
    reasoning = getattr(context, "accounting_reasoning", None)
    if reasoning:
        parts.append("")
        parts.append(
            "LLM ACCOUNTING REASONING (your own prior reading of the live "
            "records for this request — re-verify it against the books above; "
            "correct it if the records say otherwise):"
        )
        understanding = reasoning.get("understanding") or {}
        for key in ("economic_event", "what_user_wants", "event_type", "basis"):
            if understanding.get(key):
                parts.append(f"  - {key}: {understanding.get(key)}")
        proposal = reasoning.get("proposal") or {}
        if proposal:
            if proposal.get("interpretation"):
                parts.append(f"  - proposed interpretation: {proposal['interpretation']}")
            if proposal.get("accounting_impact"):
                parts.append(
                    f"  - proposed impact: {str(proposal['accounting_impact'])[:800]}"
                )
            if proposal.get("not_affected"):
                parts.append(
                    "  - explicitly NOT affected: "
                    + "; ".join(str(x) for x in proposal["not_affected"][:6])
                )
            if proposal.get("unresolved_uncertainty"):
                parts.append(
                    "  - unresolved uncertainty: "
                    + "; ".join(str(x) for x in proposal["unresolved_uncertainty"][:6])
                )
        if reasoning.get("evidence_summary"):
            parts.append(f"  - evidence inspected: {reasoning['evidence_summary']}")

    # Work Stream S3 — the deterministic extraction, labelled as PRELIMINARY so
    # it can never be mistaken for a decided accounting treatment.
    preliminary = getattr(context, "preliminary_extraction", None)
    if preliminary:
        parts.append("")
        parts.append(
            "PRELIMINARY EXTRACTION — may be corrected after accounting review:"
        )
        for key in ("literals", "candidate_subject_areas", "provisional_intent_hint"):
            if preliminary.get(key):
                parts.append(f"  - {key}: {preliminary[key]}")

    # Extracted entities — authoritative values parsed from the user's
    # message. The model must reuse these instead of re-parsing (and must
    # never ask the user for anything already here).
    if context.extracted_entities:
        parts.append("")
        parts.append("EXTRACTED ENTITIES (authoritative — parsed from the user's message; use these values directly, never re-ask them):")
        for key, value in context.extracted_entities.items():
            if value is not None and value != "":
                parts.append(f"  - {key}: {value}")

    # Clarification history — questions the user already answered.
    if context.clarification_history:
        parts.append("")
        parts.append("CLARIFICATION HISTORY (already answered — merge these answers with the request; never re-ask):")
        for qa in context.clarification_history:
            parts.append(f"  - Q: {qa.get('question', '')} -> A: {qa.get('answer', '')}")

    parts.append("")
    parts.append(f"User request: {message}")

    return "\n".join(parts)
