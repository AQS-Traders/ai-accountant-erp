"""
Provider-chain fallback tests (Phase 13 A–E) — stubbed, no network.

Chain: qwen3.6-plus → qwen3.5-plus → qwen-max → qwen-plus → Gemini.
"""

from __future__ import annotations

import pytest

from app.ai_orchestrator import AIOrchestrator
from app.models.schemas import AgentContext
from app.qwen_client import ProviderError

CHAIN = ["qwen3.6-plus", "qwen3.5-plus", "qwen-max", "qwen-plus"]


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
            return _ok(f"{self.name}-answer")
        return await self.behaviour(executor)


def _ok(text):
    return {"text": text, "tool_calls": [], "tool_results": [], "iteration_count": 1}


def _stub_orch(monkeypatch, behaviours, gemini_behaviour=None):
    """Orchestrator with a controlled stub candidate chain."""
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
async def test_a_first_model_success_short_circuits(monkeypatch):
    orch, s = _stub_orch(monkeypatch, {m: None for m in CHAIN}, None)
    r = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert (r["provider"], r["model"]) == ("qwen", CHAIN[0])
    assert s[CHAIN[0]].calls == 1
    assert all(s[m].calls == 0 for m in CHAIN[1:]) and s["gemini"].calls == 0


@pytest.mark.asyncio
async def test_b_quota_failure_moves_to_next_model_only(monkeypatch):
    async def quota(exec):
        raise ProviderError("HTTP 429: quota exhausted")

    b = {m: None for m in CHAIN}
    b[CHAIN[0]] = quota
    orch, s = _stub_orch(monkeypatch, b, None)
    r = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert r["model"] == CHAIN[1]
    assert s[CHAIN[0]].calls == 1 and s[CHAIN[1]].calls == 1
    assert all(s[m].calls == 0 for m in CHAIN[2:]) and s["gemini"].calls == 0


@pytest.mark.asyncio
async def test_c_two_failures_third_succeeds(monkeypatch):
    async def fail(exec):
        raise ProviderError("connection reset")

    b = {m: None for m in CHAIN}
    b[CHAIN[0]] = b[CHAIN[1]] = fail
    orch, s = _stub_orch(monkeypatch, b, None)
    r = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert r["model"] == CHAIN[2]
    assert s[CHAIN[2]].calls == 1
    assert s[CHAIN[3]].calls == 0 and s["gemini"].calls == 0


@pytest.mark.asyncio
async def test_d_all_qwen_fail_gemini_final_fallback(monkeypatch):
    async def fail(exec):
        raise ProviderError("down")

    orch, s = _stub_orch(monkeypatch, {m: fail for m in CHAIN}, None)
    r = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert r["provider"] == "gemini"
    assert all(s[m].calls == 1 for m in CHAIN) and s["gemini"].calls == 1


@pytest.mark.asyncio
async def test_e_no_fallback_after_mutation(monkeypatch):
    orch, s = _stub_orch(monkeypatch, {m: None for m in CHAIN}, None)
    ran = {"n": 0}

    async def die_after_mutation(exec):
        if exec:
            ran["n"] += 1
            await exec("record_cash_sale", {"amount": 100})
        raise ProviderError("connection reset mid-conversation")

    s[CHAIN[0]].behaviour = die_after_mutation

    async def executor(tool, args):
        return {"success": True}

    with pytest.raises(RuntimeError, match="AFTER tools executed"):
        await orch.generate_with_tools(user_message="hi", context=_ctx(), executor=executor)
    assert all(s[m].calls == 0 for m in CHAIN[1:]) and s["gemini"].calls == 0
    assert ran["n"] == 1  # executed exactly once — never replayed
