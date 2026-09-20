"""
AI-Native ERP — Semantic Context Package (Work Stream S2)
========================================================

Builds the *relevant* context the semantic understanding layer is allowed to
see. Nothing here is optional decoration: every block exists because the LLM
cannot interpret the request correctly without it.

    USER REQUEST
    + CURRENT DATE CONTEXT
    + ORGANIZATION CONTEXT        (identity, currency, fiscal year, period)
    + RELEVANT ERP ENTITIES       (candidates matched against the user's words)
    + AVAILABLE ERP CAPABILITIES  (what this ERP can actually do)
    + CURRENT SESSION STATE       (original request, established facts)
    + PREVIOUS USER ANSWERS       (clarification continuity)
    + DOMAIN RULEBOOK             (how the deterministic agent reasons)

Bounding rules (non-negotiable)
-------------------------------
* ORGANIZATION-SCOPED: every query is issued with the authenticated
  ``organization_id``. The semantic layer can never see another tenant's data.
* RELEVANT, NOT EXHAUSTIVE: candidates are found by searching with terms
  taken from the USER'S OWN WORDS — never a table dump. Hard caps per kind.
* TRACEABLE: every candidate carries its database id, so grounding can name
  the exact record the LLM was shown. The LLM is told to reference those
  candidates; it may never invent an id.
* BEST-EFFORT: a failing source degrades to empty rather than failing the
  request (same isolation discipline as ``context_manager``).

The rulebook is the agent's reasoning brain expressed as instructions: the
grounding contract, the number/date resolution rules, the nature taxonomy,
money direction, the four information states, and the clarification law.
That is how the LLM is made to think the way the deterministic agent thinks
instead of guessing at ERP vocabulary.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

# Caps — the context package must stay small and predictable.
MAX_TERMS = 8
MAX_CANDIDATES_PER_KIND = 5
MAX_PRIOR_ANSWERS = 8
MAX_PRIOR_FACTS = 20
RULEBOOK_CONSTITUTION_CHARS = 6000

# Words that are never an entity term on their own.
_STOPWORDS = frozenset(
    """
    a an and the to from for of in on at by with about into over after before
    is are was were be been being do does did done have has had
    i we you they he she it this that these those me us them my our your their
    sold sell sells selling buy bought purchase purchased supply supplied
    paid pay pays paying receive received record recorded make made create
    today yesterday tomorrow cash credit bank cheque card
    rs pkr usd amount total value quantity units each per
    please kindly also then next
    """.split()
)

_KIND_LABELS = {
    "customers": "customers",
    "suppliers": "suppliers",
    "products": "products (stock items)",
    "services": "services",
    "accounts": "chart-of-accounts entries",
    "bank_accounts": "bank accounts",
}


@dataclass
class SemanticContext:
    """The bounded context package handed to the semantic LLM."""

    organization: Dict[str, Any] = field(default_factory=dict)
    candidates: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    prior_answers: List[Dict[str, str]] = field(default_factory=list)
    prior_facts: Dict[str, Any] = field(default_factory=dict)
    session_state: Dict[str, Any] = field(default_factory=dict)
    rulebook: str = ""
    today: Optional[str] = None

    def candidate_names(self, kind: str) -> List[str]:
        return [str(c.get("name") or "") for c in (self.candidates.get(kind) or [])]

    def all_candidate_names(self) -> List[str]:
        out: List[str] = []
        for rows in self.candidates.values():
            for row in rows:
                name = str(row.get("name") or "").strip()
                if name and name not in out:
                    out.append(name)
        return out

    def as_prompt_block(self) -> str:
        """Render the package as the CONTEXT section of the LLM prompt."""
        parts: List[str] = []

        if self.today:
            parts.append(f"Current date: {self.today}")

        org = self.organization or {}
        if org:
            bits = [
                f"{k}={v}"
                for k, v in (
                    ("name", org.get("name")),
                    ("currency", org.get("base_currency") or org.get("currency")),
                    ("business_type", org.get("business_type")),
                    ("country", org.get("country")),
                )
                if v
            ]
            if bits:
                parts.append("Organization: " + "; ".join(str(b) for b in bits))

        fy = self.session_state.get("financial_year")
        period = self.session_state.get("open_period")
        if fy or period:
            span = []
            if fy:
                span.append(f"financial year {fy}")
            if period:
                span.append(f"open accounting period {period}")
            parts.append(
                "Accounting calendar: " + ", ".join(span)
                + " - a transaction date outside this window is INVALID."
            )

        if self.session_state.get("original_request"):
            parts.append(
                "Original request (already understood - do NOT re-interpret from "
                f"scratch, extend it): {self.session_state['original_request']}"
            )

        if self.prior_facts:
            facts = ", ".join(
                f"{k}={v}"
                for k, v in list(self.prior_facts.items())[:MAX_PRIOR_FACTS]
                if v not in (None, "")
            )
            if facts:
                parts.append(
                    "ALREADY ESTABLISHED FACTS (authoritative - never contradict, "
                    f"never report these as missing): {facts}"
                )

        if self.prior_answers:
            parts.append("PREVIOUS USER ANSWERS (authoritative - already resolved):")
            for qa in self.prior_answers[:MAX_PRIOR_ANSWERS]:
                parts.append(
                    f"  - Q: {qa.get('question', '')} -> A: {qa.get('answer', '')}"
                )

        if self.candidates:
            parts.append("")
            parts.append(
                "RELEVANT EXISTING RECORDS in this organization (the ONLY real "
                "records that exist; you may reference a name from here, and you "
                "must NEVER state an id of your own):"
            )
            for kind, rows in self.candidates.items():
                if not rows:
                    continue
                label = _KIND_LABELS.get(kind, kind)
                rendered = "; ".join(
                    f"{r.get('name')}"
                    + (f" (code {r['code']})" if r.get("code") else "")
                    for r in rows
                    if r.get("name")
                )
                if rendered:
                    parts.append(f"  - {label}: {rendered}")

        if self.capabilities:
            parts.append("")
            parts.append(
                "AVAILABLE ERP CAPABILITIES (this ERP can ONLY do these; if the "
                "request needs something outside this list, say so in `unsupported` "
                "instead of inventing a capability):"
            )
            parts.append("  - " + ", ".join(self.capabilities))

        if self.rulebook:
            parts.append("")
            parts.append(self.rulebook)

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# The domain rulebook — how the deterministic agent thinks
# ---------------------------------------------------------------------------
#
# This is NOT a field list. It is the reasoning contract: the same laws the
# deterministic engine enforces, written so the LLM reasons WITH them instead
# of searching for keywords. Sending it is what makes the LLM's judgement the
# agent's judgement — the "brain", not just the parser.

_RULEBOOK_CORE = """
DOMAIN RULEBOOK - how this ERP's accounting agent reasons. Think with these
rules; they are the same laws the deterministic engine enforces afterwards.

