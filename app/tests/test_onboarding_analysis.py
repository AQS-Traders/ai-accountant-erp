"""
Onboarding assistant tests — stubbed provider and stubbed RPC, no network.

These tests pin the behaviour the requirements demand:
  * only real backend fields are ever populated (no AI-only fields);
  * nothing is invented — a fact the owner did not state is reported as
    unresolved instead of being assumed;
  * the backend's own requirements drive the questions;
  * account bundles can only come from the published catalog for the chosen
    business type;
  * the assistant reports honestly when it cannot analyse at all.
"""

from __future__ import annotations

import json

import pytest

import app.onboarding_analysis as oa

# ---------------------------------------------------------------------------
# Fixtures: a contract/catalog shaped like the real ones (see migration 070)
# ---------------------------------------------------------------------------

CONTRACT = {
    "rpc": {
        "name": "create_organization",
        "signature": "p_name text, p_business_type business_type_code DEFAULT 'OTHER'::business_type_code",
        "arguments": [
            {"argument": "p_name text", "has_default": False, "default": None},
            {"argument": "p_business_type business_type_code DEFAULT 'OTHER'::business_type_code",
             "has_default": True, "default": "OTHER"},
            {"argument": "p_base_currency_code character DEFAULT 'PKR'::bpchar",
             "has_default": True, "default": "PKR"},
            {"argument": "p_country_code character DEFAULT 'PK'::bpchar",
             "has_default": True, "default": "PK"},
            {"argument": "p_timezone text DEFAULT 'Asia/Karachi'::text",
             "has_default": True, "default": "Asia/Karachi"},
            {"argument": "p_fiscal_year_end_month smallint DEFAULT 6",
             "has_default": True, "default": "6"},
            {"argument": "p_fiscal_year_start_year integer DEFAULT NULL::integer",
             "has_default": True, "default": None},
            {"argument": "p_core_services text DEFAULT NULL::text",
             "has_default": True, "default": None},
            {"argument": "p_industry_details text DEFAULT NULL::text",
             "has_default": True, "default": None},
        ],
        "validation_rules": [
            "Authentication required",
            "Organization name must be at least 2 characters",
        ],
    },
    "required_fields": ["name"],
    "backend_defaults": {
        "business_type": "OTHER",
        "base_currency_code": "PKR",
        "country_code": "PK",
        "timezone": "Asia/Karachi",
        "fiscal_year_end_month": "6",
    },
    "business_types": [
        {"value": "SOFTWARE_HOUSE", "template_code": "SOFTWARE_HOUSE",
         "template_name": "Software House", "base_account_count": 52,
         "optional_group_count": 22},
        {"value": "TRADING", "template_code": "TRADING",
         "template_name": "Trading Business", "base_account_count": 51,
         "optional_group_count": 25},
    ],
    "currencies": [
        {"code": "PKR", "name": "Pakistani Rupee", "symbol": "Rs.", "decimal_places": 2},
        {"code": "AED", "name": "UAE Dirham", "symbol": "AED", "decimal_places": 2},
    ],
    "account_bundles": [
        {"code": "INVENTORY", "label": "Inventory", "description": "Stock held for resale"},
        {"code": "COST_OF_SALES", "label": "Cost of Sales - Goods", "description": "Purchases"},
        {"code": "CLOUD_HOSTING", "label": "Cloud Infrastructure", "description": "Hosting"},
        {"code": "FA_MACHINERY", "label": "Machinery & Plant", "description": "Machinery"},
    ],
    "bundles_by_business_type": {
        "SOFTWARE_HOUSE": [
            {"code": "CLOUD_HOSTING", "recommended": True},
            {"code": "FA_MACHINERY", "recommended": False},
        ],
        "TRADING": [
            {"code": "INVENTORY", "recommended": True},
            {"code": "COST_OF_SALES", "recommended": True},
        ],
    },
    "fiscal_year_end_month": {"min": 1, "max": 12},
}

CATALOGS = {
    "SOFTWARE_HOUSE": {
        "business_type": "SOFTWARE_HOUSE",
        "template": {"code": "SOFTWARE_HOUSE", "name": "Software House"},
        "base_accounts": [{"code": "1010", "name": "Bank"}],
        "optional_groups": [
            {"code": "CLOUD_HOSTING", "label": "Cloud Infrastructure",
             "description": "Cloud hosting and infrastructure costs", "recommended": True,
             "account_count": 1, "accounts": [{"code": "6420", "name": "Cloud Infrastructure"}]},
            {"code": "FA_MACHINERY", "label": "Machinery & Plant",
             "description": "Production / plant machinery", "recommended": False,
             "account_count": 2, "accounts": [{"code": "1550", "name": "Machinery & Plant"}]},
        ],
    },
    "TRADING": {
        "business_type": "TRADING",
        "template": {"code": "TRADING", "name": "Trading Business"},
        "base_accounts": [{"code": "1010", "name": "Bank"},
                          {"code": "4120", "name": "Sales Revenue"}],
        "optional_groups": [
            {"code": "INVENTORY", "label": "Inventory", "description": "Stock held for resale",
             "recommended": True, "account_count": 1,
             "accounts": [{"code": "1200", "name": "Inventory"}]},
            {"code": "COST_OF_SALES", "label": "Cost of Sales - Goods",
             "description": "Purchases, cost of goods sold", "recommended": True,
             "account_count": 3,
             "accounts": [{"code": "6810", "name": "Purchases"}]},
        ],
    },
}


