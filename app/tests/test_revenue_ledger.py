"""
Revenue-ledger flexibility tests.

Pins the behaviour that REPLACED the silent "first REVENUE account in the
chart" default — the defect that credited a mobile-phone sale to
*Software Development Revenue* purely because that account sorted first.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import revenue_ledger_service as rls

ORG = uuid.uuid4()


class _FakeRepo:
    """Minimal stand-in for app.repositories.account_repository."""

    def __init__(self, accounts, next_code="4011"):
        self.accounts = list(accounts)
        self.next_code = next_code
        self.created = []

    async def get_chart_of_accounts(
        self, organization_id, *, account_type=None, limit=500, is_active=True
    ):
        rows = self.accounts
        if account_type:
            rows = [r for r in rows if r.get("account_type") == account_type]
        return rows[:limit]

    async def next_available_code(
        self, organization_id, requested_code, *, max_probes=200
    ):
        return self.next_code

    async def create_account(self, **kw):
        row = {
            "id": str(uuid.uuid4()),
            "code": kw["code"],
            "name": kw["name"],
            "account_type": kw["account_type"],
            "normal_balance": kw.get("normal_balance"),
            "parent_account_id": kw.get("parent_account_id"),
            "description": kw.get("description"),
        }
        self.accounts.append(row)
        self.created.append(row)
        return row


def _acct(code, name, parent=None):
    return {
        "id": str(uuid.uuid4()),
        "code": code,
        "name": name,
        "account_type": "REVENUE",
        "normal_balance": "CREDIT",
        "parent_account_id": parent,
    }


@pytest.fixture
def repo(monkeypatch):
    fake = _FakeRepo([
        _acct("4000", "Operating Revenue"),
        _acct("4010", "Software Development Revenue"),
        _acct("4020", "Consulting Revenue"),
    ])
    monkeypatch.setattr(rls, "a_repo", fake)
    return fake


class TestStreamLabel:
    def test_document_boilerplate_is_stripped(self):
        assert rls.stream_label("Sale of Mobile PHONE") == "Mobile PHONE"
        assert rls.stream_label("Invoice for Chairs") == "Chairs"

    def test_quantity_noise_is_stripped(self):
        assert rls.stream_label("2 tables") == "tables"

    @pytest.mark.parametrize(
        "value",
        ["", "   ", None, "something", "goods", "item", "items", "stuff", "service"],
    )
    def test_generic_never_becomes_a_ledger(self, value):
        assert rls.stream_label(value) is None

    def test_multiline_uses_first_line_only(self):
        # An attached document's item list must not become one giant name.
        assert rls.stream_label("Chairs\nBeds\nTables") == "Chairs"

    def test_long_text_is_capped(self):
        label = rls.stream_label("x" * 200)
        assert label is not None and len(label) <= 60


def test_suggest_ledger_name():
    assert rls.suggest_ledger_name("Chairs") == "Chairs Sales"
    assert rls.suggest_ledger_name("Mobile PHONE") == "Mobile PHONE Sales"


class TestResolution:
    @pytest.mark.asyncio
    async def test_named_stream_without_ledger_is_ambiguous(self, repo):
        """The exact defect: a phone sale must NOT silently take a revenue
        account just because one happens to exist."""
        account, label, reason = await rls.resolve_revenue_account(
            ORG, "Sale of Mobile PHONE"
        )
        assert account is None
        assert reason == "ambiguous"
        assert label == "Mobile PHONE"

    @pytest.mark.asyncio
    async def test_existing_stream_ledger_is_used(self, repo):
        repo.accounts.append(_acct("4011", "Mobile PHONE Sales"))
        account, _, reason = await rls.resolve_revenue_account(
            ORG, "Sale of Mobile PHONE"
        )
        assert account is not None
        assert account["name"] == "Mobile PHONE Sales"
        assert reason == "stream"

    @pytest.mark.asyncio
    async def test_single_revenue_account_is_unambiguous(self, monkeypatch):
        monkeypatch.setattr(rls, "a_repo", _FakeRepo([_acct("4000", "Revenue")]))
        account, _, reason = await rls.resolve_revenue_account(ORG, "Chairs")
        assert account is not None and reason == "only-one"

    @pytest.mark.asyncio
    async def test_find_stream_ledger_is_exact_not_fuzzy(self, repo):
        # A partial name must never satisfy the lookup.
        assert await rls.find_stream_ledger(ORG, "Phone") is None
        repo.accounts.append(_acct("4011", "Mobile PHONE Sales"))
        assert await rls.find_stream_ledger(ORG, "Mobile PHONE") is not None


class TestNeedsReview:
    @pytest.mark.asyncio
    async def test_asks_when_stream_unknown_and_choice_matters(self, repo):
        assert await rls.needs_review(ORG, "Chairs", None) is True

    @pytest.mark.asyncio
    async def test_never_nags_once_answered(self, repo):
        assert await rls.needs_review(ORG, "Chairs", "CREATE") is False

    @pytest.mark.asyncio
    async def test_never_asks_for_a_generic_item(self, repo):
        assert await rls.needs_review(ORG, "something", None) is False

    @pytest.mark.asyncio
    async def test_single_revenue_account_does_not_ask(self, monkeypatch):
        monkeypatch.setattr(rls, "a_repo", _FakeRepo([_acct("4000", "Revenue")]))
        assert await rls.needs_review(ORG, "Chairs", None) is False


class TestCreation:
    @pytest.mark.asyncio
    async def test_create_hangs_the_ledger_under_revenue(self, repo):
        account, created = await rls.create_stream_ledger(ORG, "Chairs")
        assert created is True
        assert account["name"] == "Chairs Sales"
        assert account["account_type"] == "REVENUE"
        assert account["normal_balance"] == "CREDIT"
        # A CHILD of the revenue parent, not a new root — so reporting rolls up.
        # (The repository stringifies the parent id, as its signature requires.)
        parent = next(a for a in repo.accounts if a["code"] == "4000")
        assert str(account["parent_account_id"]) == parent["id"]

    @pytest.mark.asyncio
    async def test_creation_is_idempotent(self, repo):
        first, created1 = await rls.create_stream_ledger(ORG, "Chairs")
        second, created2 = await rls.create_stream_ledger(ORG, "Chairs")
        assert created1 is True and created2 is False
        assert first["id"] == second["id"]
        assert len(repo.created) == 1


class TestApplyDecision:
    @pytest.mark.asyncio
    async def test_create_decision_creates_the_ledger(self, repo):
        account, created = await rls.apply_decision(ORG, "Chairs", "CREATE")
        assert created is True and account["name"] == "Chairs Sales"

    @pytest.mark.asyncio
    async def test_use_existing_decision_takes_general_revenue(self, repo):
        account, created = await rls.apply_decision(ORG, "Chairs", "USE_EXISTING")
        assert created is False and account["name"] == "Operating Revenue"

    @pytest.mark.asyncio
    async def test_unknown_decision_resolves_nothing(self, repo):
        account, created = await rls.apply_decision(ORG, "Chairs", None)
        assert account is None and created is False

    @pytest.mark.asyncio
    async def test_create_without_a_label_resolves_nothing(self, repo):
        account, created = await rls.apply_decision(ORG, None, "CREATE")
        assert account is None and created is False