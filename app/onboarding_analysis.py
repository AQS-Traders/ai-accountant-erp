"""
Organization onboarding assistant
==================================
Turns a business owner's plain-language description of their business into
the structured organization configuration the ERP actually accepts.

WHY IT EXISTS
-------------
First-time onboarding asked a non-accountant to populate an accounting
configuration by hand: which business type, which accounts, which
structure.  The assistant reasons over the owner's own description *on top
of the real backend contract* and proposes values — but it never finalises
anything.  The user reviews every field, edits what is wrong, and confirms;
the organization is only created after that.

GROUNDING RULES (no fabrication)
--------------------------------
* The contract — RPC arguments and their defaults, the NOT NULL/defaulted
  columns, the creation function's own validation rules, every business
  type with the chart it resolves to, the currencies, and the optional
  account bundles per business type — is read from the database at runtime
  via ``public.organization_onboarding_contract()``.  Nothing about the
  backend is hard-coded here.
* Every value the model proposes is re-validated against that contract and
  dropped when it does not verify, so the assistant can only ever populate
  backend-compatible fields (never a parallel set of AI-only fields).
* A fact the owner did not state, and that cannot be derived from what they
  did state, is reported as unresolved — with a question when the backend
  requires it or when it changes the accounts — instead of being guessed.
  "We don't maintain inventory" and "the owner never mentioned inventory"
  are therefore treated differently: only the first removes inventory.
* Account bundles are proposed solely from the catalog published for the
  chosen business type, so no account can be invented.

The module is intentionally free of the heavy agent stack: the AI provider
chain is imported lazily inside :func:`analyze_organization`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from app.database import call_rpc

log = structlog.get_logger(__name__)

# The contract is schema/reference data that changes only by migration.
_CONTRACT_TTL_SECONDS = 300.0
_contract_cache: Dict[str, Any] = {"value": None, "fetched_at": 0.0}

# Field formats accepted by the backend (mirrored from the database).
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_PREFIX_RE = re.compile(r"^[A-Z0-9]{1,8}$")
_TIMEZONE_RE = re.compile(r"^UTC$|^[A-Za-z]+(?:/[A-Za-z_+\-0-9]+)+$")

# Sanity caps for text columns.
_MAX_SHORT_TEXT = 120
_MAX_LONG_TEXT = 2000

# Which organization fields the assistant may populate, mapped to the
# contract argument they correspond to.  The keys are the column names in
# `organizations` / `organization_settings` — exactly what the frontend
# hands to the two RPCs.
FIELD_TO_ARGUMENT = {
    "name": "p_name",
    "business_type": "p_business_type",
    "legal_name": "p_legal_name",
    "tax_number": "p_tax_number",
    "registration_number": "p_registration_number",
    "core_services": "p_core_services",
    "industry_details": "p_industry_details",
    "base_currency_code": "p_base_currency_code",
    "country_code": "p_country_code",
    "timezone": "p_timezone",
    "fiscal_year_end_month": "p_fiscal_year_end_month",
    "fiscal_year_start_year": "p_fiscal_year_start_year",
}

# Document prefixes live on organization_settings and have backend
# defaults, so they are offered only when the owner expresses a numbering
# preference.
PREFIX_FIELDS = ("invoice_prefix", "quotation_prefix", "bill_prefix", "journal_prefix")

# Country → (timezone, currency).  Used ONLY to translate a country the
# owner actually stated; never to infer a country.
COUNTRY_LOCALE_HINTS: Dict[str, Tuple[str, str]] = {
    "PK": ("Asia/Karachi", "PKR"),
    "AE": ("Asia/Dubai", "AED"),
    "SA": ("Asia/Riyadh", "SAR"),
    "GB": ("Europe/London", "GBP"),
    "US": ("America/New_York", "USD"),
    "IN": ("Asia/Kolkata", "INR"),
}

_MAX_QUESTIONS = 3

# Zones the product itself offers.  Used as the accepted set only when the
# runtime has no IANA time-zone database to verify against (a bare Windows
# Python without the `tzdata` wheel); on Linux/Vercel `zoneinfo` is
# authoritative instead.
_FALLBACK_TIMEZONES = {timezone for timezone, _ in COUNTRY_LOCALE_HINTS.values()} | {"UTC"}

_TZ_DB_AVAILABLE: Optional[bool] = None


def _tz_database_available() -> bool:
    """Whether the runtime can verify IANA zone names via ``zoneinfo``."""
    global _TZ_DB_AVAILABLE
    if _TZ_DB_AVAILABLE is None:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo("UTC")
            _TZ_DB_AVAILABLE = True
        except Exception:  # noqa: BLE001 — no tz database installed
            _TZ_DB_AVAILABLE = False
    return bool(_TZ_DB_AVAILABLE)


def _valid_timezone(value: str) -> bool:
    """A timezone is only accepted when it can actually be verified."""
    if not value or len(value) > 64 or not _TIMEZONE_RE.match(value):
        return False
    if _tz_database_available():
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(value)
            return True
        except Exception:  # noqa: BLE001 — unknown zone
            return False
    return value in _FALLBACK_TIMEZONES

# ---------------------------------------------------------------------------
# Contract / catalog loading
# ---------------------------------------------------------------------------

async def load_contract(*, force: bool = False) -> Dict[str, Any]:
    """Return the organization-creation contract from the database.

    Cached briefly: it is schema + reference data that only changes when a
    migration runs.  Returns ``{}`` when the database cannot be reached, so
    the caller degrades to "unavailable" instead of inventing a contract.
    """
    now = time.monotonic()
    cached = _contract_cache.get("value")
    fetched_at = float(_contract_cache.get("fetched_at") or 0.0)
    if cached and not force and (now - fetched_at) < _CONTRACT_TTL_SECONDS:
        return cached

    try:
        value = await call_rpc("organization_onboarding_contract")
    except Exception as exc:  # noqa: BLE001 — availability must never 500
        log.warning("onboarding.contract_unavailable", error=str(exc)[:200])
        return cached or {}

    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return cached or {}

    _contract_cache["value"] = value
    _contract_cache["fetched_at"] = now
    return value


async def load_catalog(business_type: str) -> Dict[str, Any]:
    """Return the published chart catalog for one business type."""
    if not business_type:
        return {}
    try:
        value = await call_rpc(
            "account_template_catalog", params={"p_business_type": business_type}
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding.catalog_unavailable", error=str(exc)[:200])
        return {}
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else {}


def parse_model_json(raw: str) -> Dict[str, Any]:
    """Parse the model's JSON reply, tolerating markdown fences and prose.

    Mirrors the tolerance already used by the document extractor so the
    onboarding path behaves like the rest of the system.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty model reply")
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object in model reply")
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("onboarding reply is not a JSON object")
    return data


