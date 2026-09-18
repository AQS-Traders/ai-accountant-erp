"""
Grounded LLM entity segregation — the accuracy gap-filler for the planner.

WHY THIS EXISTS
---------------
The deterministic extractor in ``app/planner.py`` matches hard-coded regex
templates. Any phrasing outside those templates yields no party/item, so the
agent re-asks for facts the user already gave ("make an invoice out for
Bilal Electronics covering 4 voltage stabilizers, they'll pay later" —
regex finds neither the customer nor the item).

WHAT THIS DOES
--------------
ONE extra LLM call — made ONLY when the regex stage found gaps — segregates
the user's own sentence into the entities the planner needs.

CONTRACT (do not weaken these):

1. GROUNDED: every value returned by the model must appear VERBATIM
   (case/punctuation/whitespace-insensitively) in the user's own text.
   A value that is not in the text is a hallucination and is DROPPED —
   never trusted, never repaired, never "closest-match"ed.
2. ADDITIVE: the regex extractor stays authoritative. A grounded value only
   fills a gap (``setdefault`` semantics in ``plan()``); it can never
   overwrite a value the regex stage or the user already provided.
3. DEGRADE SAFELY: provider down, timeout, malformed JSON, empty response —
   every failure path returns ``{}`` and the behaviour is exactly the old
   regex-only behaviour. This stage can never make a request fail.
4. BOUNDED: one call, hard wall-clock timeout, no tool-calling, no retries
   of its own (the orchestrator already owns provider fallback).

Scope: party names, item description, quantity, amount. Dates and payment
method stay regex-only — their canonical forms are not verbatim substrings
and grounding them would be unreliable.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict

import structlog

log = structlog.get_logger(__name__)

# Generic nouns that must never be taken as an item description
# (mirrors the filter in planner._extract_item).
_GENERIC_ITEMS = frozenset(
    {"it", "something", "goods", "items", "stuff", "things", "product", "products"}
)

# Fields this stage may ever return. Anything else the model emits is
# discarded before grounding even runs.
_ALLOWED_FIELDS = frozenset(
    {
        "supplier_name",
        "customer_name",
        "item_description",
        "item_quantity",
        "amount",
    }
)

_NAME_FIELDS = frozenset({"supplier_name", "customer_name", "item_description"})

_SYSTEM_PROMPT = (
    "You extract accounting entities from a user's request. "
    "Copy values EXACTLY as written in the request - same words, same order, "
    "same spelling. NEVER invent, translate, rephrase or complete a value. "
    "If a value is not stated, OMIT its key. "
    'Answer with ONLY a JSON object, keys limited to: "supplier_name", '
    '"customer_name", "item_description", "item_quantity", "amount". '
    "item_quantity and amount are plain numbers. No markdown, no commentary."
)

def _norm(text) -> str:
    """Lowercase, punctuation-free, whitespace-collapsed form for grounding."""
    lowered = str(text or "").lower()
    lowered = re.sub(r"[^\w\s]", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def _is_grounded(candidate, normalized_message: str) -> bool:
    """True only when the candidate text occurs verbatim in the message."""
    value = _norm(candidate)
    if len(value) < 2:
        return False
    return value in normalized_message


def _clean_candidate(value) -> str:
    """Strip quoting/wrapping the model may add around a copied value."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    # A leading preposition is a copy artefact, not part of the name.
    text = re.sub(r"^(?:to|from|for|of|the)\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" .;,:-")


def _parse_json_response(raw) -> dict:
    """Extract the first JSON object from the response, or {} on any failure."""
    if not raw:
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

def _ground_fields(parsed: dict, normalized_message: str) -> dict:
    """Keep only allowed fields whose values pass the grounding checks."""
    grounded: dict = {}
    for key, raw_value in parsed.items():
        if key not in _ALLOWED_FIELDS:
            continue
        if key in _NAME_FIELDS:
            candidate = _clean_candidate(raw_value)
            if not candidate or len(candidate) > 120:
                log.info("llm_entity_rejected", field=key, reason="empty_or_oversized")
                continue
            if key == "item_description" and candidate.lower() in _GENERIC_ITEMS:
                log.info("llm_entity_rejected", field=key, reason="generic_item")
                continue
            if not _is_grounded(candidate, normalized_message):
                # EVERY name field must be the user's own words; anything
                # else is a hallucinated vendor/customer/item and would
                # misstate the books.
                log.info("llm_entity_rejected", field=key, reason="not_in_text")
                continue
            grounded[key] = candidate
        else:
            try:
                number = float(str(raw_value).replace(",", "").strip())
            except (TypeError, ValueError):
                log.info("llm_entity_rejected", field=key, reason="not_a_number")
                continue
            if number <= 0:
                log.info("llm_entity_rejected", field=key, reason="non_positive")
                continue
            grounded[key] = number
    return grounded


async def segregate_entities(user_message: str, orchestrator=None) -> dict:
    """Return grounded entities the regex extractor missed.

    ``orchestrator`` is the AIOrchestrator (or any object exposing
    ``generate_text(prompt=...)``). Pass ``None``/an object without
    ``generate_text`` to get ``{}`` back immediately.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.entity_llm_fallback:
        return {}

    msg = (user_message or "").strip()
    if not msg:
        return {}

    generate = getattr(orchestrator, "generate_text", None) if orchestrator else None
    if not callable(generate):
        return {}

    prompt = (
        "User request:\n"
        f"{msg}\n\n"
        "Extract the entities listed in the instructions. Copy every value "
        "verbatim from the request."
    )

    try:
        raw = await asyncio.wait_for(
            generate(prompt=prompt), timeout=settings.entity_llm_timeout_seconds
        )
    except asyncio.TimeoutError:
        log.warning(
            "llm_entity_failed",
            reason="timeout",
            timeout_s=settings.entity_llm_timeout_seconds,
        )
        return {}
    except Exception as exc:  # noqa: BLE001 — the stage must never raise
        log.warning("llm_entity_failed", reason="provider_error", detail=str(exc)[:200])
        return {}

    parsed = _parse_json_response(raw if isinstance(raw, str) else "")
    if not parsed:
        log.info("llm_entity_failed", reason="unparseable_response")
        return {}

    grounded = _ground_fields(parsed, _norm(msg))
    if grounded:
        log.info("llm_entities_grounded", fields=sorted(grounded.keys()))
    else:
        log.info("llm_entities_none_grounded")
    return grounded
