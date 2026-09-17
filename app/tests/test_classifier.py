"""
Unit tests for the deterministic transaction classifier and the semantic
error normalizer (offline — no DB, no LLM).

Run: venv\\Scripts\\python -m pytest app/tests/test_classifier.py -v
"""

from __future__ import annotations

import pytest

from app.classifier import (
    DEPOSIT_ADVANCE,
    INVENTORY,
    OPERATING_EXPENSE,
    SERVICE,
    _search_account_by_nature,
    rule_based_nature,
)
from app.error_normalizer import (
    INFRASTRUCTURE_ERROR,
    MISSING_REQUIRED_VALUE,
    DUPLICATE_RECORD,
    INVALID_REFERENCE,
    normalize_error,
    build_missing_field_question,
    build_duplicate_guidance,
)
from app.repositories import account_repository


def _acct(code: str, name: str, acct_type: str = "EXPENSE") -> dict:
    return {
        "id": f"id-{code}", "code": code, "name": name,
        "account_type": acct_type, "normal_balance": (
            "DEBIT" if acct_type in ("ASSET", "EXPENSE") else "CREDIT"
        ),
        "is_active": True, "is_control_account": False,
    }


GENERAL = _acct("6000", "General Operating Expense")
SALARIES = _acct("6010", "Salaries")
SUPPLIES = _acct("6100", "Office Supplies")
UTILITIES = _acct("6140", "Utilities Expense")
SERVICES_REVENUE = _acct("4010", "Services Revenue", "REVENUE")
EXPENSE_COA = [GENERAL, SALARIES, SUPPLIES, UTILITIES]


def _install_repo(monkeypatch, by_query):
    """Stub the account repository's search + COA fetch (offline)."""

    async def fake_search(organization_id, *, query, limit=50):
        return list(by_query.get(query, []))

    async def fake_coa(organization_id, *, account_type=None,
                       is_active=True, limit=500):
        return list(EXPENSE_COA)

    monkeypatch.setattr(account_repository, "search_accounts", fake_search)
    monkeypatch.setattr(account_repository, "get_chart_of_accounts", fake_coa)


class TestAccountResolutionByNature:
    """_search_account_by_nature must map expenses to the RIGHT account —
    never to whichever category account the search hits first."""

    @pytest.mark.asyncio
    async def test_consumable_maps_to_supplies_account(self, monkeypatch):
        _install_repo(monkeypatch, {"supplies": [SUPPLIES]})
        acc = await _search_account_by_nature(
            "org", OPERATING_EXPENSE, "printer toner"
        )
        assert acc["code"] == "6100"

    @pytest.mark.asyncio
    async def test_rule_item_maps_to_its_category(self, monkeypatch):
        _install_repo(monkeypatch, {"utilities": [UTILITIES]})
        acc = await _search_account_by_nature(
            "org", OPERATING_EXPENSE, "office electricity bill"
        )
        assert acc["code"] == "6140"

    @pytest.mark.asyncio
    async def test_unmapped_item_falls_back_to_general_default(
        self, monkeypatch
    ):
        # "utilities" would match Utilities first — an unmapped item must
        # skip the category scan and reach the general default instead.
        _install_repo(monkeypatch, {"utilities": [UTILITIES]})
        acc = await _search_account_by_nature(
            "org", OPERATING_EXPENSE, "miscellaneous widget"
        )
        assert acc["code"] == "6000"

    @pytest.mark.asyncio
    async def test_service_never_matches_revenue_account(self, monkeypatch):
        _install_repo(monkeypatch, {"service": [SERVICES_REVENUE]})
        acc = await _search_account_by_nature(
            "org", SERVICE, "software consultant for development services"
        )
        assert acc is not None
        assert acc["account_type"] == "EXPENSE"

    @pytest.mark.asyncio
    async def test_item_name_still_wins_over_category(self, monkeypatch):
        _install_repo(monkeypatch, {
            "utilities": [UTILITIES], "office supplies": [SUPPLIES],
        })
        acc = await _search_account_by_nature(
            "org", OPERATING_EXPENSE, "office stationery pack"
        )
        assert acc["code"] == "6100"