# ---------------------------------------------------------------------------
# Grounding / validation
# ---------------------------------------------------------------------------

def _clean_text(value: Any, *, limit: int = _MAX_SHORT_TEXT) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _option_values(contract: Dict[str, Any], key: str) -> List[str]:
    """Allowed values for a contract list (business types, currencies) or
    the creation API's argument names."""
    if key == "business_types":
        return [
            str(item.get("value"))
            for item in contract.get("business_types") or []
            if item.get("value")
        ]
    if key == "currencies":
        return [
            str(item.get("code"))
            for item in contract.get("currencies") or []
            if item.get("code")
        ]
    if key == "arguments":
        # The contract reports full argument definitions ("p_name text,
        # p_business_type business_type_code DEFAULT ..."); only the NAME is
        # compared against FIELD_TO_ARGUMENT.
        names: List[str] = []
        for item in (contract.get("rpc") or {}).get("arguments") or []:
            raw = str(item.get("argument") or "").strip()
            if raw:
                names.append(raw.split()[0])
        return names
    return []


def _argument_defaults(contract: Dict[str, Any]) -> Dict[str, Any]:
    return dict(contract.get("backend_defaults") or {})


def _backend_accepts(contract: Dict[str, Any], field: str) -> bool:
    """True when the creation API really has an argument for *field*."""
    argument = FIELD_TO_ARGUMENT.get(field)
    if not argument or not contract:
        return False
    return argument in _option_values(contract, "arguments")