class StubProvider:
    """Stand-in for the provider chain (records the prompts it was given)."""

    def __init__(self, reply: str = ""):
        self.reply = reply
        self.prompts: list = []

    async def generate_text(self, *, prompt: str, context=None):  # noqa: ANN001
        self.prompts.append(prompt)
        return self.reply


@pytest.fixture
def stub_rpc(monkeypatch):
    """Contract + catalog lookups served from the fixtures above."""

    async def fake_call_rpc(function_name: str, *, params=None):  # noqa: ANN001
        if function_name == "organization_onboarding_contract":
            return CONTRACT
        if function_name == "account_template_catalog":
            return CATALOGS.get((params or {}).get("p_business_type"), {})
        raise AssertionError(f"unexpected RPC {function_name}")

    monkeypatch.setattr(oa, "call_rpc", fake_call_rpc)
    # load_contract caches process-wide: give every test a clean cache.
    monkeypatch.setattr(oa, "_contract_cache", {"value": None, "fetched_at": 0.0})
    return fake_call_rpc


@pytest.fixture
def stub_provider(monkeypatch):
    """Install a stubbed provider chain and return it."""

    def install(reply: str) -> StubProvider:
        provider = StubProvider(reply)
        import app.ai_orchestrator as orchestrator

        monkeypatch.setattr(orchestrator, "get_client", lambda: provider)
        return provider

    return install


def _reply(**kwargs) -> str:
    return json.dumps(kwargs)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_model_json_accepts_fences_and_prose():
    fenced = '```json\n{"fields": {"name": "Acme"}}\n```'
    assert oa.parse_model_json(fenced)["fields"]["name"] == "Acme"

    prose = 'Sure! Here you go:\n{"fields": {"name": "Acme"}}\nHope that helps.'
    assert oa.parse_model_json(prose)["fields"]["name"] == "Acme"


def test_parse_model_json_rejects_a_non_object():
    with pytest.raises(ValueError):
        oa.parse_model_json("no json here")


# ---------------------------------------------------------------------------
# Grounding: only real backend fields, no invention
# ---------------------------------------------------------------------------

def test_only_backend_fields_survive_validation():
    fields, sources, rejected = oa.validate_fields(
        {
            "name": "Zameer Labs",
            "business_type": "software_house",          # case-insensitive
            "base_currency_code": "aed",
            "inventory": True,                          # AI-only field
            "accounts": [{"code": "9999"}],             # AI-only field
        },
        CONTRACT,
    )
    assert fields == {
        "name": "Zameer Labs",
        "business_type": "SOFTWARE_HOUSE",
        "base_currency_code": "AED",
    }
    assert sources["business_type"] == "inferred"
    assert {item["field"] for item in rejected} == {"inventory", "accounts"}


def test_invented_values_are_rejected_not_coerced():
    fields, _, rejected = oa.validate_fields(
        {
            "business_type": "RESTAURANT",     # not a backend business type
            "base_currency_code": "XYZ",       # not a supported currency
            "country_code": "GBR",             # not alpha-2
            "timezone": "Mars/Olympus",        # not IANA
            "fiscal_year_end_month": 15,       # outside the backend range
            "fiscal_year_start_year": 1900,    # outside the supported range
            "name": "Z",                       # backend needs >= 2 chars
        },
        CONTRACT,
    )
    assert fields == {}
    reasons = {item["field"]: item["reason"] for item in rejected}
    assert reasons["business_type"] == "not_a_backend_business_type"
    assert reasons["base_currency_code"] == "not_a_supported_currency"
    assert reasons["country_code"] == "expected_iso_3166_alpha_2"
    assert reasons["timezone"] == "not_a_verifiable_timezone"
    assert reasons["fiscal_year_end_month"] == "month_out_of_backend_range"
    assert reasons["fiscal_year_start_year"] == "year_out_of_supported_range"
    assert reasons["name"] == "backend_requires_min_2_chars"