# ---------------------------------------------------------------------------
# Classification rules — the authority hierarchy's deterministic layer
# ---------------------------------------------------------------------------


class TestInventoryRules:
    def test_resale_is_inventory(self):
        nature, conf = rule_based_nature(
            "keyboards", {"item_description": "100 keyboards for resale", "quantity": 100}
        )
        assert nature == INVENTORY
        assert conf == "HIGH"

    def test_for_stock_is_inventory(self):
        nature, _ = rule_based_nature("bought goods for stock", {})
        assert nature == INVENTORY

    def test_bulk_durable_lean_inventory(self):
        nature, conf = rule_based_nature(
            "laptops", {"item_description": "laptops", "quantity": 25}
        )
        assert nature == INVENTORY
        assert conf == "MEDIUM"


class TestOperatingExpenseRules:
    def test_electricity_is_expense_never_asset(self):
        nature, conf = rule_based_nature("office electricity bill", {})
        assert nature == OPERATING_EXPENSE
        assert conf == "HIGH"

    def test_rent_is_expense(self):
        nature, _ = rule_based_nature("monthly office rent", {})
        assert nature == OPERATING_EXPENSE

    def test_salaries_are_expense(self):
        nature, _ = rule_based_nature("staff salaries", {})
        assert nature == OPERATING_EXPENSE


class TestConsumableRules:
    def test_toner_is_expense(self):
        # Consumables reuse the expense treatment — no asset-vs-expense ask.
        nature, conf = rule_based_nature("printer toner for the office", {})
        assert nature == OPERATING_EXPENSE
        assert conf == "HIGH"


class TestServiceRules:
    def test_consultant_is_service(self):
        nature, _ = rule_based_nature("software consultant for development services", {})
        assert nature == SERVICE


class TestPrepaymentRules:
    def test_rent_advance_is_prepayment(self):
        nature, _ = rule_based_nature("advance to the landlord for next month's rent", {})
        assert nature == "PREPAYMENT"

    def test_deposit_is_deposit_advance(self):
        nature, _ = rule_based_nature("security deposit for the warehouse", {})
        assert nature == DEPOSIT_ADVANCE


class TestDurableGoodsAmbiguity:
    def test_laptop_without_configuration_is_materially_ambiguous(self):
        # A laptop MAY be a fixed asset or an expense — the rule engine must
        # NOT guess.  nature=None + LOW confidence signals the agent to ask.
        nature, conf = rule_based_nature("laptop", {"item_description": "laptop"})
        assert nature is None
        assert conf == "LOW"

    def test_computer_is_materially_ambiguous(self):
        nature, _ = rule_based_nature("desktop computer", {})
        assert nature is None


class TestUnknownItems:
    def test_unknown_item_defaults_to_expense_without_asking(self):
        # Everyday unmapped items take the conservative expense default —
        # the agent must NOT turn every purchase into a questionnaire.
        nature, conf = rule_based_nature("mystery gadget", {})
        assert nature == OPERATING_EXPENSE
        assert conf == "MEDIUM"


# ---------------------------------------------------------------------------
# Semantic error normalisation — generic, no per-tool special cases
# ---------------------------------------------------------------------------