A. UNDERSTAND MEANING, NOT WORDS
   Business meaning decides everything. "we supplied HJK with 2 chairs worth
   23k, settlement deferred" and "HJK bought two chairs from us for 23,000
   and will pay later" are THE SAME business event. Never require the user to
   use a word this system knows; infer the act from its economic meaning.

B. GROUND EVERY COPIED VALUE (verbatim only)
   A party name or item must be copied EXACTLY as written in the request
   (same words, same spelling). If you cannot copy it verbatim, do not output
   it. NEVER invent, translate, shorten or "tidy" a name.

C. NUMBERS
   Written numbers become digits: two -> 2, a pair of -> 2, a dozen -> 12,
   23k -> 23000, 2.5 lac -> 250000, twenty-three thousand -> 23000.
   amount = the money value (no currency symbol). quantity = units.

D. DATES
   Resolve relative expressions with the provided current date:
   yesterday -> current date minus 1 day; "two days ago" -> minus 2;
   "last Friday" -> the most recent Friday before today; "end of last month"
   -> the last day of the previous month. Explicit dates -> YYYY-MM-DD
   (treat DD/MM/YYYY as day-first). If the request states no date and none can
   be derived, the date is MISSING - never default it silently.

E. MONEY DIRECTION DECIDES THE PARTY ROLE
   Money coming IN (sale, invoice, credit note, receipt) means the party is a
   CUSTOMER. Money going OUT (purchase, bill, expense, payment, return) means
   the party is a SUPPLIER. The role is derived from the direction of the act,
   not from a keyword.