def _question_priority(contract: Dict[str, Any]) -> List[str]:
    """Fields worth ASKING about, in order.

    Only two things are worth forcing a question: a field the backend
    requires, and the business type (it decides which chart of accounts the
    organization gets).  Everything else either has a documented backend
    default or is visible and editable on the review screen, so asking would
    add friction without adding accuracy — the assistant asks about those
    only when the owner's own words make them genuinely ambiguous.
    """
    required = [str(f) for f in contract.get("required_fields") or []]
    ordered = [f for f in required if f in FIELD_TO_ARGUMENT]
    if "business_type" not in ordered and "business_type" in FIELD_TO_ARGUMENT:
        ordered.append("business_type")
    return ordered


def apply_country_locale_hints(fields: Dict[str, Any], sources: Dict[str, str]) -> None:
    """Translate a *stated* country into its timezone/currency defaults.

    Only runs when the owner's own words put the organization in that
    country; it is never used to infer a country.
    """
    country = fields.get("country_code")
    hint = COUNTRY_LOCALE_HINTS.get(str(country).upper()) if country else None
    if not hint:
        return
    timezone, currency = hint
    if not fields.get("timezone"):
        fields["timezone"] = timezone
        sources["timezone"] = "inferred_from_stated_country"
    if not fields.get("base_currency_code"):
        fields["base_currency_code"] = currency
        sources["base_currency_code"] = "inferred_from_stated_country"


def validate_fields(
    proposed: Dict[str, Any],
    contract: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, str], List[Dict[str, Any]]]:
    """Validate model-proposed values against the backend contract.

    Returns ``(fields, source_by_field, rejected)``.

    * Only fields the creation API actually accepts survive; a value for
      any other field is rejected rather than silently forwarded.
    * Values are enum/format/range checked.  A value that fails is rejected
      and recorded — never coerced into something merely plausible.
    """
    fields: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    rejected: List[Dict[str, Any]] = []

    business_types = set(_option_values(contract, "business_types"))
    currencies = set(_option_values(contract, "currencies"))
    defaults = _argument_defaults(contract)
    this_year = time.gmtime().tm_year

    for field, raw_value in (proposed or {}).items():
        if field not in FIELD_TO_ARGUMENT:
            if field not in PREFIX_FIELDS:
                rejected.append({"field": field, "reason": "unknown_field"})
            continue
        if contract and not _backend_accepts(contract, field):
            rejected.append({"field": field, "reason": "not_accepted_by_backend"})
            continue

        source = "inferred"
        value: Any = raw_value

        if field == "name":
            value = _clean_text(value, limit=_MAX_SHORT_TEXT)
            if not value or len(value) < 2:
                rejected.append({"field": field, "reason": "backend_requires_min_2_chars"})
                continue

        elif field == "business_type":
            value = (str(value).strip().upper() if value else "") or None
            if not value:
                rejected.append({"field": field, "reason": "empty_value"})
                continue
            if business_types and value not in business_types:
                rejected.append({"field": field, "reason": "not_a_backend_business_type"})
                continue

        elif field == "base_currency_code":
            value = (str(value).strip().upper() if value else "") or None
            if not value:
                rejected.append({"field": field, "reason": "empty_value"})
                continue
            if not _CURRENCY_RE.match(value) or (currencies and value not in currencies):
                rejected.append({"field": field, "reason": "not_a_supported_currency"})
                continue

        elif field == "country_code":
            value = (str(value).strip().upper() if value else "") or None
            if not value:
                rejected.append({"field": field, "reason": "empty_value"})
                continue
            if not _COUNTRY_RE.match(value):
                rejected.append({"field": field, "reason": "expected_iso_3166_alpha_2"})
                continue

        elif field == "timezone":
            value = _clean_text(value, limit=64)
            if not value:
                rejected.append({"field": field, "reason": "empty_value"})
                continue
            if not _valid_timezone(value):
                rejected.append({"field": field, "reason": "not_a_verifiable_timezone"})
                continue

        elif field == "fiscal_year_end_month":
            try:
                value = int(value)
            except (TypeError, ValueError):
                rejected.append({"field": field, "reason": "not_a_month_number"})
                continue
            bounds = contract.get("fiscal_year_end_month") or {}
            low, high = int(bounds.get("min", 1) or 1), int(bounds.get("max", 12) or 12)
            if not (low <= value <= high):
                rejected.append({"field": field, "reason": "month_out_of_backend_range"})
                continue

        elif field == "fiscal_year_start_year":
            try:
                value = int(value)
            except (TypeError, ValueError):
                rejected.append({"field": field, "reason": "not_a_year"})
                continue
            if not (this_year - 2 <= value <= this_year + 2):
                rejected.append({"field": field, "reason": "year_out_of_supported_range"})
                continue

        else:
            limit = _MAX_LONG_TEXT if field in ("core_services", "industry_details") else _MAX_SHORT_TEXT
            value = _clean_text(value, limit=limit)
            if not value:
                rejected.append({"field": field, "reason": "empty_value"})
                continue

        if field in defaults and str(defaults.get(field)) == str(value):
            # A value equal to the backend default is still information
            # ("we bank in Karachi"), so it is kept — but labelled.
            source = "backend_default"

        fields[field] = value
        sources[field] = source

    return fields, sources, rejected