# ---------------------------------------------------------------------------
# End-to-end analysis (stubbed provider)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_populates_fields_and_reports_what_is_unresolved(stub_rpc, stub_provider):
    stub_provider(_reply(
        fields={
            "name": "Zameer Labs (Pvt) Ltd",
            "business_type": "SOFTWARE_HOUSE",
            "core_services": "Web and mobile development, maintenance contracts",
            "industry_details": "Software services",
            "fiscal_year_end_month": 6,
        },
        field_notes={"business_type": "They build custom software for clients."},
        account_groups=[{"code": "CLOUD_HOSTING", "reason": "Hosting is a named cost"}],
        summary="A software house delivering client projects.",
    ))

    result = await oa.analyze_organization(
        description="We are a small software house building web and mobile apps for clients.",
    )

    assert result["status"] == "ok"
    assert result["fields"]["business_type"] == "SOFTWARE_HOUSE"
    assert result["fields"]["fiscal_year_end_month"] == 6
    assert result["field_sources"]["fiscal_year_end_month"] == "backend_default"
    assert result["field_notes"]["business_type"].startswith("They build custom software")
    assert result["analysis"]["business_type_chart"] == "Software House"
    # Facts the owner never mentioned are reported, not invented.
    assert "legal_name" in result["unresolved"]
    assert "tax_number" in result["unresolved"]
    assert "legal_name" not in result["fields"]
    assert result["questions"] == []


@pytest.mark.asyncio
async def test_backend_required_field_drives_a_question_when_the_ai_omits_it(
    stub_rpc, stub_provider
):
    stub_provider(_reply(fields={"business_type": "TRADING"}, summary="Trading."))

    result = await oa.analyze_organization(description="We buy and resell electrical goods.")

    assert result["status"] == "needs_information"
    question = next(q for q in result["questions"] if q["field"] == "name")
    assert question["source"] == "backend_requirement"
    assert "backend" in question["why"].lower()


@pytest.mark.asyncio
async def test_inventory_is_never_assumed_when_the_owner_did_not_say_it(stub_rpc, stub_provider):
    stub_provider(_reply(
        fields={"name": "Acme Consultancy", "business_type": "SOFTWARE_HOUSE"},
        account_groups=[],
        summary="Consultancy.",
    ))

    result = await oa.analyze_organization(description="We provide consulting to clients.")

    codes = {group["code"]: group for group in result["account_groups"]}
    # A software house is never offered inventory at all...
    assert "INVENTORY" not in codes
    # ...and machinery is offered but NOT selected without evidence.
    assert codes["FA_MACHINERY"]["selected"] is False
    # The product default for the type is offered and selected.
    assert codes["CLOUD_HOSTING"]["recommended"] is True
    assert codes["CLOUD_HOSTING"]["selected"] is True


@pytest.mark.asyncio
async def test_inventory_bundle_is_selected_when_the_owner_confirms_stock(
    stub_rpc, stub_provider
):
    stub_provider(_reply(
        fields={"name": "Marazi Traders", "business_type": "TRADING"},
        account_groups=[
            {"code": "INVENTORY", "reason": "They hold stock for resale."},
            {"code": "FA_MACHINERY", "reason": "Not offered for this type."},
        ],
        summary="A trading business holding stock.",
    ))

    result = await oa.analyze_organization(
        description="We buy electrical goods and resell them from our warehouse."
    )

    codes = {group["code"]: group for group in result["account_groups"]}
    assert codes["INVENTORY"]["selected"] is True
    assert codes["INVENTORY"]["reason"] == "They hold stock for resale."
    assert codes["INVENTORY"]["accounts"][0]["code"] == "1200"
    # A bundle that is not offered for this business type cannot sneak in.
    assert "FA_MACHINERY" not in codes


@pytest.mark.asyncio
async def test_provider_failure_degrades_honestly(stub_rpc, stub_provider):
    stub_provider("")  # the provider chain exhausted (generate_text returns "")

    result = await oa.analyze_organization(
        description="We sell software services.",
        business_type="SOFTWARE_HOUSE",
    )

    assert result["status"] == "unavailable"
    assert result["used_ai"] is False
    assert result["reason"] == "no_ai_provider_available"
    # Only the owner's own form selection survives — nothing was invented.
    assert result["fields"] == {"business_type": "SOFTWARE_HOUSE"}
    assert result["account_groups"] == []
    # The backend requirement is still surfaced.
    assert any(q["field"] == "name" for q in result["questions"])


@pytest.mark.asyncio
async def test_unparseable_reply_is_reported_not_guessed(stub_rpc, stub_provider):
    stub_provider("I am afraid I cannot answer that.")

    result = await oa.analyze_organization(description="A trading business.")

    assert result["status"] == "unavailable"
    assert result["reason"].startswith("unparseable_model_reply")
    assert result["fields"] == {}
    assert "cannot answer" in result["analysis"]["raw_reply"]


