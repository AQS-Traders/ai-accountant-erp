"""Work Stream S3 — LLM-primary accounting reasoning (architecture tests).

These tests pin the ARCHITECTURAL correction described in
``docs/LLM_PRIMARY_REASONING_ARCHITECTURE.md``:

* the LLM is the primary accounting reasoning layer; Python is the controlled
  execution/enforcement layer;
* Python's deterministic extraction is LITERAL-ONLY and explicitly labelled
  PRELIMINARY (never a decided treatment);
* the LLM inspects the LIVE BOOKS through a closed, permission-checked,
  organization-scoped read-only evidence catalog before it proposes anything;
* evidence results are returned to the model labelled LIVE BOOKS EVIDENCE and
  the model reassesses the request against them;
* the model — not a keyword route — decides between asking a contextual
  question, proposing a preparatory/correcting transaction, proposing a
  settlement/mutation, refusing, or reporting completion;
* a proposal that is incomplete, names an unoffered tool, or touches a
  prohibited tool is REFUSED by Python and fed back to the model (never
  silently repaired into a hardcoded route);
* provider unavailability degrades to the previous deterministic behaviour —
  never to an invented decision;
* the same reasoning model applies to every accounting area (assets, payables,
  receivables, expenses, journals, periods, policies, statements) — the
  evidence catalog is not fixed-asset specific.

The LLM is simulated with a scripted orchestrator: that is deliberate. These
tests pin the CONTRACT (labels, closed sets, enforcement, degradation), not a
live model's behaviour.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.accounting_reasoning as ar
import app.agent as agent_mod
import app.books_evidence as be
from app.models.schemas import ExecutionStatus

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")

MOTOR_BIKE = "record sale of fixed asset motor bike on cash for 56000"
PAID_ABC = "paid 50,000 to ABC"


class _NoChainSettings:
    """Settings stub: standard provider chain, generous budgets."""

    accounting_reasoning_timeout = 30.0
    accounting_reasoning_total_timeout = 45.0
    accounting_reasoning_model_chain = ""


class ScriptedOrchestrator:
    """Returns scripted responses; picks the script matching the prompt.

    The reasoning stage and the semantic stage share one provider interface in
    production, so the stub distinguishes them by prompt content exactly the
    way the real pipeline does.
    """

    def __init__(self, reasoning=None, semantic=None, boom=False):
        self.reasoning = list(reasoning or [])
        self.semantic = semantic
        self.boom = boom
        self.prompts = []
        #: How many provider round-trips each interface received — lets a test
        #: prove that a failed reasoning round is NOT retried twice more.
        self.text_calls = 0
        self.tools_calls = 0

    async def generate_text(self, *, prompt: str = ""):
        self.prompts.append(prompt)
        self.text_calls += 1
        if self.boom:
            raise RuntimeError("provider down")
        if "PRIMARY ACCOUNTING REASONING LAYER" in prompt:
            if self.reasoning:
                item = self.reasoning.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item if isinstance(item, str) else json.dumps(item)
            return "no decision"
        if self.semantic is not None:
            return self.semantic if isinstance(self.semantic, str) else json.dumps(self.semantic)
        return "no facts"

    async def generate_with_tools(
        self,
        *,
        user_message: str = "",
        context=None,
        max_tool_iterations: int = 10,
        executor=None,
        light_budget: bool = False,
        excluded_tools=None,
    ):
        """The agent's planning/execution provider interface.

        The accounting reasoning stage never uses this path (it calls
        ``generate_text``), but the agent's planning call does — so a provider
        outage has to surface here as well.
        """
        self.prompts.append(user_message)
        self.tools_calls += 1
        if self.boom:
            raise RuntimeError("provider down")
        return {"text": "", "tool_calls": []}


def _evidence_prompt_index(orchestrator) -> int:
    """Index of the prompt that carried live books evidence (or -1)."""
    for idx, prompt in enumerate(orchestrator.prompts):
        if be.EVIDENCE_LABEL in prompt:
            return idx
    return -1


def _fake_kind(loader, *, kind="fake_kind", permission_slug="search_account", args=None):
    """A stand-in evidence kind for the tests.

    The default argument map mirrors the arguments these tests actually send,
    so a scripted request is exercised through the REAL validation path rather
    than being rejected for an argument name the fake kind forgot to declare.
    """
    return be.EvidenceKind(
        kind=kind,
        title=f"{kind} title",
        description=f"{kind} description",
        args=args
        if args is not None
        else {
            "terms": "list",
            "party_name": "string",
            "party_role": "string",
            "limit": "number",
        },
        permission_slug=permission_slug,
        loader=loader,
    )



# ---------------------------------------------------------------------------
# A. The evidence layer — a CLOSED, permission-checked, bounded read surface
# ---------------------------------------------------------------------------


class EvidenceRequestLike(be.EvidenceRequest):
    """EvidenceRequest with positional convenience for the tests."""

    def __init__(self, kind, args=None):
        super().__init__(kind=kind, why="test", args=args or {})


class TestEvidenceCatalog:
    def test_catalog_covers_every_accounting_area(self):
        """Not a fixed-asset special case: assets, liabilities, equity,
        revenue, expenses, subledgers, journals, periods and policies all
        have an evidence area."""
        kinds = set(be.evidence_kind_names())
        for required in (
            "chart_of_accounts",
            "journal_entries",
            "documents",
            "open_receivables",
            "open_payables",
            "fixed_assets",
            "catalog",
            "bank_accounts",
            "periods",
            "policies",
            "prior_transactions",
            "reports",
            "parties",
            "ledgers",
            "account_search",
        ):
            assert required in kinds, f"missing evidence area: {required}"

    def test_every_kind_is_gated_by_a_registered_read_only_tool(self):
        from app.tools import get_handler

        for name in be.evidence_kind_names():
            spec = be.EVIDENCE_KINDS[name]
            entry = get_handler(spec.permission_slug)
            assert entry is not None, f"{name} is gated by an unknown tool"
            assert entry["read_only"] is True, (
                f"{name} must be gated by a READ-ONLY tool, got "
                f"{spec.permission_slug}"
            )

    def test_unknown_kind_is_rejected(self):
        problem = be.validate_evidence_request(EvidenceRequestLike("select_all"))
        assert problem and "Unknown evidence kind" in problem

    def test_unknown_argument_is_rejected(self):
        problem = be.validate_evidence_request(
            EvidenceRequestLike("fixed_assets", {"sql": "select 1"})
        )
        assert problem and "not accepted" in problem

    def test_organization_scope_can_never_come_from_the_model(self):
        """The organization id is injected by Python, so any attempt to pass
        one (or a tenant selector of any kind) is refused outright."""
        for attempt in (
            {"organization_id": str(ORG)},
            {"org_id": str(ORG)},
            {"tenant": "other"},
        ):
            problem = be.validate_evidence_request(
                EvidenceRequestLike("open_payables", attempt)
            )
            assert problem, f"tenant selector accepted: {attempt}"

    def test_argument_types_are_enforced(self):
        assert be.validate_evidence_request(
            EvidenceRequestLike("fixed_assets", {"terms": 12})
        )
        assert be.validate_evidence_request(
            EvidenceRequestLike("periods", {"limit": "many"})
        )
        assert be.validate_evidence_request(
            EvidenceRequestLike("fixed_assets", {"terms": ["motor bike", 7]})
        ) is None



class TestEvidenceExecution:
    @pytest.mark.asyncio
    async def test_permission_denial_is_returned_as_evidence(self, monkeypatch):
        called = {"n": 0}

        async def loader(organization_id, **kw):
            called["n"] += 1
            return [{"id": "1"}]

        monkeypatch.setitem(
            be.EVIDENCE_KINDS,
            "journal_entries",
            _fake_kind(
                loader, kind="journal_entries", permission_slug="get_general_ledger"
            ),
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=False)
        )
        results = await be.gather_evidence(
            [EvidenceRequestLike("journal_entries")],
            organization_id=ORG,
            auth=SimpleNamespace(role_code="VIEWER"),
        )
        assert len(results) == 1
        assert results[0].rejected is True
        assert "Permission denied" in (results[0].error or "")
        assert called["n"] == 0, "a denied lookup must not read the books"

    @pytest.mark.asyncio
    async def test_loader_failure_is_data_not_an_exception(self, monkeypatch):
        async def broken(organization_id, **kw):
            raise RuntimeError("db down")

        async def fine(organization_id, **kw):
            return [{"id": "ok"}]

        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "fixed_assets", _fake_kind(broken, kind="fixed_assets")
        )
        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "documents", _fake_kind(fine, kind="documents")
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        results = await be.gather_evidence(
            [EvidenceRequestLike("fixed_assets"), EvidenceRequestLike("documents")],
            organization_id=ORG,
            auth=SimpleNamespace(role_code="OWNER"),
        )
        by_kind = {r.kind: r for r in results}
        assert by_kind["fixed_assets"].ok is False
        assert "db down" in (by_kind["fixed_assets"].error or "")
        assert by_kind["documents"].records == [{"id": "ok"}]

    @pytest.mark.asyncio
    async def test_results_are_bounded_and_empty_is_information(self, monkeypatch):
        async def many(organization_id, **kw):
            return [{"id": str(i)} for i in range(50)]

        async def none(organization_id, **kw):
            return []

        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "documents", _fake_kind(many, kind="documents")
        )
        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "ledgers", _fake_kind(none, kind="ledgers")
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        results = await be.gather_evidence(
            [EvidenceRequestLike("documents"), EvidenceRequestLike("ledgers")],
            organization_id=ORG,
            auth=SimpleNamespace(role_code="OWNER"),
        )
        by_kind = {r.kind: r for r in results}
        assert len(by_kind["documents"].records) == be.MAX_RECORDS_PER_KIND
        assert by_kind["documents"].truncated is True
        assert by_kind["ledgers"].empty is True
        assert by_kind["ledgers"].error is None

    @pytest.mark.asyncio
    async def test_render_labels_evidence_and_marks_emptiness(self, monkeypatch):
        async def none(organization_id, **kw):
            return []

        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "fixed_assets", _fake_kind(none, kind="fixed_assets")
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        results = await be.gather_evidence(
            [EvidenceRequestLike("fixed_assets", {"terms": ["motor bike"]})],
            organization_id=ORG,
            auth=SimpleNamespace(role_code="OWNER"),
        )
        rendered = be.render_evidence(results)
        assert rendered.startswith(be.EVIDENCE_LABEL)
        assert "EMPTY" in rendered
        assert "do not assume the record exists" in rendered


# ---------------------------------------------------------------------------
# B. Preliminary extraction — literals only, never a decided treatment
# ---------------------------------------------------------------------------


class TestPreliminaryExtraction:
    def test_literals_are_captured_and_labelled_preliminary(self):
        pre = ar.preliminary_extraction(MOTOR_BIKE)
        assert pre["label"] == (
            "PRELIMINARY EXTRACTION — may be corrected after accounting review"
        )
        assert pre["original_request"] == MOTOR_BIKE
        assert pre["literals"]["amount"] == 56000.0
        assert pre["literals"]["payment_channel"] == "CASH"
        assert "fixed_assets" in pre["candidate_subject_areas"]

    def test_extraction_never_asserts_a_treatment(self):
        """The only hint about meaning is explicitly a HINT, and the note tells
        the model the records decide.  No account, no nature, no workflow."""
        pre = ar.preliminary_extraction(PAID_ABC)
        assert isinstance(pre["provisional_intent_hint"], (str, type(None)))
        assert "Provisional only" in pre["note"]
        assert "accounts" not in pre["literals"]
        assert "transaction_nature" not in pre["literals"]


# ---------------------------------------------------------------------------
# C. The reasoning loop
# ---------------------------------------------------------------------------


class TestReasoningLoop:
    def _facts(self, message=PAID_ABC):
        return ar.ReasoningFacts(
            user_request=message,
            preliminary=ar.preliminary_extraction(message),
            today="2026-09-19",
        )

    @pytest.mark.asyncio
    async def test_requests_evidence_then_reassesses_and_proposes(self, monkeypatch):
        """Round 1: the model asks for the payables.  Python validates, reads
        the books, and returns them labelled LIVE BOOKS EVIDENCE.  Round 2: the
        model reassesses and proposes the settlement."""

        async def loader(organization_id, **kw):
            return [
                {
                    "bill_number": "PB-0007",
                    "supplier_name": "ABC Traders",
                    "total": 50000.0,
                    "amount_paid": 0.0,
                    "status": "OPEN",
                }
            ]

        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "open_payables", _fake_kind(loader, kind="open_payables")
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        orchestrator = ScriptedOrchestrator(
            reasoning=[
                {
                    "evidence_requests": [
                        {
                            "kind": "open_payables",
                            "why": "is ABC owed?",
                            "args": {"party_name": "ABC"},
                        }
                    ]
                },
                {
                    "understanding": {
                        "economic_event": "settlement of an existing payable",
                        "what_user_wants": "clear ABC's bill",
                        "event_type": "settlement",
                    },
                    "proposal": {
                        "interpretation": (
                            "This settles the open bill PB-0007 for ABC Traders"
                        ),
                        "affected_records": ["purchase_bill PB-0007", "cash/bank"],
                        "accounting_impact": [
                            {"account": "Trade Payables", "debit": 50000},
                            {"account": "Cash/Bank", "credit": 50000},
                        ],
                        "not_affected": ["no new expense is recognised"],
                        "unresolved_uncertainty": ["which bank account was used"],
                        "tools": [
                            {
                                "tool_name": "record_supplier_payment",
                                "arguments": {"supplier_name": "ABC", "amount": 50000},
                            }
                        ],
                        "confirmation": "Record the 50,000 payment settling PB-0007?",
                    },
                },
            ]
        )
        outcome = await ar.run_reasoning_loop(
            self._facts(),
            organization_id=ORG,
            auth=SimpleNamespace(role_code="OWNER"),
            orchestrator=orchestrator,
            offered_tools=["record_supplier_payment", "record_expense"],
        )
        assert outcome.status == ar.PROPOSAL
        assert [c["tool_name"] for c in ar.proposed_mutation_tools(outcome)] == [
            "record_supplier_payment"
        ]
        # The reassessment really did happen ON the books: the second prompt
        # carried the labelled evidence and the model's proposal cites the
        # record that was read.
        assert _evidence_prompt_index(orchestrator) == 1
        assert "PB-0007" in orchestrator.prompts[1]
        assert be.EVIDENCE_LABEL in orchestrator.prompts[1]
        assert outcome.evidence_results[0].records[0]["bill_number"] == "PB-0007"

    @pytest.mark.asyncio
    async def test_same_phrase_different_books_changes_the_answer(self, monkeypatch):
        """'paid 50,000 to ABC' is NOT routed by the word 'paid': with an open
        payable the model settles it; with none it asks what the money was."""
        async def with_payable(organization_id, **kw):
            return [{"bill_number": "PB-1", "total": 50000.0, "status": "OPEN"}]

        async def without_payable(organization_id, **kw):
            return []

        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        monkeypatch.setitem(
            be.EVIDENCE_KINDS,
            "open_payables",
            _fake_kind(with_payable, kind="open_payables"),
        )
        settle = ScriptedOrchestrator(
            reasoning=[
                {
                    "evidence_requests": [
                        {"kind": "open_payables", "args": {"party_name": "ABC"}}
                    ]
                },
                {
                    "proposal": {
                        "interpretation": "settlement of PB-1",
                        "affected_records": ["PB-1"],
                        "accounting_impact": [
                            {"account": "Trade Payables", "debit": 50000}
                        ],
                        "not_affected": ["no new expense"],
                        "unresolved_uncertainty": [],
                        "tools": [
                            {
                                "tool_name": "record_supplier_payment",
                                "arguments": {"amount": 50000},
                            }
                        ],
                        "confirmation": "Record the payment against PB-1?",
                    }
                },
            ]
        )
        out1 = await ar.run_reasoning_loop(
            self._facts(),
            organization_id=ORG,
            orchestrator=settle,
            offered_tools=["record_supplier_payment"],
        )
        assert out1.status == ar.PROPOSAL

        monkeypatch.setitem(
            be.EVIDENCE_KINDS,
            "open_payables",
            _fake_kind(without_payable, kind="open_payables"),
        )
        ask = ScriptedOrchestrator(
            reasoning=[
                {
                    "evidence_requests": [
                        {"kind": "open_payables", "args": {"party_name": "ABC"}}
                    ]
                },
                {
                    "question": {
                        "text": (
                            "I found no open bill for ABC. Was this money an "
                            "advance, a loan repayment, an owner withdrawal, or "
                            "payment for a new purchase?"
                        ),
                        "options": [
                            "Advance",
                            "Loan repayment",
                            "Owner drawing",
                            "New purchase",
                        ],
                    },
                    "missing_material_facts": [
                        {
                            "fact": "nature_of_payment",
                            "why_material": "decides the accounts",
                        }
                    ],
                },
            ]
        )
        out2 = await ar.run_reasoning_loop(
            self._facts(),
            organization_id=ORG,
            orchestrator=ask,
            offered_tools=["record_supplier_payment"],
        )
        assert out2.status == ar.NEEDS_INPUT
        assert "no open bill" in out2.question["text"]
        assert ar.proposed_mutation_tools(out2) == []

        # The two models saw different LIVE BOOKS EVIDENCE — the differing
        # answer came from the differing records, not from the verb "paid".
        assert _evidence_prompt_index(settle) >= 0
        assert _evidence_prompt_index(ask) >= 0
        assert "PB-1" in settle.prompts[_evidence_prompt_index(settle)]
        assert "PB-1" not in ask.prompts[_evidence_prompt_index(ask)]

    @pytest.mark.asyncio
    async def test_absent_fixed_asset_yields_a_question_not_a_disposal(
        self, monkeypatch
    ):
        """The motor-bike case: the asset is NOT registered, so the model asks
        for the acquisition facts instead of pretending it exists — and no
        disposal tool is ever proposed."""

        async def no_assets(organization_id, **kw):
            return []

        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "fixed_assets", _fake_kind(no_assets, kind="fixed_assets")
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        orchestrator = ScriptedOrchestrator(
            reasoning=[
                {
                    "evidence_requests": [
                        {"kind": "fixed_assets", "args": {"terms": ["motor bike"]}}
                    ]
                },
                {
                    "understanding": {
                        "economic_event": (
                            "possible disposal of an unregistered asset"
                        ),
                        "event_type": "disposal",
                    },
                    "question": {
                        "text": (
                            "I could not find this motor bike in the fixed-asset "
                            "register. Is 56,000 the sale proceeds or the original "
                            "acquisition value, and what were the acquisition cost "
                            "and date?"
                        )
                    },
                    "missing_material_facts": [
                        {
                            "fact": "acquisition_cost",
                            "why_material": (
                                "no book value can be computed without it"
                            ),
                        },
                        {
                            "fact": "proceeds_vs_cost",
                            "why_material": (
                                "decides gain/loss versus recognition"
                            ),
                        },
                    ],
                },
            ]
        )
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(
                user_request=MOTOR_BIKE,
                preliminary=ar.preliminary_extraction(MOTOR_BIKE),
                today="2026-09-19",
            ),
            organization_id=ORG,
            orchestrator=orchestrator,
            offered_tools=["dispose_fixed_asset", "register_fixed_asset"],
        )
        assert outcome.status == ar.NEEDS_INPUT
        assert "56,000" in outcome.question["text"]
        assert ar.proposed_mutation_tools(outcome) == []
        assert outcome.evidence_results[0].empty is True

    @pytest.mark.asyncio
    async def test_incomplete_proposal_is_refused_never_executed(self):
        """A proposal that hides the mandatory disclosures is refused by Python;
        with the round budget exhausted nothing is executed."""
        orch = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "record something",
                        "tools": [
                            {"tool_name": "record_expense", "arguments": {"amount": 5000}}
                        ],
                    }
                }
            ]
        )
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request="record internet expense of 5,000"),
            organization_id=ORG,
            orchestrator=orch,
            offered_tools=["record_expense"],
            max_rounds=1,
        )
        assert outcome.status == ar.UNSUPPORTED
        assert outcome.violations
        assert any("accounting_impact" in v for v in outcome.violations)
        assert len(orch.prompts) == 1  # budget exhausted after the first round

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_empty_disclosure_list_is_a_statement_not_an_omission(self):
        """Presence, not non-emptiness: 'no unresolved uncertainty' is a claim
        the model is allowed to make.  Omitting the field is silence, and
        silence is refused."""
        filled = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "record a new internet expense",
                        "affected_records": ["Internet Expense"],
                        "accounting_impact": [{"account": "Internet Expense", "debit": 5000}],
                        "not_affected": [],
                        "unresolved_uncertainty": [],
                        "tools": [
                            {"tool_name": "record_expense", "arguments": {"amount": 5000}}
                        ],
                        "confirmation": "Record the 5,000 internet expense?",
                    }
                }
            ]
        )
        accepted = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request="record internet expense of 5,000"),
            organization_id=ORG,
            orchestrator=filled,
            offered_tools=["record_expense"],
            max_rounds=1,
        )
        assert accepted.status == ar.PROPOSAL
        assert accepted.violations == []
        assert accepted.stated_disclosures and set(accepted.stated_disclosures) == set(
            ar.REQUIRED_PROPOSAL_FIELDS
        )

        silent = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "record a new internet expense",
                        "affected_records": ["Internet Expense"],
                        "accounting_impact": [{"account": "Internet Expense", "debit": 5000}],
                        "tools": [
                            {"tool_name": "record_expense", "arguments": {"amount": 5000}}
                        ],
                        "confirmation": "Record the 5,000 internet expense?",
                    }
                }
            ]
        )
        refused = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request="record internet expense of 5,000"),
            organization_id=ORG,
            orchestrator=silent,
            offered_tools=["record_expense"],
            max_rounds=1,
        )
        assert refused.status == ar.UNSUPPORTED
        assert any("not_affected" in v for v in refused.violations)
        assert any("unresolved_uncertainty" in v for v in refused.violations)

    @pytest.mark.asyncio
    async def test_round_timeout_is_attempted_and_bounded(self):
        """The per-round cap must be enforced AND reported as a provider
        ATTEMPT (so the caller never retries the same chain twice more)."""
        import asyncio as _asyncio

        class _Slow:
            async def generate_text(self, *, prompt: str = ""):
                await _asyncio.sleep(5)
                return "{}"

        started = _asyncio.get_running_loop().time()
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=_Slow(),
            timeout_seconds=0.3,
        )
        elapsed = _asyncio.get_running_loop().time() - started
        assert outcome.provider_failed is True
        assert outcome.provider_attempted is True
        assert outcome.rounds == 1
        assert elapsed < 2.0, "the round cap was not enforced"
        assert ar.proposed_mutation_tools(outcome) == []

    @pytest.mark.asyncio
    async def test_total_budget_caps_every_round_not_just_one(self, monkeypatch):
        """Three slow rounds must not add up: the stage has a TOTAL budget."""
        import asyncio as _asyncio

        class _Slow:
            async def generate_text(self, *, prompt: str = ""):
                await _asyncio.sleep(0.25)
                # Always ask for more evidence, so the loop wants 3 rounds.
                return json.dumps(
                    {"evidence_requests": [{"kind": "chart_of_accounts"}]}
                )

        class _Settings:
            accounting_reasoning_timeout = 0.2
            accounting_reasoning_total_timeout = 0.4

        monkeypatch.setattr("app.config.get_settings", lambda: _Settings())
        started = _asyncio.get_running_loop().time()
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=_Slow(),
        )
        elapsed = _asyncio.get_running_loop().time() - started
        assert outcome.provider_failed is True
        assert outcome.rounds < ar.MAX_REASONING_ROUNDS, (
            "the loop kept spending rounds after the total budget was gone"
        )
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_configured_model_chain_is_passed_to_the_provider(self, monkeypatch):
        """The reasoning round may name a faster model than the standard chain
        (a thinking model dominates the latency of this stage)."""
        seen = {}

        class _Provider:
            async def generate_text(self, *, prompt: str = "", model_chain=None):
                seen["chain"] = model_chain
                return json.dumps({"question": {"text": "Which period?"}})

        class _Settings:
            accounting_reasoning_timeout = 5.0
            accounting_reasoning_total_timeout = 10.0
            accounting_reasoning_model_chain = "qwen-max, qwen3.6-flash"

        monkeypatch.setattr("app.config.get_settings", lambda: _Settings())
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=_Provider(),
        )
        assert outcome.status == ar.NEEDS_INPUT
        assert seen["chain"] == ["qwen-max", "qwen3.6-flash"]

    @pytest.mark.asyncio
    async def test_default_chain_is_not_overridden(self, monkeypatch):
        """With no dedicated chain configured, the provider contract is the
        plain ``prompt`` kwarg (so any orchestrator keeps working)."""
        seen = {}

        class _Provider:
            async def generate_text(self, *, prompt: str = ""):
                seen["kwargs"] = "prompt-only"
                return json.dumps({"question": {"text": "Which period?"}})

        monkeypatch.setattr("app.config.get_settings", lambda: _NoChainSettings())
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=_Provider(),
        )
        assert outcome.status == ar.NEEDS_INPUT
        assert seen["kwargs"] == "prompt-only"

    @pytest.mark.asyncio
    async def test_provider_without_chain_support_still_works(self, monkeypatch):
        """A dedicated chain is only sent to providers that declare it — any
        orchestrator (or double) with the plain ``prompt`` contract keeps
        working, so the default can be a fast model without breaking callers."""
        class _Provider:
            async def generate_text(self, *, prompt: str = ""):
                return json.dumps({"question": {"text": "Which period?"}})

        class _ChainSettings:
            accounting_reasoning_timeout = 5.0
            accounting_reasoning_total_timeout = 10.0
            accounting_reasoning_model_chain = "qwen-max"

        monkeypatch.setattr("app.config.get_settings", lambda: _ChainSettings())
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=_Provider(),
        )
        assert outcome.status == ar.NEEDS_INPUT

    @pytest.mark.asyncio
    async def test_refusal_is_fed_back_when_a_round_remains(self):
        orch = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "record something",
                        "tools": [{"tool_name": "record_expense", "arguments": {}}],
                    }
                },
                {"question": {"text": "Which expense account should I use?"}},
            ]
        )
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request="record internet expense of 5,000"),
            organization_id=ORG,
            orchestrator=orch,
            offered_tools=["record_expense"],
            max_rounds=2,
        )
        assert outcome.status == ar.NEEDS_INPUT
        assert "REJECTED BY THE EXECUTION LAYER" in orch.prompts[1]
        assert "accounting_impact" in orch.prompts[1]

    @pytest.mark.asyncio
    async def test_unoffered_tool_is_refused(self):
        orch = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "post a journal",
                        "affected_records": ["GL"],
                        "accounting_impact": [{"account": "Cash", "debit": 1}],
                        "not_affected": ["nothing else"],
                        "unresolved_uncertainty": [],
                        "tools": [{"tool_name": "post_journal", "arguments": {}}],
                        "confirmation": "Post it?",
                    }
                }
            ]
        )
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request="record an adjustment for 25,000"),
            organization_id=ORG,
            orchestrator=orch,
            offered_tools=["record_expense"],
            max_rounds=1,
        )
        assert outcome.status == ar.UNSUPPORTED
        assert any("not in the offered tool list" in v for v in outcome.violations)

    @pytest.mark.asyncio
    async def test_prohibited_tool_is_refused_even_if_offered(self):
        orch = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "create the party",
                        "affected_records": ["customer ledger"],
                        "accounting_impact": [],
                        "not_affected": [],
                        "unresolved_uncertainty": [],
                        "tools": [{"tool_name": "create_customer", "arguments": {}}],
                        "confirmation": "Create the customer?",
                    }
                }
            ]
        )
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request="record cash sale of chairs to Bilal"),
            organization_id=ORG,
            orchestrator=orch,
            offered_tools=["create_customer", "record_cash_sale"],
            forbidden_tools=["create_customer"],
            max_rounds=1,
        )
        assert outcome.status == ar.UNSUPPORTED
        assert any("prohibited" in v for v in outcome.violations)

    @pytest.mark.asyncio
    async def test_unknown_evidence_kind_rejection_is_fed_back(self):
        orch = ScriptedOrchestrator(
            reasoning=[
                {"evidence_requests": [{"kind": "read_everything"}]},
                {"question": {"text": "Which supplier is this for?"}},
            ]
        )
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=orch,
            max_rounds=2,
        )
        assert outcome.status == ar.NEEDS_INPUT
        assert "Unknown evidence kind" in orch.prompts[1]
        assert be.EVIDENCE_LABEL not in orch.prompts[1]

    @pytest.mark.asyncio
    async def test_unparseable_output_degrades_to_no_decision(self):
        orch = ScriptedOrchestrator(reasoning=["I think you should pay ABC."])
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=orch,
        )
        assert outcome.provider_failed is True
        assert outcome.usable is False

    @pytest.mark.asyncio
    async def test_provider_failure_degrades_to_no_decision(self):
        orch = ScriptedOrchestrator(boom=True)
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=orch,
        )
        assert outcome.provider_failed is True
        assert outcome.proposal is None

    @pytest.mark.asyncio
    async def test_no_orchestrator_degrades(self):
        outcome = await ar.run_reasoning_loop(
            ar.ReasoningFacts(user_request=PAID_ABC),
            organization_id=ORG,
            orchestrator=None,
        )
        assert outcome.provider_failed is True

        assert ar.proposed_mutation_tools(outcome) == []
        # With no model there is nothing to decide WHAT to inspect — the loop
        # must not invent a lookup of its own.
        assert outcome.evidence_results == []
        assert outcome.evidence_requests == []


# ---------------------------------------------------------------------------
# D. Prompt contract + provisional planning
# ---------------------------------------------------------------------------


class TestPromptContract:
    def test_system_instructions_declare_the_llm_primary(self):
        from app.prompts import (
            build_primary_reasoning_instructions,
            build_system_instructions,
        )

        text = build_primary_reasoning_instructions()
        assert "primary accounting reasoning layer" in text
        assert "preliminary planner may be incomplete or wrong" in text
        assert "reclassify the event" in text
        assert "Do not invent missing facts" in text
        assert "Ask questions based on the actual records" in text
        assert text in build_system_instructions()

    def test_reasoning_prompt_stays_within_its_latency_budget(self):
        """The reasoning prompt is the PIPELINE'S SLOWEST call, and its size is
        what the per-round budget has to cover. A prompt that quietly grows
        re-introduces the timeout that silently disabled the reasoning layer."""
        from app.tools import list_tools

        facts = ar.ReasoningFacts(
            user_request=MOTOR_BIKE,
            preliminary=ar.preliminary_extraction(MOTOR_BIKE),
        )
        prompt = ar.build_reasoning_prompt(
            facts, offered_tools=list(list_tools())
        )
        # Measured baseline after the latency fix: ~10.9 KB. The ceiling leaves
        # little headroom on purpose — see the "Why the budget must exceed the
        # provider's real latency" note in the architecture doc.
        assert len(prompt) < 11500, f"reasoning prompt grew to {len(prompt)} chars"
        for marker in (
            "PRIMARY ACCOUNTING REASONING LAYER",
            "EVIDENCE CATALOG",
            "OFFERED TOOLS",
            "HARD PROHIBITIONS",
            "RESPONSE FORMAT",
        ):
            assert marker in prompt

    def test_user_content_labels_evidence_and_preliminary(self):
        from app.models.schemas import AgentContext
        from app.prompts import build_user_content

        context = AgentContext(
            organization={"name": "Acme"},
            user={"id": str(USER)},
            preliminary_extraction=ar.preliminary_extraction(PAID_ABC),
            live_evidence=[
                {
                    "kind": "open_payables",
                    "ok": True,
                    "empty": False,
                    "records": [{"bill_number": "PB-1", "total": 50000.0}],
                },
                {"kind": "ledgers", "ok": True, "empty": True, "records": []},
            ],
            accounting_reasoning={
                "status": "PROPOSAL",
                "understanding": {"economic_event": "settlement of a payable"},
                "proposal": {
                    "interpretation": "settles PB-1",
                    "accounting_impact": [
                        {"account": "Trade Payables", "debit": 50000}
                    ],
                    "not_affected": ["no new expense"],
                    "unresolved_uncertainty": ["bank account unknown"],
                },
            },
        )
        content = build_user_content(PAID_ABC, context)
        assert (
            "PRELIMINARY EXTRACTION — may be corrected after accounting review"
            in content
        )
        assert "LIVE BOOKS EVIDENCE — use this to reassess the request" in content
        assert "PB-1" in content
        assert "EMPTY — no matching record exists" in content
        assert "LLM ACCOUNTING REASONING" in content
        assert "settles PB-1" in content

    def test_llm_interpretation_overrides_the_keyword_route(self):
        """The planner's job is provisional: a grounded LLM intent is honoured
        instead of the keyword trigger."""
        from app.planner import plan

        plan_keyword = plan(PAID_ABC)
        plan_llm = plan(PAID_ABC, prefill_entities={"semantic_intent": "record_expense"})
        assert plan_llm.intent == "record_expense"
        assert plan_llm.intent != plan_keyword.intent


# ---------------------------------------------------------------------------
# E. Agent wiring — the reasoning stage drives the response it produced
# ---------------------------------------------------------------------------


def _stub_context():
    """Attribute bag mirroring AgentContext for the deterministic path."""
    return SimpleNamespace(
        relevant_customers=[],
        relevant_suppliers=[],
        relevant_accounts=[],
        relevant_bank_accounts=[],
        extracted_entities={},
        classification=None,
        economic_event=None,
        impact_map={},
        prohibited_actions=[],
        preliminary_extraction={},
        live_evidence=[],
        accounting_reasoning=None,
    )


@pytest.fixture
def boundary(monkeypatch):
    """Only the process boundaries are stubbed — the REAL planner, reasoning
    loop and confirmation gate run."""
    session = {
        "id": str(uuid.uuid4()),
        "organization_id": str(ORG),
        "user_id": str(USER),
        "user_request": "",
        "conversation_id": "conv-s3",
        "status": "PENDING",
        "current_phase": "RECEIVED",
    }
    clarifications, confirmations, tool_runs = [], [], []

    async def fake_create_session(**kw):
        session["user_request"] = kw.get("user_message", "")
        return session

    async def fake_create_clarification(**kw):
        clarifications.append(kw)
        return {"id": f"clar-{len(clarifications)}", **kw}

    async def fake_create_confirmation(**kw):
        confirmations.append(kw)
        return {"id": f"conf-{len(confirmations)}", **kw}

    async def fake_execute_planned(tool_calls, executor):
        for tc in tool_calls:
            tool_runs.append((tc.tool_name, dict(tc.arguments or {})))
        return [{"success": True, "data": {}} for _ in tool_calls]

    async def fake_classify(**kw):
        from app.classifier import TransactionClassification

        return TransactionClassification(
            transaction_nature=(kw.get("entities") or {}).get("transaction_nature"),
            confidence="HIGH",
            source="DETERMINISTIC_RULE",
            requires_clarification=False,
        )

    monkeypatch.setattr(agent_mod, "create_execution_session", fake_create_session)
    monkeypatch.setattr(agent_mod, "_close_superseded_sessions", AsyncMock())
    monkeypatch.setattr(agent_mod, "seed_clarification_history", AsyncMock())
    monkeypatch.setattr(agent_mod, "_load_org_preferences", AsyncMock(return_value={}))
    monkeypatch.setattr(agent_mod, "_log_step", AsyncMock())
    monkeypatch.setattr(agent_mod, "_update_status", AsyncMock())
    monkeypatch.setattr(agent_mod, "flush_step_logs", AsyncMock())
    monkeypatch.setattr(
        agent_mod, "build_context", AsyncMock(return_value=_stub_context())
    )
    monkeypatch.setattr(agent_mod, "create_clarification", fake_create_clarification)
    monkeypatch.setattr(agent_mod, "create_confirmation", fake_create_confirmation)
    monkeypatch.setattr(agent_mod, "execute_planned_tool_calls", fake_execute_planned)
    monkeypatch.setattr(
        agent_mod, "verify_journal", AsyncMock(return_value={"verified": True})
    )
    monkeypatch.setattr(
        agent_mod, "create_execution_result", AsyncMock(return_value={"id": "res-1"})
    )
    monkeypatch.setattr(
        "app.classifier.classify_transaction", AsyncMock(side_effect=fake_classify)
    )
    monkeypatch.setattr(
        "app.services.preference_service.record_answer_preference", AsyncMock()
    )
    return {
        "session": session,
        "clarifications": clarifications,
        "confirmations": confirmations,
        "tool_runs": tool_runs,
    }


class TestAgentWiring:
    @pytest.mark.asyncio
    async def test_contextual_question_from_the_books_is_asked(
        self, monkeypatch, boundary
    ):
        """The question the user sees comes from the model that inspected the
        records — not from the planner's template questionnaire."""
        async def loader(organization_id, **kw):
            return []

        monkeypatch.setitem(
            be.EVIDENCE_KINDS, "open_payables", _fake_kind(loader, kind="open_payables")
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        question = (
            "I found no open bill for ABC. Was this payment an advance, a loan "
            "repayment, an owner withdrawal, or a new purchase?"
        )
        monkeypatch.setattr(
            agent_mod,
            "get_client",
            lambda: ScriptedOrchestrator(
                reasoning=[
                    {
                        "evidence_requests": [
                            {"kind": "open_payables", "args": {"party_name": "ABC"}}
                        ]
                    },
                    {"question": {"text": question}},
                ]
            ),
        )
        response = await agent_mod.execute(
            user_message=PAID_ABC, user_id=USER, organization_id=ORG
        )
        assert response.status == ExecutionStatus.AWAITING_CLARIFICATION
        assert response.question == question
        assert boundary["clarifications"][0]["question"] == question
        assert boundary["confirmations"] == []
        assert boundary["tool_runs"] == []

    @pytest.mark.asyncio
    async def test_reasoning_is_offered_the_trusted_tool_vocabulary(
        self, monkeypatch, boundary
    ):
        """The model can only name real tools if Python tells it which exist:
        the reasoning prompt carries the TRUSTED registry, not a guess."""
        orch = ScriptedOrchestrator(
            reasoning=[{"question": {"text": "Which bank account was used?"}}]
        )
        monkeypatch.setattr(agent_mod, "get_client", lambda: orch)
        response = await agent_mod.execute(
            user_message=PAID_ABC, user_id=USER, organization_id=ORG
        )
        assert response.status == ExecutionStatus.AWAITING_CLARIFICATION
        prompt = orch.prompts[0]
        assert "OFFERED TOOLS" in prompt
        assert "record_supplier_payment" in prompt
        assert "create_expense" in prompt

    @pytest.mark.asyncio
    async def test_invented_tool_name_never_reaches_a_confirmation(
        self, monkeypatch, boundary
    ):
        """A tool Python does not have is refused by the validator and fed BACK
        to the model — nothing is snapshotted for the user to approve."""
        orch = ScriptedOrchestrator(
            reasoning=[
                {
                    "proposal": {
                        "interpretation": "settle the ABC bill",
                        "affected_records": ["trade payables"],
                        "accounting_impact": [
                            {"account": "Trade Payables", "debit": 50000}
                        ],
                        "not_affected": [],
                        "unresolved_uncertainty": [],
                        "tools": [
                            {"tool_name": "record_payment_by_vibe", "arguments": {}}
                        ],
                        "confirmation": "Record the payment?",
                    }
                },
                {
                    "question": {
                        "text": "Which registered tool should settle the ABC bill?"
                    }
                },
            ]
        )
        monkeypatch.setattr(agent_mod, "get_client", lambda: orch)
        response = await agent_mod.execute(
            user_message=PAID_ABC, user_id=USER, organization_id=ORG
        )
        assert "not in the offered tool list" in orch.prompts[1]
        assert response.status == ExecutionStatus.AWAITING_CLARIFICATION
        assert boundary["confirmations"] == []
        assert boundary["tool_runs"] == []



    @pytest.mark.asyncio
    async def test_accepted_proposal_reaches_confirmation_not_execution(
        self, monkeypatch, boundary
    ):
        """A validated proposal is snapshotted for the user's confirmation and
        executed only after approval (Python's enforcement)."""
        async def loader(organization_id, **kw):
            return [
                {"name": "Internet Expense", "code": "6150", "account_type": "EXPENSE"}
            ]

        monkeypatch.setitem(
            be.EVIDENCE_KINDS,
            "chart_of_accounts",
            _fake_kind(loader, kind="chart_of_accounts"),
        )
        monkeypatch.setattr(
            "app.permissions.authorize_tool", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            agent_mod,
            "get_client",
            lambda: ScriptedOrchestrator(
                reasoning=[
                    {
                        "evidence_requests": [
                            {
                                "kind": "chart_of_accounts",
                                "args": {"terms": ["internet"]},
                            }
                        ]
                    },
                    {
                        "understanding": {"economic_event": "new operating expense"},
                        "proposal": {
                            "interpretation": "record a new internet expense",
                            "affected_records": ["Internet Expense", "cash/bank"],
                            "accounting_impact": [
                                {"account": "Internet Expense", "debit": 5000}
                            ],
                            "not_affected": ["no supplier ledger is created"],
                            "unresolved_uncertainty": ["payment channel"],
                            "tools": [
                                {
                                    "tool_name": "create_expense",
                                    "arguments": {
                                        "amount": 5000,
                                        "payee_name": "Internet Provider",
                                        "description": "internet",
                                    },
                                }
                            ],
                            "confirmation": (
                                "Record the 5,000 internet expense to Internet "
                                "Expense? No supplier ledger will be created."
                            ),
                        },
                    },
                ]
            ),
        )
        response = await agent_mod.execute(
            user_message="record internet expense of 5,000",
            user_id=USER,
            organization_id=ORG,
        )
        assert response.status == ExecutionStatus.AWAITING_CONFIRMATION
        # The user is shown the MODEL's accounting disclosure — interpretation,
        # proposed impact, what will deliberately not change, and the exact
        # confirmation sentence — not a keyword-intent summary.
        summary = response.summary or ""
        assert "record a new internet expense" in summary
        assert "Dr Internet Expense 5,000.00" in summary
        assert "no supplier ledger is created" in summary
        assert "payment channel" in summary
        assert boundary["confirmations"][0]["description"] == (
            "Record the 5,000 internet expense to Internet Expense? "
            "No supplier ledger will be created."
        )
        assert boundary["tool_runs"] == [], "nothing may execute before approval"
        assert len(boundary["confirmations"]) == 1
        snapshot = boundary["confirmations"][0]["plan"]
        assert [entry["tool_name"] for entry in snapshot] == ["create_expense"]
        assert snapshot[0]["arguments"]["amount"] == 5000

    @pytest.mark.asyncio
    async def test_provider_failure_never_invents_a_decision(
        self, monkeypatch, boundary
    ):
        """A dead provider must never become a guessed accounting route: the
        run stops honestly, NOTHING is written, and the provider is not
        hammered twice more in the same request."""
        orch = ScriptedOrchestrator(boom=True)
        monkeypatch.setattr(agent_mod, "get_client", lambda: orch)
        response = await agent_mod.execute(
            user_message=PAID_ABC, user_id=USER, organization_id=ORG
        )
        assert response.status == ExecutionStatus.FAILED
        assert "Nothing was recorded" in (response.summary or "")
        assert boundary["tool_runs"] == []
        assert boundary["confirmations"] == []
        assert boundary["clarifications"] == []
        # ONE provider attempt (the reasoning round) — the perception call and
        # the planning call are skipped instead of adding two more waits.
        assert orch.text_calls == 1
        assert orch.tools_calls == 0

    @pytest.mark.asyncio
    async def test_refusal_is_reported_without_execution(self, monkeypatch, boundary):
        monkeypatch.setattr(
            agent_mod,
            "get_client",
            lambda: ScriptedOrchestrator(
                reasoning=[
                    {
                        "refusal": {
                            "reason": (
                                "Payroll is not supported by this chart of "
                                "accounts and no employee ledgers exist."
                            )
                        }
                    }
                ]
            ),
        )
        response = await agent_mod.execute(
            user_message="run payroll for 12 employees",
            user_id=USER,
            organization_id=ORG,
        )
        assert response.status == ExecutionStatus.REJECTED
        assert "Payroll is not supported" in (response.summary or "")
        assert boundary["tool_runs"] == []