# Presentation-only labels.  A required field with no phrasing here still
# gets a question generated from the contract (label + validation rule), so
# adding a column to the backend never silently loses a question.
_FIELD_LABELS = {
    "name": "Organization name",
    "business_type": "Business type",
    "legal_name": "Registered legal name",
    "tax_number": "Tax / VAT number",
    "registration_number": "Company registration number",
    "core_services": "Main products or services",
    "industry_details": "Industry",
    "base_currency_code": "Base currency",
    "country_code": "Country",
    "timezone": "Timezone",
    "fiscal_year_end_month": "Fiscal year end month",
    "fiscal_year_start_year": "Fiscal year start year",
}

_FIELD_QUESTIONS = {
    "name": "What is the business called?",
    "business_type": "Which of these best describes the business?",
    "base_currency_code": "Which currency should the books be kept in?",
    "country_code": "Which country is the business based in?",
    "fiscal_year_end_month": "Which month does your financial year end in?",
    "fiscal_year_start_year": "Which year should the first financial year start?",
}

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _question_options(contract: Dict[str, Any], field: str) -> List[Dict[str, str]]:
    """Tap-to-answer options for a field, from the contract's own data."""
    if field == "business_type":
        return [
            {"value": str(item.get("value")), "label": str(item.get("template_name") or item.get("value"))}
            for item in contract.get("business_types") or []
            if item.get("value")
        ]
    if field == "base_currency_code":
        return [
            {"value": str(item.get("code")), "label": f"{item.get('code')} — {item.get('name')}"}
            for item in contract.get("currencies") or []
            if item.get("code")
        ]
    if field == "fiscal_year_end_month":
        return [{"value": str(i), "label": _MONTH_NAMES[i - 1]} for i in range(1, 13)]
    if field == "country_code":
        return [{"value": code, "label": code} for code in COUNTRY_LOCALE_HINTS]
    return []


def _normalise_options(options: Sequence[Any]) -> List[Dict[str, str]]:
    """Normalise tap-to-answer options to ``{value, label}``.

    Contract options are already pairs; options suggested by the assistant
    are plain strings.  One shape on the wire keeps the UI simple.
    """
    normalised: List[Dict[str, str]] = []
    for opt in options or []:
        if isinstance(opt, dict):
            value = _clean_text(opt.get("value"), limit=80)
            label = _clean_text(opt.get("label"), limit=120) or value
        else:
            value = _clean_text(opt, limit=80)
            label = value
        if value:
            normalised.append({"value": value, "label": label or value})
    return normalised[:6]