@pytest.mark.asyncio
async def test_missing_contract_disables_the_assistant(stub_rpc, stub_provider, monkeypatch):
    async def no_contract(function_name, *, params=None):  # noqa: ANN001
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(oa, "call_rpc", no_contract)
    stub_provider(_reply(fields={"name": "Unused"}))

    result = await oa.analyze_organization(description="Anything at all.")

    assert result["status"] == "unavailable"
    assert result["reason"] == "backend_contract_unavailable"
    assert result["fields"] == {}


@pytest.mark.asyncio
async def test_empty_description_asks_instead_of_inventing(stub_rpc, stub_provider):
    stub_provider(_reply(fields={"name": "Ghost Ltd"}))

    result = await oa.analyze_organization(description="   ")

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_description_supplied"
    assert result["fields"] == {}


@pytest.mark.asyncio
async def test_country_is_only_translated_when_the_owner_stated_it(stub_rpc, stub_provider):
    stub_provider(_reply(
        fields={"name": "Gulf Ops", "business_type": "TRADING", "country_code": "AE"},
    ))
    with_country = await oa.analyze_organization(description="We trade in Dubai.")

    assert with_country["fields"]["timezone"] == "Asia/Dubai"
    assert with_country["fields"]["base_currency_code"] == "AED"
    assert with_country["field_sources"]["timezone"] == "inferred_from_stated_country"

    stub_provider(_reply(fields={"name": "Gulf Ops", "business_type": "TRADING"}))
    without_country = await oa.analyze_organization(description="We trade.")

    # Nothing is invented: no country, no currency and no timezone.
    assert "country_code" not in without_country["fields"]
    assert "timezone" not in without_country["fields"]
    assert "base_currency_code" not in without_country["fields"]
    # Those fields carry documented backend defaults and stay editable on the
    # review screen, so the assistant does not force a question about them.
    assert without_country["questions"] == []


@pytest.mark.asyncio
async def test_assistant_questions_are_kept_only_for_real_fields(stub_rpc, stub_provider):
    stub_provider(_reply(
        fields={"name": "Acme", "business_type": "SOFTWARE_HOUSE"},
        questions=[
            {"field": "core_services", "question": "What do you mainly sell?",
             "why": "It decides the revenue accounts", "options": ["Projects", "Retainers"]},
            {"field": "favourite_colour", "question": "What is your favourite colour?",
             "why": "Not a backend field"},
            {"field": "name", "question": "Name?", "why": "already answered"},
        ],
    ))

    result = await oa.analyze_organization(description="Software services.")

    assert [q["field"] for q in result["questions"]] == ["core_services"]
    # Assistant-suggested options are normalised to {value, label} pairs.
    assert result["questions"][0]["options"] == [
        {"value": "Projects", "label": "Projects"},
        {"value": "Retainers", "label": "Retainers"},
    ]
    assert result["status"] == "needs_information"


@pytest.mark.asyncio
async def test_prompt_is_grounded_in_the_contract(stub_rpc, stub_provider):
    provider = stub_provider(_reply(fields={"name": "Acme", "business_type": "TRADING"}))

    await oa.analyze_organization(
        description="We import and resell generators.",
        answers=[{"field": "name", "answer": "Acme Trading"}],
        history=[{"role": "assistant", "content": "What do you sell?"}],
    )

    prompt = provider.prompts[0]
    # The real contract travels with the prompt...
    assert "TRADING" in prompt and "SOFTWARE_HOUSE" in prompt
    assert "Organization name must be at least 2 characters" in prompt
    assert "INVENTORY" in prompt and "COST_OF_SALES" in prompt
    # ...together with the owner's words and clarifications.
    assert "We import and resell generators." in prompt
    assert "Acme Trading" in prompt
    assert "never send a guessed value" in prompt


def test_schema_payload_exposes_the_real_choices():
    payload = oa.build_schema_payload(CONTRACT, CATALOGS["TRADING"], "TRADING")

    assert payload["available"] is True
    assert payload["required_fields"] == ["name"]
    assert {item["value"] for item in payload["business_types"]} == {"SOFTWARE_HOUSE", "TRADING"}
    assert {item["code"] for item in payload["currencies"]} == {"PKR", "AED"}
    assert payload["defaults"]["base_currency_code"] == "PKR"
    assert payload["fiscal_year_end_month"] == {"min": 1, "max": 12}
    assert payload["catalog"]["template"]["name"] == "Trading Business"
    assert payload["business_type"] == "TRADING"
    assert payload["validation_rules"]


def test_schema_payload_reports_unavailable_without_a_contract():
    payload = oa.build_schema_payload({})
    assert payload["available"] is False
    assert payload["business_types"] == []
    assert "catalog" not in payload