F. TRANSACTION NATURE (classify only when the item makes it unambiguous)
   GOODS           stock items the business deals in
   SERVICE         work or expertise delivered
   ASSET_DISPOSAL  selling a fixed asset the business owns
   OTHER_INCOME    income outside the trading activity
   If several treatments are materially possible, the nature is AMBIGUOUS -
   never choose silently.

G. FOUR INFORMATION STATES - classify EVERY material fact
   EXPLICIT         the user stated it. Preserve exactly.
   SAFELY_INFERRED  derived by a rule above (e.g. "yesterday" -> a date).
                    Always include the evidence you inferred it from.
   AMBIGUOUS        the request contains information, but several materially
                    different interpretations remain (e.g. the user says "ABC"
                    and the organization has ABC Pvt Ltd and ABC Traders).
                    Never pick one silently.
   MISSING          not stated and not safely derivable, but REQUIRED for this
                    operation. Never guess it.

H. AMBIGUOUS IS NOT MISSING
   AMBIGUOUS = something was said, but it does not resolve to one record.
   MISSING   = nothing was said. They need different questions: ambiguity
   offers the competing options; missing asks openly for the value.

I. NEVER ASK WHAT IS ALREADY KNOWN
   If a fact is EXPLICIT or SAFELY_INFERRED or already in ALREADY ESTABLISHED
   FACTS / PREVIOUS USER ANSWERS, it must NOT appear in missing or in any
   question. Asking again for information the user already gave is the defect
   this architecture exists to prevent.

J. QUESTIONS ARE GENERATED, NOT TEMPLATED
   Decide what is ACTUALLY required before this particular operation can be
   recorded safely, subtract everything known, and ask ONLY the remainder.
   A fully-stated request asks NOTHING. Ask at most 3 questions, each one
   specific to this request's real gap, with the concrete values involved.

K. GROUND ENTITIES, NEVER INVENT IDS
   You may name a party/item from the user's words or from the RELEVANT
   EXISTING RECORDS section. You must NEVER output a database id - Python
   resolves real records. If a named party is not in the records and the text
   is ambiguous, mark it AMBIGUOUS with the candidate names you were shown.

L. DO NOT INVENT
   AI-first never means manufacturing values. Anything not stated and not
   derivable by a rule above is MISSING/AMBIGUOUS - never a plausible guess.

M. YOU INTERPRET ONLY
   You never decide what is authorised, never compute debits/credits, never
   balance a journal and never execute anything. The backend validates,
   the accounting engine composes the entries and the tools execute.
