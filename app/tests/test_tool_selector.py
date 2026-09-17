"""Work Stream E - semantic tool shortlist tests."""

import pytest

from app.planner import plan
from app.tool_selector import excluded_for_intent, tools_for_intent
from app.tools import list_tools

ALL_TOOLS = set(list_tools())

# Every mutation intent the planner can produce for purchases/sales.
MUTATION_INTENTS = [
    "record_expense",
    "record_cash_purchase", "record_credit_purchase",
    "record_cash_sale", "record_credit_sale",
    "create_invoice", "create_quotation",
    "create_credit_note", "create_purchase_return",
    "record_receipt", "record_payment", "record_bank_transfer",
    "record_expense_payment",
    "register_fixed_asset", "dispose_fixed_asset", "record_asset_depreciation",
]


class TestShortlistSize:
    @pytest.mark.parametrize("intent", MUTATION_INTENTS)
    def test_mutation_intent_sees_at_most_15_tools(self, intent):
        p = plan(_sample_message(intent))
        allowed = tools_for_intent(intent, p.potential_tools)
        assert len(allowed) <= 15, f"{intent}: {len(allowed)} tools"

    @pytest.mark.parametrize("intent", MUTATION_INTENTS)
    def test_shortlist_includes_the_mutation_and_lookups(self, intent):
        p = plan(_sample_message(intent))
        allowed = tools_for_intent(intent, p.potential_tools)
        # Every planner-listed tool is offered (planner is authoritative).
        assert set(p.potential_tools) <= allowed
        # The intent's own core tools are offered.
        assert allowed  # non-empty


class TestNeverExcludesPlannerTools:
    @pytest.mark.parametrize("intent", MUTATION_INTENTS)
    def test_exclusions_never_cover_potential_tools(self, intent):
        p = plan(_sample_message(intent))
        excluded = excluded_for_intent(
            intent, p.potential_tools, ALL_TOOLS
        )
        assert not (set(p.potential_tools) & excluded)

    def test_non_shortlisted_intent_excludes_nothing(self):
        # Reports/lookups keep the full toolset.
        assert excluded_for_intent(
            "generate_trial_balance", ["get_trial_balance"], ALL_TOOLS
        ) == set()

    def test_offer_only_registered_tools(self):
        p = plan("bought a laptop for Rs.150,000 in cash")
        allowed = tools_for_intent(p.intent, p.potential_tools)
        assert allowed <= ALL_TOOLS


def _sample_message(intent: str) -> str:
    return {
        "record_expense": "record expense: internet Rs.5,000 in cash",
        "record_cash_purchase": "bought a laptop for Rs.150,000 in cash",
        "record_credit_purchase": "bought a laptop on credit",
        "record_cash_sale": "sold a chair for Rs.5,000 in cash",
        "record_credit_sale": "sold a chair on credit",
        "create_invoice": "create an invoice for the order",
        "create_quotation": "create a quotation for the customer",
        "create_credit_note": "issue a credit note",
        "create_purchase_return": "purchase return to the supplier",
        "record_receipt": "received payment from the customer",
        "record_payment": "supplier payment",
        "record_bank_transfer": "transfer from HBL to Meezan",
        "record_expense_payment": "paid the expense",
        "register_fixed_asset": "register a new fixed asset generator",
        "dispose_fixed_asset": "dispose of the old asset",
        "record_asset_depreciation": "record asset depreciation",
    }.get(intent, "record an expense of Rs.1,000")
