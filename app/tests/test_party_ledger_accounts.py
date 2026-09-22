"""Party sub-ledgers: one receivable/payable account per party.

Production evidence this pins (org "Zameer Labs PVT Ltd", 2026-09-22):

    JV-000020  Dr 1100 Accounts Receivable   50,000.01
    JV-000021  Dr 1100 Accounts Receivable   60,000.00

Two different customers, the SAME account - so the chart could not answer "what
does each party owe?" even though the revenue side already had a dedicated child
ledger ("4001 chairs Sales").  ``customers.receivable_account_id`` /
``suppliers.payable_account_id`` existed and the payment flows honoured them;
nothing ever created or set them.

Rules pinned here
-----------------
* a party account is ALWAYS a child of the control account (never a new root),
  so the reporting hierarchy keeps rolling up;
* the code ladder is deterministic: (a) parent series sub-code -> 1100-0001,
  (b) numeric series -> 1101..., (c) generated fallback;
* an existing party account is REUSED, never duplicated; a linked party is left
  alone;
* resolution during posting is READ-ONLY - no chart growth as a side effect;
* every query carries the caller's organization_id.

No live provider, no live database (the DB layer is stubbed).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest

from app.services import party_ledger_service as svc

ORG = uuid.UUID("4f20f43c-bb3b-4747-abe1-02d9380884df")
CUST = uuid.UUID("11111111-1111-1111-1111-111111111111")
SUPP = uuid.UUID("22222222-2222-2222-2222-222222222222")
AR = "33333333-3333-3333-3333-333333333333"
AR_PARTY = "44444444-4444-4444-4444-444444444444"
AP = "55555555-5555-5555-5555-555555555555"
AP_PARTY = "66666666-6666-6666-6666-666666666666"
CASH = "77777777-7777-7777-7777-777777777777"
REV = "88888888-8888-8888-8888-888888888888"
EXP = "99999999-9999-9999-9999-999999999999"


def _ar_control() -> Dict[str, Any]:
    return {
        "id": AR,
        "code": "1100",
        "name": "Accounts Receivable",
        "account_type": "ASSET",
        "normal_balance": "DEBIT",
        "parent_account_id": None,
        "is_active": True,
    }


def _ap_control() -> Dict[str, Any]:
    return {
        "id": AP,
        "code": "2010",
        "name": "Accounts Payable",
        "account_type": "LIABILITY",
        "normal_balance": "CREDIT",
        "parent_account_id": None,
        "is_active": True,
    }


class _World:
    """Minimal stand-in for the chart-of-accounts DB layer."""

    def __init__(self, *, chart: List[Dict[str, Any]], taken_codes=()):
        self.chart = chart
        self.taken_codes = set(taken_codes)
        self.calls: List[tuple] = []
        self.created: List[Dict[str, Any]] = []

    async def get_chart_of_accounts(self, organization_id, *, account_type=None,
                                    limit=500):
        self.calls.append(("get_chart_of_accounts", str(organization_id), account_type))
        return [row for row in self.chart if row.get("account_type") == account_type]

    async def get_account_by_code(self, organization_id, *, code):
        self.calls.append(("get_account_by_code", str(organization_id), code))
        if code in {row.get("code") for row in self.chart} or code in self.taken_codes:
            return {"id": "existing-%s" % code, "code": code}
        return None

    async def get_account(self, organization_id, *, account_id):
        self.calls.append(("get_account", str(organization_id), str(account_id)))
        for row in self.chart:
            if str(row.get("id")) == str(account_id):
                return row
        return None

    async def next_available_code(self, organization_id, requested_code, **kw):
        self.calls.append(("next_available_code", str(organization_id), requested_code))
        return "GENERATED-FOR-%s" % requested_code

    async def create_account(self, **kwargs):
        self.calls.append(("create_account", str(kwargs.get("organization_id")),
                           kwargs.get("code")))
        self.created.append(kwargs)
        return {
            "id": "created-1",
            "code": kwargs.get("code"),
            "name": kwargs.get("name"),
            "account_type": kwargs.get("account_type"),
            "normal_balance": kwargs.get("normal_balance"),
            "parent_account_id": kwargs.get("parent_account_id"),
            "is_active": True,
        }


def _patch_repo(monkeypatch, world: _World) -> None:
    for name in (
        "get_chart_of_accounts", "get_account_by_code", "get_account",
        "next_available_code", "create_account",
    ):
        monkeypatch.setattr(svc.a_repo, name, getattr(world, name))



# ---------------------------------------------------------------------------
# 1. The code ladder: (a) parent series -> (b) numeric series -> (c) generated
# ---------------------------------------------------------------------------


class TestCodeLadder:
    @pytest.mark.asyncio
    async def test_a_sub_code_in_the_control_series(self, monkeypatch):
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)

        code = await svc.allocate_code(ORG, kind=svc.RECEIVABLE, parent_code="1100")

        assert code == "1100-0001"

    @pytest.mark.asyncio
    async def test_a_skips_sub_codes_that_are_taken(self, monkeypatch):
        world = _World(chart=[_ar_control()], taken_codes={"1100-0001"})
        _patch_repo(monkeypatch, world)

        code = await svc.allocate_code(ORG, kind=svc.RECEIVABLE, parent_code="1100")

        assert code == "1100-0002"

    @pytest.mark.asyncio
    async def test_b_falls_through_to_the_numeric_series(self, monkeypatch):
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)

        async def taken(organization_id, *, code):
            # every sub-code of the parent series is in use
            return {"id": "x", "code": code} if code.startswith("1100-") else None

        monkeypatch.setattr(svc.a_repo, "get_account_by_code", taken)

        code = await svc.allocate_code(ORG, kind=svc.RECEIVABLE, parent_code="1100")

        assert code == "GENERATED-FOR-1101"  # next_available_code(1100 + 1)
        assert world.calls[-1] == ("next_available_code", str(ORG), "1101")

    @pytest.mark.asyncio
    async def test_c_generated_fallback_when_the_control_code_is_not_numeric(
        self, monkeypatch
    ):
        world = _World(chart=[])
        _patch_repo(monkeypatch, world)

        async def taken(organization_id, *, code):
            return {"id": "x", "code": code} if "-" in code else None

        monkeypatch.setattr(svc.a_repo, "get_account_by_code", taken)

        code = await svc.allocate_code(ORG, kind=svc.RECEIVABLE, parent_code="RECV")

        assert code == "GENERATED-FOR-1100-0001"
        assert world.calls[-1] == ("next_available_code", str(ORG), "1100-0001")

    @pytest.mark.asyncio
    async def test_c_generated_fallback_without_a_control_account(self, monkeypatch):
        world = _World(chart=[])
        _patch_repo(monkeypatch, world)

        code = await svc.allocate_code(ORG, kind=svc.PAYABLE, parent_code=None)

        assert code == "GENERATED-FOR-2010-0001"



# ---------------------------------------------------------------------------
# 2. Ensuring the account: created once, reused after that, never duplicated
# ---------------------------------------------------------------------------


class TestEnsurePartyAccount:
    @pytest.mark.asyncio
    async def test_a_new_party_gets_a_dedicated_child_account(self, monkeypatch):
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)

        account, created = await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="ABC Furnitures"
        )

        assert created is True
        assert len(world.created) == 1
        kwargs = world.created[0]
        assert kwargs["code"] == "1100-0001"
        assert kwargs["name"] == "ABC Furnitures"
        assert kwargs["account_type"] == "ASSET"
        assert kwargs["normal_balance"] == "DEBIT"
        assert kwargs["parent_account_id"] == uuid.UUID(AR)  # a CHILD of the control
        assert kwargs["is_control_account"] is False
        assert account["id"] == "created-1"

    @pytest.mark.asyncio
    async def test_the_party_name_is_normalised(self, monkeypatch):
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)

        await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="  ABC   Furnitures "
        )

        assert world.created[0]["name"] == "ABC Furnitures"

    @pytest.mark.asyncio
    async def test_an_existing_party_account_is_reused_never_duplicated(
        self, monkeypatch
    ):
        existing = {
            "id": AR_PARTY, "code": "1100-0007", "name": "ABC Furnitures",
            "account_type": "ASSET", "parent_account_id": AR, "is_active": True,
        }
        world = _World(chart=[_ar_control(), existing])
        _patch_repo(monkeypatch, world)

        async def must_not_create(**kwargs):
            raise AssertionError("a duplicate account must never be created")

        monkeypatch.setattr(svc.a_repo, "create_account", must_not_create)

        account, created = await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="ABC Furnitures"
        )

        assert created is False
        assert account["id"] == AR_PARTY

    @pytest.mark.asyncio
    async def test_a_party_that_already_carries_an_account_is_left_alone(
        self, monkeypatch
    ):
        existing = {
            "id": AR_PARTY, "code": "1100-0007", "name": "ABC Furnitures",
            "account_type": "ASSET", "parent_account_id": AR, "is_active": True,
        }
        world = _World(chart=[_ar_control(), existing])
        _patch_repo(monkeypatch, world)

        async def must_not_create(**kwargs):
            raise AssertionError("a linked party must not get a second account")

        monkeypatch.setattr(svc.a_repo, "create_account", must_not_create)

        account, created = await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="ABC Furnitures",
            existing_account_id=AR_PARTY,
        )

        assert created is False and account["id"] == AR_PARTY

    @pytest.mark.asyncio
    async def test_without_a_control_account_nothing_is_created(self, monkeypatch):
        """No heading to segregate under -> leave the party unlinked."""
        world = _World(chart=[])
        _patch_repo(monkeypatch, world)

        async def must_not_create(**kwargs):
            raise AssertionError("no root account may be invented")

        monkeypatch.setattr(svc.a_repo, "create_account", must_not_create)

        account, created = await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="ABC Furnitures"
        )

        assert (account, created) == (None, False)

    @pytest.mark.asyncio
    async def test_the_concurrency_loser_recovers_the_winner(self, monkeypatch):
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)
        winner = {
            "id": AR_PARTY, "code": "1100-0001", "name": "ABC Furnitures",
            "account_type": "ASSET", "parent_account_id": AR, "is_active": True,
        }

        async def racing_create(**kwargs):
            world.chart.append(winner)  # the other request won the insert
            raise RuntimeError("duplicate key value violates unique constraint")

        monkeypatch.setattr(svc.a_repo, "create_account", racing_create)

        account, created = await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="ABC Furnitures"
        )

        assert created is False and account["id"] == AR_PARTY

    @pytest.mark.asyncio
    async def test_a_supplier_gets_a_payable_child_of_the_ap_control(self, monkeypatch):
        world = _World(chart=[_ap_control()])
        _patch_repo(monkeypatch, world)

        await svc.ensure_party_account(
            ORG, kind=svc.PAYABLE, party_name="FurnishMart"
        )

        kwargs = world.created[0]
        assert kwargs["parent_account_id"] == uuid.UUID(AP)
        assert kwargs["account_type"] == "LIABILITY"
        assert kwargs["normal_balance"] == "CREDIT"
        assert kwargs["code"] == "2010-0001"

    @pytest.mark.asyncio
    async def test_every_query_carries_the_caller_organization(self, monkeypatch):
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)

        await svc.ensure_party_account(
            ORG, kind=svc.RECEIVABLE, party_name="ABC Furnitures"
        )

        assert world.calls, "the chart must have been read"
        for call in world.calls:
            assert call[1] == str(ORG), call


# ---------------------------------------------------------------------------
# 3. Resolution during posting is READ-ONLY
# ---------------------------------------------------------------------------


class TestResolutionIsReadOnly:
    @pytest.mark.asyncio
    async def test_the_linked_party_account_is_returned(self, monkeypatch):
        world = _World(chart=[{
            "id": AR_PARTY, "code": "1100-0007", "name": "ABC Furnitures",
            "account_type": "ASSET", "parent_account_id": AR, "is_active": True,
        }])
        _patch_repo(monkeypatch, world)

        async def fake_fetch_one(table, *, filters, select="*"):
            assert filters["organization_id"] == str(ORG)
            return {"id": str(CUST), "receivable_account_id": AR_PARTY}

        monkeypatch.setattr(svc, "fetch_one", fake_fetch_one)

        account = await svc.resolve_customer_receivable_account(ORG, CUST)

        assert account["id"] == AR_PARTY

    @pytest.mark.asyncio
    async def test_resolution_never_creates_or_updates(self, monkeypatch):
        world = _World(chart=[{
            "id": AR_PARTY, "code": "1100-0007", "name": "ABC Furnitures",
            "account_type": "ASSET", "parent_account_id": AR, "is_active": True,
        }])
        _patch_repo(monkeypatch, world)

        async def must_not_write(**kwargs):
            raise AssertionError("resolution must never write")

        monkeypatch.setattr(svc.a_repo, "create_account", must_not_write)
        monkeypatch.setattr(svc, "_link_party", must_not_write)

        async def fake_fetch_one(table, *, filters, select="*"):
            return {"id": str(CUST), "receivable_account_id": AR_PARTY}

        monkeypatch.setattr(svc, "fetch_one", fake_fetch_one)

        account = await svc.resolve_customer_receivable_account(ORG, CUST)

        assert account["id"] == AR_PARTY

    @pytest.mark.asyncio
    async def test_a_party_without_a_link_resolves_to_none(self, monkeypatch):
        world = _World(chart=[])
        _patch_repo(monkeypatch, world)

        async def fake_fetch_one(table, *, filters, select="*"):
            return {"id": str(CUST), "receivable_account_id": None}

        monkeypatch.setattr(svc, "fetch_one", fake_fetch_one)

        assert await svc.resolve_customer_receivable_account(ORG, CUST) is None

    @pytest.mark.asyncio
    async def test_a_missing_party_row_is_not_an_error(self, monkeypatch):
        world = _World(chart=[])
        _patch_repo(monkeypatch, world)



# ---------------------------------------------------------------------------
# 4. The creation hooks: adding a party provisions its ledger
# ---------------------------------------------------------------------------


class TestCreationHooks:
    @pytest.mark.asyncio
    async def test_customer_creation_links_the_dedicated_account(self, monkeypatch):
        from app.services import customer_service
        from app.repositories import customer_repository

        async def fake_search(organization_id, *, query, limit=25):
            return []

        async def fake_create(**kwargs):
            return {"id": str(CUST), "name": kwargs["name"]}

        linked: List[tuple] = []

        async def fake_link(organization_id, *, customer_id, account_id):
            linked.append((str(customer_id), str(account_id)))
            return {"id": str(customer_id)}

        async def fake_ensure(organization_id, customer):
            return ({"id": AR_PARTY, "code": "1100-0001", "name": customer["name"]}, True)

        monkeypatch.setattr(customer_repository, "search_customers", fake_search)
        monkeypatch.setattr(customer_repository, "create_customer", fake_create)
        monkeypatch.setattr(customer_repository, "set_receivable_account", fake_link)
        monkeypatch.setattr(svc, "ensure_customer_receivable_account", fake_ensure)

        result = await customer_service.create(ORG, name="ABC Furnitures")

        assert linked == [(str(CUST), AR_PARTY)]
        assert result["receivable_account_id"] == AR_PARTY
        assert result["receivable_account_code"] == "1100-0001"
        assert result["receivable_account_created"] is True

    @pytest.mark.asyncio
    async def test_a_ledger_failure_never_loses_the_customer(self, monkeypatch):
        from app.services import customer_service
        from app.repositories import customer_repository

        async def fake_search(organization_id, *, query, limit=25):
            return []

        async def fake_create(**kwargs):
            return {"id": str(CUST), "name": kwargs["name"]}

        async def boom(organization_id, customer):
            raise RuntimeError("chart service unavailable")

        monkeypatch.setattr(customer_repository, "search_customers", fake_search)
        monkeypatch.setattr(customer_repository, "create_customer", fake_create)
        monkeypatch.setattr(svc, "ensure_customer_receivable_account", boom)

        result = await customer_service.create(ORG, name="ABC Furnitures")

        assert result["id"] == str(CUST)
        assert "receivable_account_id" not in result

    @pytest.mark.asyncio
    async def test_a_reused_customer_still_gets_its_ledger(self, monkeypatch):
        from app.services import customer_service
        from app.repositories import customer_repository

        async def fake_search(organization_id, *, query, limit=25):
            return [{"id": str(CUST), "name": "ABC Furnitures"}]

        linked: List[tuple] = []

        async def fake_link(organization_id, *, customer_id, account_id):
            linked.append((str(customer_id), str(account_id)))

        async def fake_ensure(organization_id, customer):
            return ({"id": AR_PARTY, "code": "1100-0001", "name": "ABC Furnitures"}, True)

        monkeypatch.setattr(customer_repository, "search_customers", fake_search)
        monkeypatch.setattr(customer_repository, "set_receivable_account", fake_link)
        monkeypatch.setattr(svc, "ensure_customer_receivable_account", fake_ensure)

        result = await customer_service.create(ORG, name="ABC Furnitures")

        assert result["reused"] is True
        assert linked == [(str(CUST), AR_PARTY)]

    @pytest.mark.asyncio
    async def test_supplier_creation_links_the_dedicated_account(self, monkeypatch):
        from app.services import supplier_service
        from app.repositories import supplier_repository

        async def fake_search(organization_id, *, query, limit=25):
            return []

        async def fake_create(**kwargs):
            return {"id": str(SUPP), "name": kwargs["name"]}

        linked: List[tuple] = []

        async def fake_link(organization_id, *, supplier_id, account_id):
            linked.append((str(supplier_id), str(account_id)))

        async def fake_ensure(organization_id, supplier):
            return ({"id": AP_PARTY, "code": "2010-0001", "name": supplier["name"]}, True)

        monkeypatch.setattr(supplier_repository, "search_suppliers", fake_search)
        monkeypatch.setattr(supplier_repository, "create_supplier", fake_create)
        monkeypatch.setattr(supplier_repository, "set_payable_account", fake_link)


# ---------------------------------------------------------------------------
# 5. What the ENGINE posts: the party's account, not the shared control account
# ---------------------------------------------------------------------------


class TestPostingUsesThePartyAccount:
    @staticmethod
    def _patch_engine(monkeypatch, *, receivable=None, payable=None, recorded=None):
        import app.accounting_engine as engine

        async def fake_resolve_customer(organization_id, customer_id):
            return receivable

        async def fake_resolve_supplier(organization_id, supplier_id):
            return payable

        monkeypatch.setattr(
            svc, "resolve_customer_receivable_account", fake_resolve_customer
        )
        monkeypatch.setattr(
            svc, "resolve_supplier_payable_account", fake_resolve_supplier
        )

        accounts = {
            "ASSET": {"id": CASH, "name": "Cash", "code": "1000"},
            "REVENUE": {"id": REV, "name": "Sales", "code": "4000"},
            "EXPENSE": {"id": EXP, "name": "Office Expense", "code": "6100"},
            "LIABILITY": {"id": AP, "name": "Accounts Payable", "code": "2010"},
        }

        async def fake_default(organization_id, account_type, **kwargs):
            return accounts.get(account_type)

        async def fake_control(organization_id, *, account_name=None, **kwargs):
            if account_name == "Accounts Receivable":
                return {"id": AR, "name": "Accounts Receivable", "code": "1100"}
            return None

        monkeypatch.setattr(engine, "_resolve_default_account", fake_default)
        monkeypatch.setattr(
            engine.accounting_service, "resolve_account", fake_control
        )

        async def fake_credit_sale(**kwargs):
            if recorded is not None:
                recorded["credit_sale"] = kwargs
            return {"id": "je-1"}

        async def fake_credit_purchase(**kwargs):
            if recorded is not None:
                recorded["credit_purchase"] = kwargs
            return {"id": "je-2"}

        monkeypatch.setattr(engine, "record_credit_sale", fake_credit_sale)
        monkeypatch.setattr(engine, "record_credit_purchase", fake_credit_purchase)
        return engine

    @pytest.mark.asyncio
    async def test_invoice_debits_the_customers_own_receivable(self, monkeypatch):
        recorded: Dict[str, Any] = {}
        engine = self._patch_engine(
            monkeypatch,
            receivable={"id": AR_PARTY, "name": "ABC Furnitures", "code": "1100-0001"},
            recorded=recorded,
        )

        await engine.auto_journal(
            organization_id=ORG,
            document_type="invoice",
            document={"id": str(uuid.uuid4())},
            amount=50000.01,
            transaction_date="2026-09-22",
            description="Invoice INV-000006",
            customer_id=CUST,
        )

        assert recorded["credit_sale"]["receivable_account_id"] == uuid.UUID(AR_PARTY)
        assert recorded["credit_sale"]["customer_id"] == CUST

    @pytest.mark.asyncio
    async def test_invoice_falls_back_to_the_control_account(self, monkeypatch):
        recorded: Dict[str, Any] = {}
        engine = self._patch_engine(monkeypatch, receivable=None, recorded=recorded)

        await engine.auto_journal(
            organization_id=ORG,
            document_type="invoice",
            document={"id": str(uuid.uuid4())},
            amount=100,
            transaction_date="2026-09-22",
            description="Invoice INV-000007",
            customer_id=CUST,
        )

        assert recorded["credit_sale"]["receivable_account_id"] == uuid.UUID(AR)

    @pytest.mark.asyncio
    async def test_purchase_bill_credits_the_suppliers_own_payable(self, monkeypatch):
        recorded: Dict[str, Any] = {}
        engine = self._patch_engine(
            monkeypatch,
            payable={"id": AP_PARTY, "name": "FurnishMart", "code": "2010-0001"},
            recorded=recorded,
        )

        await engine.auto_journal(
            organization_id=ORG,
            document_type="purchase_bill",
            document={"id": str(uuid.uuid4())},
            amount=49000,
            transaction_date="2026-09-22",
            description="Bill PB-000001",
            supplier_id=SUPP,
        )

        assert recorded["credit_purchase"]["payable_account_id"] == uuid.UUID(AP_PARTY)

    @pytest.mark.asyncio
    async def test_purchase_bill_falls_back_to_the_control_account(self, monkeypatch):
        recorded: Dict[str, Any] = {}
        engine = self._patch_engine(monkeypatch, payable=None, recorded=recorded)

        await engine.auto_journal(
            organization_id=ORG,
            document_type="purchase_bill",
            document={"id": str(uuid.uuid4())},
            amount=49000,
            transaction_date="2026-09-22",
            description="Bill PB-000002",
            supplier_id=SUPP,
        )

        assert recorded["credit_purchase"]["payable_account_id"] == uuid.UUID(AP)

    @pytest.mark.asyncio
    async def test_the_dry_run_plan_allocates_a_distinct_code_per_party(
        self, monkeypatch
    ):
        """The report must read like the real run: 1100-0001, 1100-0002, ..."""
        world = _World(chart=[_ar_control()])
        _patch_repo(monkeypatch, world)

        async def fake_fetch_many(table, *, filters, select, limit=1000):
            assert table == "customers"
            return [
                {"id": str(CUST), "organization_id": str(ORG),
                 "name": "ABC Furnitures", "receivable_account_id": None},
                {"id": str(SUPP), "organization_id": str(ORG),
                 "name": "FDS Labs Pvt", "receivable_account_id": None},
                {"id": "33333333-3333-3333-3333-333333333334",
                 "organization_id": str(ORG), "name": "xyz tech",
                 "receivable_account_id": None},
            ]

        monkeypatch.setattr(svc, "fetch_many", fake_fetch_many)
        monkeypatch.setattr(svc, "_PARTY_TABLES", (("customers", "receivable_account_id",
                                                    svc.RECEIVABLE),))

        report = await svc.backfill_missing_accounts(apply=False)

        assert [p["code"] for p in report["planned"]] == [
            "1100-0001", "1100-0002", "1100-0003",
        ]
        assert report["created"] == [] and world.created == []  # nothing written


class TestControlAccountStaysPostable:
    """Once parties hang under 1100/2010 those accounts become HEADINGS.

    ``_resolve_default_account`` deliberately refuses to return a heading (a
    heading must never carry a posting), so the engine must resolve the control
    account BY NAME for the fallback - otherwise a supplier-less bill would lose
    its payable the moment the backfill ran.
    """

    @staticmethod
    def _patch(monkeypatch, *, default_returns_none: bool):
        import app.accounting_engine as engine

        recorded: Dict[str, Any] = {}

        async def fake_default(organization_id, account_type, **kwargs):
            # A heading is excluded from default resolution -> this is what the
            # resolver returns for LIABILITY once 2010 has children.
            if account_type == "EXPENSE":
                return {"id": EXP, "name": "Office Expense", "code": "6100"}
            if account_type == "LIABILITY":
                return None if default_returns_none else {
                    "id": AP, "name": "Accounts Payable", "code": "2010"
                }
            return None

        async def fake_control_by_name(organization_id, *, account_name=None, **kw):
            if account_name == "Accounts Payable":
                return {"id": AP, "name": "Accounts Payable", "code": "2010"}
            return None

        async def fake_credit_purchase(**kwargs):
            recorded["credit_purchase"] = kwargs
            return {"id": "je-2"}

        monkeypatch.setattr(engine, "_resolve_default_account", fake_default)
        monkeypatch.setattr(
            engine.accounting_service, "resolve_account", fake_control_by_name
        )
        monkeypatch.setattr(engine, "record_credit_purchase", fake_credit_purchase)
        return engine, recorded

    @pytest.mark.asyncio
    async def test_a_supplier_less_bill_still_credits_the_ap_control(self, monkeypatch):
        engine, recorded = self._patch(monkeypatch, default_returns_none=True)

        result = await engine.auto_journal(
            organization_id=ORG,
            document_type="purchase_bill",
            document={"id": str(uuid.uuid4())},
            amount=49000,
            transaction_date="2026-09-22",
            description="Bill PB-000003 (no supplier)",
        )

        assert "journal_warning" not in result, result
        assert recorded["credit_purchase"]["payable_account_id"] == uuid.UUID(AP)

    @pytest.mark.asyncio
    async def test_a_deactivated_party_account_is_ignored(self, monkeypatch):
        """An archived party ledger is not a posting target - fall back to 1100."""
        world = _World(chart=[{
            "id": AR_PARTY, "code": "1100-0007", "name": "ABC Furnitures",
            "account_type": "ASSET", "parent_account_id": AR, "is_active": False,
        }])
        _patch_repo(monkeypatch, world)

        async def fake_fetch_one(table, *, filters, select="*"):
            return {"id": str(CUST), "receivable_account_id": AR_PARTY}

        monkeypatch.setattr(svc, "fetch_one", fake_fetch_one)

        assert await svc.resolve_customer_receivable_account(ORG, CUST) is None