""".strip()

#: Constitution sections whose content the semantic layer must reason with.
#: Matched case-insensitively against the "## " heading text.
_RULEBOOK_SECTIONS = (
    "deterministic vs ai responsibilities",
    "ai reasoning and context acquisition",
    "financial truth boundary",
    "validation rules",
    "multi-tenancy",
)


def _constitution_excerpt(constitution: str) -> str:
    """The reasoning-governance sections of the constitution, size-bounded."""
    text = str(constitution or "").strip()
    if not text:
        return ""
    # Split on level-2 headings; keep the heading with its body.
    blocks = re.split(r"(?m)^(?=##\s)", text)
    kept: List[str] = []
    used = 0
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        head = lines[0].strip().lower().lstrip("# ").strip()
        if not head or not any(key in head for key in _RULEBOOK_SECTIONS):
            continue
        trimmed = block.strip()
        if used + len(trimmed) > RULEBOOK_CONSTITUTION_CHARS:
            trimmed = trimmed[: max(0, RULEBOOK_CONSTITUTION_CHARS - used)]
        if trimmed:
            kept.append(trimmed)
            used += len(trimmed)
        if used >= RULEBOOK_CONSTITUTION_CHARS:
            break
    if not kept:
        return ""
    return (
        "GOVERNANCE EXTRACT (from the ERP Agent Constitution - the binding "
        "responsibility split this system operates under):\n\n"
        + "\n\n".join(kept)
    )


def distill_rulebook(constitution: str = "") -> str:
    """Compose the rulebook handed to the semantic LLM.

    ``_RULEBOOK_CORE`` carries the reasoning laws (the distilled brain of the
    deterministic agent); the constitution extract carries the governance
    split (what the AI decides vs what the backend decides). Together they
    tell the model HOW to think about this ERP, not merely which fields to
    fill.
    """
    parts = [_RULEBOOK_CORE]
    excerpt = _constitution_excerpt(constitution)
    if excerpt:
        parts.append(excerpt)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Candidate discovery from the user's own words
# ---------------------------------------------------------------------------


def extract_candidate_terms(message: str) -> List[str]:
    """Search terms taken from the USER'S OWN message — never a table dump.

    Prefers multi-word proper-noun runs ("hjk pvt limited", "ABC Traders"),
    then quoted strings, then meaningful single tokens. Bounded to
    ``MAX_TERMS``.
    """
    text = str(message or "")
    terms: List[str] = []

    def _add(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" .,;:'\"-()[]")
        if len(cleaned) < 2 or len(cleaned) > 60:
            return
        low = cleaned.lower()
        if low in _STOPWORDS or low in terms or cleaned in terms:
            return
        if low.isdigit():
            return
        terms.append(cleaned)

    # 1. Quoted spans are an explicit hint from the user.
    for match in re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,60})[\"'“”‘’]", text):
        _add(match)

    # 2. Proper-noun runs of 2-4 tokens (e.g. "hjk pvt limited").
    for match in re.findall(
        r"\b([A-Za-z][\w&.'-]*(?:\s+(?:[A-Za-z][\w&.'-]*|pvt|ltd|limited|inc|llc)){1,3})",
        text,
    ):
        run = match.strip()
        words = run.split()
        if all(w.lower() in _STOPWORDS for w in words):
            continue
        _add(run)

    # 3. Individual meaningful tokens as a fallback.
    for token in re.findall(r"\b[A-Za-z][\w&.'-]{2,}\b", text):
        if token.lower() in _STOPWORDS:
            continue
        _add(token)
        if len(terms) >= MAX_TERMS * 3:
            break

    return terms[:MAX_TERMS]


# ---------------------------------------------------------------------------
# Org-scoped candidate lookup
# ---------------------------------------------------------------------------


def _row_summary(row: Any) -> Optional[Dict[str, Any]]:
    """Reduce a repository row to the fields the LLM may legitimately see."""
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or row.get("account_name") or "").strip()
    if not name:
        return None
    out: Dict[str, Any] = {"name": name, "id": str(row.get("id") or "")}
    code = row.get("code") or row.get("account_code")
    if code:
        out["code"] = str(code)
    return out


async def _search_kind(
    kind: str, organization_id: uuid.UUID, terms: List[str]
) -> List[Dict[str, Any]]:
    """Search ONE kind with the user's own terms; org-scoped and capped."""
    from app.repositories import (
        account_repository,
        customer_repository,
        product_repository,
        service_repository,
        supplier_repository,
    )

    searchers = {
        "customers": customer_repository.search_customers,
        "suppliers": supplier_repository.search_suppliers,
        "products": product_repository.search_products,
        "services": service_repository.search_services,
        "accounts": account_repository.search_accounts,
    }
    search = searchers.get(kind)
    if not callable(search):
        return []

    found: List[Dict[str, Any]] = []
    seen: set = set()
    for term in terms:
        if len(found) >= MAX_CANDIDATES_PER_KIND:
            break
        try:
            rows = await search(organization_id, query=term, limit=3) or []
        except Exception as exc:  # noqa: BLE001 - context is best-effort
            log.info(
                "semantic_context.search_failed", kind=kind, error=str(exc)[:120]
            )
            return found
        for row in rows:
            summary = _row_summary(row)
            if not summary:
                continue
            key = summary["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(summary)
            if len(found) >= MAX_CANDIDATES_PER_KIND:
                break
    return found


async def _load_capabilities() -> List[str]:
    """The controlled tool slugs this ERP can execute (bounded, names only)."""
    try:
        from app.tools import list_tools

        tools = list_tools() or []
    except Exception as exc:  # noqa: BLE001 - context is best-effort
        log.info("semantic_context.capabilities_failed", error=str(exc)[:120])
        return []
    slugs: List[str] = []
    for tool in tools:
        name = ""
        if isinstance(tool, dict):
            name = str(tool.get("name") or tool.get("slug") or "")
        else:
            name = str(getattr(tool, "name", "") or getattr(tool, "slug", ""))
        if name and name not in slugs:
            slugs.append(name)
    return slugs


async def build_semantic_context(
    *,
    organization_id: uuid.UUID,
    user_id: Optional[uuid.UUID] = None,
    user_message: str = "",
    prior_answers: Optional[List[Dict[str, str]]] = None,
    prior_facts: Optional[Dict[str, Any]] = None,
    original_request: Optional[str] = None,
    constitution: str = "",
    today: Optional[str] = None,
) -> SemanticContext:
    """Assemble the bounded, org-scoped context package for the semantic LLM.

    Every source is isolated: a failing lookup degrades to an empty block
    rather than failing the request. Nothing outside ``organization_id`` can
    ever enter the package, because every query is scoped by it.
    """
    from app.repositories import organization_repository

    package = SemanticContext(
        prior_answers=list(prior_answers or [])[:MAX_PRIOR_ANSWERS],
        prior_facts=dict(prior_facts or {}),
        rulebook=distill_rulebook(constitution),
        today=today,
    )

    # ---- organization identity + accounting calendar (org-scoped) --------
    try:
        org = await organization_repository.get_organization(
            organization_id=organization_id
        )
        package.organization = dict(org) if isinstance(org, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.info("semantic_context.organization_failed", error=str(exc)[:120])

    state: Dict[str, Any] = {}
    if original_request:
        state["original_request"] = original_request
    for label, loader in (
        ("financial_year", organization_repository.get_current_financial_year),
        ("open_period", organization_repository.get_open_accounting_period),
    ):
        try:
            row = await loader(organization_id)
            if isinstance(row, dict) and row:
                state[label] = (
                    row.get("name")
                    or row.get("label")
                    or row.get("period_name")
                    or row.get("start_date")
                    or row.get("code")
                )
        except Exception as exc:  # noqa: BLE001
            log.info(
                "semantic_context.calendar_failed", source=label, error=str(exc)[:120]
            )
    package.session_state = state

    # ---- relevant candidates from the user's own words -------------------
    terms = extract_candidate_terms(user_message)
    if terms:
        import asyncio

        kinds = ("customers", "suppliers", "products", "services")
        results = await asyncio.gather(
            *(_search_kind(kind, organization_id, terms) for kind in kinds),
            return_exceptions=True,
        )
        for kind, rows in zip(kinds, results):
            if isinstance(rows, Exception) or not rows:
                continue
            package.candidates[kind] = rows

        # Bank accounts are only relevant when the request is about money
        # movement, so they are added only then.
        if any(
            word in (user_message or "").lower()
            for word in ("bank", "transfer", "deposit", "withdraw", "online")
        ):
            try:
                banks = await organization_repository.get_bank_accounts(
                    organization_id
                )
                summarized = [
                    s for s in (_row_summary(r) for r in (banks or [])) if s
                ][:MAX_CANDIDATES_PER_KIND]
                if summarized:
                    package.candidates["bank_accounts"] = summarized
            except Exception as exc:  # noqa: BLE001
                log.info("semantic_context.banks_failed", error=str(exc)[:120])

    # ---- available capabilities -----------------------------------------
    package.capabilities = await _load_capabilities()

    log.info(
        "semantic_context.built",
        terms=len(terms),
        kinds=sorted(package.candidates.keys()),
        candidates=sum(len(v) for v in package.candidates.values()),
        capabilities=len(package.capabilities),
        rulebook_chars=len(package.rulebook),
    )
    return package