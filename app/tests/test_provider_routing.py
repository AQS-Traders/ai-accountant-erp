"""
Provider routing tests (Phase 13 F–J) — stubbed, no network.

F/G: ERP errors never cycle providers. H: banned models excluded.
I/J: capability-aware vision routing.
"""

from __future__ import annotations

import pytest

from app.ai_orchestrator import AIOrchestrator
from app.config import Settings
from app.models.schemas import AgentContext
from app.qwen_client import ProviderError

CHAIN = ["qwen3.6-plus", "qwen3.5-plus", "qwen-max", "qwen-plus"]
VISION = ["qwen3-vl-plus", "qwen3-vl-flash"]


def _ctx():
    return AgentContext(
        organization={"name": "Test", "base_currency_code": "PKR"}, user={"id": "u1"}
    )


class Stub:
    def __init__(self, name, behaviour=None):
        self.name, self.behaviour, self.calls = name, behaviour, 0

    async def generate_with_tools(self, *, user_message, context, max_tool_iterations=10, executor=None):
        self.calls += 1
        if self.behaviour is None:
            return {"text": f"{self.name}-answer", "tool_calls": [],
                    "tool_results": [], "iteration_count": 1}
        return await self.behaviour(executor)


def _stub_orch(monkeypatch, behaviours, gemini_behaviour=None):
    orch = AIOrchestrator()
    stubs = {m: Stub(m, b) for m, b in behaviours.items()}
    cands = [
        {"provider": "qwen", "model": m, "capability": "text_tools",
         "factory": (lambda m=m: stubs[m])}
        for m in behaviours
    ]
    gem = Stub("gemini", gemini_behaviour)
    stubs["gemini"] = gem
    cands.append({"provider": "gemini", "model": None,
                  "capability": "text_tools", "factory": lambda: gem})

    def cands_fn(*, requires_vision: bool = False):
        cap = "vision" if requires_vision else "text_tools"
        return [c for c in cands if c["capability"] == cap]

    monkeypatch.setattr(orch, "_candidate_providers", cands_fn)
    return orch, stubs


@pytest.mark.asyncio
async def test_f_erp_missing_field_error_does_not_cycle(monkeypatch):
    orch, s = _stub_orch(monkeypatch, {m: None for m in CHAIN}, None)

    async def erp_error(exec):
        # ToolResult carries the business error; the SAME provider must
        # keep reasoning (ask the user). Never a provider failure.
        return await exec("create_supplier", {"name": "ABC Computers"})

    s[CHAIN[0]].behaviour = erp_error
    seen = []

    async def executor(tool, args):
        seen.append(tool)
        return {"success": False, "error": {
            "category": "MISSING_REQUIRED_VALUE", "entity": "supplier",
            "field": "credit_limit", "requires_user_input": True}}

    r = await orch.generate_with_tools(user_message="add supplier ABC",
                                       context=_ctx(), executor=executor)
    assert (r["provider"], r["model"]) == ("qwen", CHAIN[0])
    assert all(s[m].calls == 0 for m in CHAIN[1:]) and s["gemini"].calls == 0
    assert seen == ["create_supplier"]


@pytest.mark.asyncio
async def test_g_erp_database_error_does_not_cycle(monkeypatch):
    orch, s = _stub_orch(monkeypatch, {m: None for m in CHAIN}, None)

    async def erp_db(exec):
        return await exec("search_customers", {})

    s[CHAIN[0]].behaviour = erp_db

    async def executor(tool, args):
        return {"success": False, "error": {
            "category": "INFRASTRUCTURE_ERROR",
            "recoverable": False, "requires_user_input": False}}

    r = await orch.generate_with_tools(user_message="list customers",
                                       context=_ctx(), executor=executor)
    assert (r["provider"], r["model"]) == ("qwen", CHAIN[0])
    assert all(s[m].calls == 0 for m in CHAIN[1:]) and s["gemini"].calls == 0


def test_h_banned_model_excluded_from_chains():
    st = Settings(
        qwen_model_chain="qwen3.6-plus,qwen3.7-plus,qwen-max",
        qwen_vision_model_chain="qwen3-vl-plus,qwen3.7-plus",
        qwen_banned_models="qwen3.7-plus",
    )
    assert st.qwen_chain_list == ["qwen3.6-plus", "qwen-max"]
    assert "qwen3.7-plus" not in st.qwen_vision_chain_list


@pytest.mark.asyncio
async def test_h2_banned_model_refused_by_client_factory():
    with pytest.raises(Exception, match="banned"):
        await AIOrchestrator()._get_qwen("qwen3.7-plus")


@pytest.mark.asyncio
async def test_i_vision_request_skips_text_only_models(monkeypatch):
    orch = AIOrchestrator()
    vstubs = {m: Stub(m) for m in VISION}

    def cands_fn(*, requires_vision: bool = False):
        if requires_vision:
            return [{"provider": "qwen", "model": m, "capability": "vision",
                     "factory": (lambda m=m: vstubs[m])} for m in VISION]
        return [{"provider": "qwen", "model": m, "capability": "text_tools",
                 "factory": (lambda m=m: Stub(m))} for m in CHAIN] + [
            {"provider": "gemini", "model": None,
             "capability": "text_tools", "factory": lambda: Stub("gemini")}]

    monkeypatch.setattr(orch, "_candidate_providers", cands_fn)

    text_c = orch._candidate_providers(requires_vision=False)
    vision_c = orch._candidate_providers(requires_vision=True)
    assert all(c["capability"] == "text_tools" for c in text_c)
    assert [c["model"] for c in vision_c] == VISION
    assert all(c["provider"] != "gemini" for c in vision_c)


@pytest.mark.asyncio
async def test_j_vision_first_model_success_no_fallback(monkeypatch):
    orch = AIOrchestrator()
    vstubs = {m: Stub(m) for m in VISION}

    def cands_fn(*, requires_vision: bool = False):
        return [{"provider": "qwen", "model": m, "capability": "vision",
                 "factory": (lambda m=m: vstubs[m])} for m in VISION]

    monkeypatch.setattr(orch, "_candidate_providers", cands_fn)
    r = await orch.generate_with_tools(user_message="extract invoice",
                                       context=_ctx(), requires_vision=True)
    assert (r["provider"], r["model"]) == ("qwen", VISION[0])
    assert vstubs[VISION[0]].calls == 1 and vstubs[VISION[1]].calls == 0