def _unresolved_fields(contract: Dict[str, Any], fields: Dict[str, Any]) -> List[str]:
    """Fields the assistant did NOT populate, in a stable order.

    Deliberately broader than the questions asked: a field with a backend
    default does not need to interrupt the owner, but the review screen must
    still be able to mark it as "not stated", so a default can never look
    like something the owner confirmed.
    """
    ordered = list(_question_priority(contract))
    for field in FIELD_TO_ARGUMENT:
        if field not in ordered:
            ordered.append(field)
    return [f for f in ordered if f not in fields]


def build_questions(
    contract: Dict[str, Any],
    fields: Dict[str, Any],
    model_questions: Sequence[Dict[str, Any]] = (),
    *,
    max_questions: int = _MAX_QUESTIONS,
) -> List[Dict[str, Any]]:
    """The questions the user still has to answer.

    A model question is kept only when it targets a real backend field, is
    not already answered by a validated value, and states why it is needed.
    Fields the backend itself requires — and the ones that decide which
    accounts exist — are always asked when still missing, even if the model
    forgot them.
    """
    questions: List[Dict[str, Any]] = []
    asked: set = set()

    for raw in model_questions or []:
        if not isinstance(raw, dict):
            continue
        field = str(raw.get("field") or "").strip()
        text = _clean_text(raw.get("question"), limit=300)
        if field not in FIELD_TO_ARGUMENT or not text:
            continue
        if field in fields or field in asked:
            continue
        asked.add(field)
        questions.append({
            "id": field,
            "field": field,
            "question": text,
            "why": _clean_text(raw.get("why"), limit=300) or None,
            # Contract options (real values) win; the assistant's own
            # suggestions are offered when the field has no fixed value set.
            "options": _normalise_options(_question_options(contract, field))
            or _normalise_options(raw.get("options") or []),
            "source": "assistant",
        })

    for field in _question_priority(contract):
        if field in fields or field in asked:
            continue
        asked.add(field)
        questions.append({
            "id": field,
            "field": field,
            "question": _FIELD_QUESTIONS.get(
                field, f"Please provide the {_FIELD_LABELS.get(field, field)}."
            ),
            "why": "Required by the backend to create the organization"
            if field in (contract.get("required_fields") or [])
            else "Determines which accounts and reports the organization gets",
            "options": _normalise_options(_question_options(contract, field)),
            "source": "backend_requirement",
        })

    return questions[:max_questions]


# ---------------------------------------------------------------------------
# The AI step
# ---------------------------------------------------------------------------

def _contract_view(contract: Dict[str, Any]) -> Dict[str, Any]:
    """The compact contract the model is allowed to reason over."""
    bundles = {
        str(item.get("code")): str(item.get("label"))
        for item in contract.get("account_bundles") or []
        if item.get("code")
    }
    bundle_map: Dict[str, List[str]] = {}
    for business_type, entries in (contract.get("bundles_by_business_type") or {}).items():
        codes: List[str] = []
        for entry in entries or []:
            code = str((entry or {}).get("code") or "")
            if code:
                codes.append(f"{code}*" if (entry or {}).get("recommended") else code)
        bundle_map[str(business_type)] = codes

    return {
        "required_fields": contract.get("required_fields") or [],
        "backend_defaults": contract.get("backend_defaults") or {},
        "validation_rules": (contract.get("rpc") or {}).get("validation_rules") or [],
        "allowed_fields": {f: FIELD_TO_ARGUMENT[f] for f in sorted(FIELD_TO_ARGUMENT)},
        "business_types": [
            {
                "value": item.get("value"),
                "chart": item.get("template_name"),
                "base_accounts": item.get("base_account_count"),
            }
            for item in contract.get("business_types") or []
        ],
        "currencies": [item.get("code") for item in contract.get("currencies") or []],
        "account_bundles": bundles,
        "bundles_by_business_type": bundle_map,
    }


