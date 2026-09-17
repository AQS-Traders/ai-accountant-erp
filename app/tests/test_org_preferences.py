"""Work Stream F - org preferences + clarification memory tests."""

import asyncio
import uuid

import pytest

from app.planner import _resolve_same_answers, plan
from app.services import preference_service

ORG = uuid.uuid4()


class Recorder:
    """Minimal async mock of the database helpers used by the service."""

    def __init__(self, existing=None):
        self.existing = existing or []
        self.inserts = []
        self.updates = []

    async def fetch_many(self, table_name, *, filters=None, select="*",
                         order=None, limit=100, offset=0):
        if filters and filters.get("key"):
            return [r for r in self.existing if r.get("key") == filters["key"]]
        return list(self.existing)

    async def insert_one(self, table_name, *, data):
        self.inserts.append((table_name, data))
        return {"id": "new-1", **data}

    async def update_one(self, table_name, *, row_id, data):
        self.updates.append((table_name, row_id, data))
        return {"id": row_id, **data}


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(preference_service, "fetch_many", rec.fetch_many)
    monkeypatch.setattr(preference_service, "insert_one", rec.insert_one)
    monkeypatch.setattr(preference_service, "update_one", rec.update_one)
    return rec


class TestCapturePreference:
    def test_payment_answer_captured(self):
        shaped = preference_service.capture_preference_from_answer(
            "Was this paid in cash or on credit?", "cash"
        )
        assert shaped == {"key": "payment_method", "value": "CASH"}

    def test_nature_answer_captured(self):
        shaped = preference_service.capture_preference_from_answer(
            "Is this a fixed asset, inventory, consumable or operating expense?",
            "fixed asset",
        )
        assert shaped == {"key": "transaction_nature", "value": "FIXED_ASSET"}

    def test_amount_answer_never_a_preference(self):
        assert preference_service.capture_preference_from_answer(
            "What is the transaction amount?", "150000"
        ) is None
        assert preference_service.capture_preference_from_answer(
            "Who is the supplier?", "ABC Traders"
        ) is None


class TestSetPreference:
    def test_new_preference_inserts(self, recorder):
        row = asyncio.run(preference_service.set_preference(
            ORG, "payment_method", "CASH"
        ))
        assert recorder.inserts and recorder.updates == []
        assert row["value"] == "CASH"
        table, data = recorder.inserts[0]
        assert table == "ai_org_preferences"
        assert data["key"] == "payment_method"

    def test_existing_preference_updated_not_duplicated(self, recorder):
        recorder.existing = [{
            "id": "p1", "key": "payment_method", "value": "CASH",
            "source": "learned",
        }]
        asyncio.run(preference_service.set_preference(
            ORG, "payment_method", "CREDIT", source="learned"
        ))
        assert recorder.inserts == []
        assert len(recorder.updates) == 1
        _, row_id, data = recorder.updates[0]
        assert row_id == "p1" and data["value"] == "CREDIT"

    def test_user_set_never_overwritten_by_learned(self, recorder):
        recorder.existing = [{
            "id": "p1", "key": "payment_method", "value": "CREDIT",
            "source": "user_set",
        }]
        row = asyncio.run(preference_service.set_preference(
            ORG, "payment_method", "CASH", source="learned"
        ))
        assert recorder.updates == [] and recorder.inserts == []
        assert row["value"] == "CREDIT"  # kept


class TestPlannerPreferenceConsumption:
    def test_payment_preference_NOT_assumed(self):
        """Work Stream R2 (product decision): the payment TREATMENT is a
        per-transaction question - a learned payment preference is never
        auto-applied, so the cash/credit question stays in round 1 and a
        party question only appears AFTER credit is actually chosen."""
        p = plan(
            "I bought a laptop",
            org_preferences={"payment_method": "CREDIT"},
        )
        assert p.intent == "record_purchase"
        assert "payment_type" in p.missing_fields
        assert p.extracted_entities.get("payment_method") is None
        # and the supplier question is NOT pulled into this round
        assert all("supplier" not in q.lower() for q in p.clarification_questions)

    def test_explicit_user_value_overrides_preference(self):
        p = plan(
            "I bought a laptop in cash",
            org_preferences={"payment_method": "CREDIT"},
        )
        assert p.extracted_entities.get("payment_method") == "CASH"

    def test_credit_answer_pulls_party_into_next_round(self):
        p = plan(
            "I bought a laptop",
            clarification_history=[
                {"question": "Was this paid in cash or on credit?",
                 "answer": "credit"},
            ],
            org_preferences={"payment_method": "CASH"},
        )
        assert p.intent == "record_credit_purchase"
        assert "supplier_name" in p.missing_fields

    def test_nature_preference_applied_and_reroutes(self):
        p = plan(
            "I bought a generator for Rs.850,000 in cash",
            org_preferences={"transaction_nature": "FIXED_ASSET"},
        )
        assert p.extracted_entities.get("transaction_nature") == "FIXED_ASSET"

    def test_date_never_defaulted_from_preferences(self):
        p = plan(
            "I bought a laptop for Rs.150,000 in cash",
            org_preferences={"transaction_date": "2026-01-01"},
        )
        assert "transaction_date" not in p.extracted_entities
        assert "transaction_date" in p.missing_fields


class TestClarificationMemory:
    def test_same_reuses_prior_answer(self):
        resolved = _resolve_same_answers([
            {"question": "Was this paid in cash or on credit?", "answer": "cash"},
            {"question": "Was this paid in cash or on credit? "
             "(previously: CASH - reply SAME to reuse)", "answer": "SAME"},
        ])
        assert resolved[1]["answer"] == "cash"

    def test_same_without_prior_is_dropped(self):
        resolved = _resolve_same_answers([
            {"question": "Was this paid in cash or on credit?", "answer": "SAME"},
        ])
        assert resolved == []

    def test_memory_hint_offers_prior_value(self):
        import app.agent as agent

        hint = agent._preference_memory_hint(
            {"payment_method": "CASH"},
            ["Was this paid in cash or on credit?"],
        )
        assert hint == "previously: CASH - reply SAME to reuse"

    def test_no_hint_without_matching_preference(self):
        import app.agent as agent

        assert agent._preference_memory_hint(
            {}, ["Was this paid in cash or on credit?"]
        ) == ""
        assert agent._preference_memory_hint(
            {"payment_method": "CASH"}, ["What is the transaction amount?"]
        ) == ""


class TestContextInjection:
    def test_prompt_renders_preferences_block(self):
        from app.models.schemas import AgentContext
        from app.prompts import build_user_content

        ctx = AgentContext(
            organization={"name": "Test Org"},
            user={"user_id": "u1"},
            org_preferences={"payment_method": "CASH"},
        )
        content = build_user_content("I bought a laptop", ctx)
        assert "ORG PREFERENCES" in content
        assert "payment method: CASH" in content

    def test_prompt_without_preferences_has_no_block(self):
        from app.models.schemas import AgentContext
        from app.prompts import build_user_content

        ctx = AgentContext(
            organization={"name": "Test Org"}, user={"user_id": "u1"}
        )
        assert "ORG PREFERENCES" not in build_user_content("hi", ctx)