class TestMissingRequiredValue:
    def test_postgres_dict_repr_not_null(self):
        raw = (
            "{'message': 'null value in column \"credit_limit\" of relation "
            "\"suppliers\" violates not-null constraint', 'code': '23502', "
            "'details': 'Failing row contains ...'}"
        )
        n = normalize_error(raw, operation="create_supplier")
        assert n["category"] == MISSING_REQUIRED_VALUE
        assert n["recoverable"] is True
        assert n["requires_user_input"] is True
        assert n["field"] == "credit_limit"
        assert n["entity"] == "supplier"

    def test_postgres_plain_text_not_null(self):
        raw = 'null value in column "credit_limit" of relation "suppliers" violates not-null constraint'
        n = normalize_error(raw, operation="create_supplier")
        assert n["category"] == MISSING_REQUIRED_VALUE
        assert n["field"] == "credit_limit"
        assert n["entity"] == "supplier"

    def test_question_builder(self):
        q = build_missing_field_question("supplier", "credit_limit", "XYZ Computers")
        assert "credit limit" in q.lower()
        assert "XYZ Computers" in q


class TestDuplicateRecord:
    def test_unique_violation(self):
        raw = (
            "{'message': 'duplicate key value violates unique constraint "
            "\"suppliers_organization_id_name_key\"', 'code': '23505', 'details': null}"
        )
        n = normalize_error(raw, operation="create_supplier")
        assert n["category"] == DUPLICATE_RECORD
        assert n["recoverable"] is True
        # Duplicates are NOT user-input problems — the model must search
        # and reuse the existing authoritative record.
        assert n["requires_user_input"] is False

    def test_duplicate_guidance(self):
        g = build_duplicate_guidance("supplier")
        assert "search" in g.lower()
        assert "reuse" in g.lower()


class TestInvalidReference:
    def test_fk_violation(self):
        raw = (
            'insert or update on table "purchase_bills" violates foreign key '
            'constraint "purchase_bills_supplier_id_fkey"'
        )
        n = normalize_error(raw, operation="create_purchase_bill")
        assert n["category"] == INVALID_REFERENCE
        assert n["requires_user_input"] is True


class TestInfrastructureError:
    def test_connection_failure_is_not_user_input(self):
        n = normalize_error("httpx.ConnectError: [WinError 10061] connection refused")
        assert n["category"] == INFRASTRUCTURE_ERROR
        assert n["requires_user_input"] is False
        assert n["recoverable"] is False

    def test_timeout_is_not_user_input(self):
        n = normalize_error("httpx.ReadTimeout: connection timed out reaching the database")
        assert n["category"] == INFRASTRUCTURE_ERROR


class TestUnknownError:
    def test_unknown_never_pauses_for_input(self):
        n = normalize_error("something completely inexplicable happened")
        # An unrecognised failure must never be disguised as a user-input
        # problem (it stays UNKNOWN or becomes a validation error only with
        # clear signals).
        if n["category"] == "UNKNOWN_ERROR":
            assert n["requires_user_input"] is False


class TestNoArbitraryDefaultAccount:
    """LIVE DEFECT: a chair purchase was debited to 6010 Salaries because
    the fallback returned whichever EXPENSE account sorted first.  The
    fallback must accept ONLY a genuinely GENERIC account."""

    @pytest.mark.asyncio
    async def test_no_generic_account_means_none_not_first_account(self, monkeypatch):
        _install_repo(monkeypatch, {})  # nature-term searches find nothing
        async def fake_coa(organization_id, *, account_type=None,
                           is_active=True, limit=500):
            return [SALARIES, UTILITIES]  # no generic account exists
        monkeypatch.setattr(account_repository, "get_chart_of_accounts", fake_coa)
        acc = await _search_account_by_nature("org", OPERATING_EXPENSE, "chair purchase")
        assert acc is None  # NEVER accounts[0]

    @pytest.mark.asyncio
    async def test_generic_account_is_used_when_present(self, monkeypatch):
        generic = _acct("6000", "General Operating Expense")
        _install_repo(monkeypatch, {})
        async def fake_coa(organization_id, *, account_type=None,
                           is_active=True, limit=500):
            return [SALARIES, UTILITIES, generic]
        monkeypatch.setattr(account_repository, "get_chart_of_accounts", fake_coa)
        acc = await _search_account_by_nature("org", OPERATING_EXPENSE, "chair purchase")
        assert acc is not None and acc["code"] == "6000"