def _owner_context(
    answers: Sequence[Dict[str, Any]] = (),
    history: Sequence[Dict[str, str]] = (),
) -> Tuple[str, str]:
    """Render the clarifications and conversation already exchanged."""
    clarification_text = ""
    for item in answers or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("id") or "").strip()
        value = str(item.get("answer") or "").strip()
        if field and value:
            clarification_text += f"- {field}: {value}\n"

    history_text = ""
    for turn in list(history or [])[-6:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history_text += f"{role.title()}: {content[:400]}\n"

    return clarification_text, history_text


_PROMPT_RULES = (
    "SYSTEM: You are the onboarding accountant inside an AI-native accounting and ERP "
    "product. You translate a business owner's own plain-language description into the "
    "organization configuration the backend accepts.\n\n"
    "NON-NEGOTIABLE RULES\n"
    "1. Use ONLY the backend contract below. Never invent a field, business type, "
    "currency, account bundle or account. Every key you output must be one of "
    "allowed_fields.\n"
    "2. Use only what the owner actually said. Never assume a fact they did not state "
    "(do not assume inventory, stock, employees, vehicles, machinery, loans, VAT "
    "registration or exports). Absence of evidence is not evidence: if they did not "
    "mention it, leave it out and say so.\n"
    "3. Ask a question ONLY when the missing information is required by the backend, or "
    "when it genuinely changes which accounts and reports the organization needs (for "
    "example: does the business sell goods it holds as stock, or only services?). Keep it "
    "to at most THREE questions and offer 2-5 short options where a choice is possible.\n"
    "4. Account bundles: propose only codes listed for the business type you chose. A "
    "code marked with * is the product's default for that type; keep it only if this "
    "description supports it, and give a short reason for every bundle you propose. "
    "Never pre-select a bundle whose premise the owner did not state - do not select "
    "employee-benefit, borrowing, lease, insurance or tax bundles unless they actually "
    "mentioned staff, a loan, a lease, insurance or tax. They stay available for the "
    "owner to tick, so let them decide.\n"
    "5. Never finalise anything. Your output is a proposal the owner reviews and edits.\n"
    "6. Output STRICT JSON ONLY - no markdown, no commentary.\n\n"
)

_PROMPT_SCHEMA = (
    "OUTPUT SCHEMA:\n"
    "{\n"
    '  "fields": { "<field name>": <value> },\n'
    '  "field_notes": { "<field name>": "why you derived it" },\n'
    '  "questions": [ { "field": "<field name>", "question": "...", '
    '"why": "...", "options": ["..."] } ],\n'
    '  "account_groups": [ { "code": "<bundle code>", "reason": "..." } ],\n'
    '  "summary": "two short sentences for the review screen"\n'
    "}\n\n"
    "Fields you may populate: name, business_type, legal_name, tax_number, "
    "registration_number, core_services, industry_details, base_currency_code, "
    "country_code (ISO 3166 alpha-2), timezone (IANA), fiscal_year_end_month (1-12), "
    "fiscal_year_start_year. Omit any field you cannot support from the owner's words - "
    "never send a guessed value.\n\n"
)

def build_prompt(
    contract: Dict[str, Any],
    *,
    description: str,
    answers: Sequence[Dict[str, Any]] = (),
    history: Sequence[Dict[str, str]] = (),
    business_type_hint: Optional[str] = None,
) -> str:
    """Assemble the grounded prompt for the provider chain.

    The contract is embedded so the model can choose only real business
    types, real currencies and real account bundles.
    """
    clarification_text, history_text = _owner_context(answers, history)

    prompt = (
        _PROMPT_RULES
        + "BACKEND CONTRACT (the real system):\n"
        + json.dumps(_contract_view(contract), ensure_ascii=False)
        + "\n\n"
        + _PROMPT_SCHEMA
    )
    if business_type_hint:
        prompt += (
            "The owner already selected this business type in the form: "
            f"{business_type_hint}. Treat it as a strong hint, but correct it if their "
            "description clearly contradicts it.\n\n"
        )
    prompt += f"OWNER'S DESCRIPTION:\n{description.strip()[:4000]}\n\n"
    if clarification_text:
        prompt += f"CLARIFICATIONS THE OWNER ALREADY GAVE:\n{clarification_text}\n"
    if history_text:
        prompt += f"CONVERSATION SO FAR:\n{history_text}\n"
    prompt += (
        "Answer with the JSON object now. Only what they actually told you, and only "
        "bundles that exist for the business type."
    )
    return prompt


async def _ask_model(prompt: str) -> str:
    """Run the grounded prompt on the standard provider chain (lazy import:
    the heavy agent stack must not be pulled into onboarding cold starts)."""
    from app.ai_orchestrator import get_client

    return await get_client().generate_text(prompt=prompt, context=None)


def _resolve_account_groups(
    contract: Dict[str, Any],
    catalog: Dict[str, Any],
    business_type: Optional[str],
    proposed: Sequence[Dict[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """Turn the model's bundle proposals into the catalog's real bundles.

    Bundles come from the published catalog for the chosen business type,
    so the list can only contain accounts the backend can create.
    """
    if not business_type:
        return []

    allowed: Dict[str, bool] = {}
    for entry in (contract.get("bundles_by_business_type") or {}).get(business_type) or []:
        code = str((entry or {}).get("code") or "")
        if code:
            allowed[code] = bool(entry.get("recommended"))

    reasons: Dict[str, str] = {}
    chosen: List[str] = []
    for item in proposed or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().upper()
        if code in allowed and code not in chosen:
            chosen.append(code)
            reason = _clean_text(item.get("reason"), limit=240)
            if reason:
                reasons[code] = reason

    groups: List[Dict[str, Any]] = []
    for group in catalog.get("optional_groups") or []:
        code = str(group.get("code") or "")
        if not code or code not in allowed:
            continue
        recommended = bool(allowed[code])
        groups.append({
            "code": code,
            "label": group.get("label"),
            "description": group.get("description"),
            "recommended": recommended,
            "selected": recommended or code in chosen,
            "reason": reasons.get(code)
            or ("Recommended for this business type" if recommended else None),
            "account_count": group.get("account_count"),
            "accounts": group.get("accounts") or [],
        })
    return groups


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _unavailable(
    contract: Dict[str, Any],
    *,
    reason: str,
    description: str,
    business_type_hint: Optional[str],
    model_reply: str = "",
) -> Dict[str, Any]:
    """Honest degradation: report that the description could not be
    analysed, keep only the owner's own selection, ask for what the backend
    requires, and invent nothing."""
    fields: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    if business_type_hint:
        validated, validated_sources, _ = validate_fields(
            {"business_type": business_type_hint}, contract
        )
        fields.update(validated)
        sources.update(validated_sources)

    log.warning("onboarding.analysis_unavailable", reason=reason)
    return {
        "status": "unavailable",
        "used_ai": False,
        "reason": reason,
        "summary": (
            "The assistant could not analyse the description just now, so nothing was "
            "filled in for you. Describe the business again, or set the fields yourself "
            "— the chart of accounts is generated from the business type either way."
        ),
        "fields": fields,
        "field_sources": sources,
        "field_notes": {},
        "unresolved": _unresolved_fields(contract, fields),
        "questions": build_questions(contract, fields),
        "account_groups": [],
        "rejected": [],
        "analysis": {"raw_reply": (model_reply or "")[:2000]} if model_reply else {},
        "echo": {"description_length": len(description or "")},
    }


async def analyze_organization(
    *,
    description: str,
    business_type: Optional[str] = None,
    answers: Optional[Sequence[Dict[str, Any]]] = None,
    history: Optional[Sequence[Dict[str, str]]] = None,
    use_ai: bool = True,
) -> Dict[str, Any]:
    """Analyse a plain-language business description into onboarding fields.

    The result is a PROPOSAL for the user to review; nothing is created and
    nothing is finalised here.
    """
    description = (description or "").strip()

    contract = await load_contract()
    if not contract:
        return _unavailable(
            {},
            reason="backend_contract_unavailable",
            description=description,
            business_type_hint=business_type,
        )

    answers = list(answers or [])
    if not description and not answers:
        return _unavailable(
            contract,
            reason="no_description_supplied",
            description=description,
            business_type_hint=business_type,
        )

    raw_reply = ""
    if use_ai:
        prompt = build_prompt(
            contract,
            description=description,
            answers=answers,
            history=history or [],
            business_type_hint=business_type,
        )
        try:
            raw_reply = await _ask_model(prompt)
        except Exception as exc:  # noqa: BLE001 — provider issues never 500
            log.warning("onboarding.provider_failed", error=str(exc)[:200])
            raw_reply = ""

    if not (raw_reply or "").strip():
        return _unavailable(
            contract,
            reason="no_ai_provider_available",
            description=description,
            business_type_hint=business_type,
        )

    try:
        payload = parse_model_json(raw_reply)
    except Exception as exc:  # noqa: BLE001 — malformed replies are reported
        return _unavailable(
            contract,
            reason=f"unparseable_model_reply: {str(exc)[:120]}",
            description=description,
            business_type_hint=business_type,
            model_reply=raw_reply,
        )

    proposed_fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    fields, sources, rejected = validate_fields(proposed_fields, contract)

    # The form selection is a hint the model may correct; when the model did
    # not propose a type, keep the owner's own choice instead of dropping it.
    if business_type and "business_type" not in fields:
        validated, validated_sources, _ = validate_fields(
            {"business_type": business_type}, contract
        )
        fields.update(validated)
        sources.update(validated_sources)

    apply_country_locale_hints(fields, sources)

    resolved_type = fields.get("business_type")
    catalog = await load_catalog(str(resolved_type)) if resolved_type else {}

    model_questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
    questions = build_questions(contract, fields, model_questions)

    raw_notes = payload.get("field_notes") if isinstance(payload.get("field_notes"), dict) else {}
    field_notes: Dict[str, str] = {}
    for key, value in raw_notes.items():
        note = _clean_text(value, limit=300)
        if str(key) in FIELD_TO_ARGUMENT and note:
            field_notes[str(key)] = note

    proposed_groups = payload.get("account_groups")
    return {
        "status": "needs_information" if questions else "ok",
        "used_ai": True,
        "reason": None,
        "summary": _clean_text(payload.get("summary"), limit=600)
        or "Here is what I understood from your description — please check and correct it.",
        "fields": fields,
        "field_sources": sources,
        "field_notes": field_notes,
        "unresolved": _unresolved_fields(contract, fields),
        "questions": questions,
        "account_groups": _resolve_account_groups(
            contract,
            catalog,
            str(resolved_type) if resolved_type else None,
            proposed_groups if isinstance(proposed_groups, list) else [],
        ),
        "rejected": rejected,
        "analysis": {
            "business_type_chart": (catalog.get("template") or {}).get("name"),
            "base_account_count": len(catalog.get("base_accounts") or []),
            "optional_group_count": len(catalog.get("optional_groups") or []),
        },
        "echo": {"description_length": len(description), "answers": len(answers)},
    }


def build_schema_payload(
    contract: Dict[str, Any],
    catalog: Optional[Dict[str, Any]] = None,
    business_type: Optional[str] = None,
) -> Dict[str, Any]:
    """What the onboarding UI needs in order to render real
    backend-compatible choices (business types, currencies, months, required
    fields) and, once a business type is chosen, the chart it will produce.
    """
    payload: Dict[str, Any] = {
        "available": bool(contract),
        "required_fields": contract.get("required_fields") or [],
        "defaults": _argument_defaults(contract),
        "fiscal_year_end_month": contract.get("fiscal_year_end_month") or {"min": 1, "max": 12},
        "business_types": contract.get("business_types") or [],
        "currencies": contract.get("currencies") or [],
        "account_bundles": contract.get("account_bundles") or [],
        "bundles_by_business_type": contract.get("bundles_by_business_type") or {},
        "validation_rules": (contract.get("rpc") or {}).get("validation_rules") or [],
        "countries": [
            {"code": code, "timezone": tz, "currency": cur}
            for code, (tz, cur) in COUNTRY_LOCALE_HINTS.items()
        ],
        "business_type": business_type,
    }
    if catalog:
        payload["catalog"] = catalog
    return payload