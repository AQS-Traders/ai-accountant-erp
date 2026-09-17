"""
Unit tests for the AI orchestrator (stubbed providers — no network).

Run: venv\\Scripts\\python -m pytest app/tests/test_ai_orchestrator.py -v
"""

from __future__ import annotations

import pytest

from app.ai_orchestrator import AIOrchestrator
from app.models.schemas import AgentContext
from app.qwen_client import ProviderError


def _ctx() -> AgentContext:
    return AgentContext(
        organization={"name": "Test", "base_currency_code": "PKR"},
        user={"id": "u1"},
    )


class StubClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def generate_with_tools(self, *, user_message, context, max_tool_iterations=10, executor=None):
        self.calls += 1
        return await self.behaviour(executor)

    async def generate_text(self, *, prompt, context=None):
        return "stub-text"


@pytest.mark.asyncio
async def test_primary_qwen_used_when_healthy(monkeypatch):
    orch = AIOrchestrator()

    qwen = StubClient(lambda executor: _ok("qwen-answer"))
    gemini = StubClient(lambda executor: _ok("gemini-answer"))
    monkeypatch.setattr(orch, "_get_qwen", lambda model=None: qwen)
    monkeypatch.setattr(orch, "_get_gemini", lambda: gemini)

    result = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert result["provider"] == "qwen"
    assert result["text"] == "qwen-answer"
    assert qwen.calls == 1 and gemini.calls == 0


@pytest.mark.asyncio
async def test_fallback_to_gemini_on_provider_failure(monkeypatch):
    orch = AIOrchestrator()

    async def qwen_behave(executor):
        raise ProviderError("network down")

    qwen = StubClient(qwen_behave)
    gemini = StubClient(lambda executor: _ok("gemini-answer"))
    monkeypatch.setattr(orch, "_get_qwen", lambda model=None: qwen)
    monkeypatch.setattr(orch, "_get_gemini", lambda: gemini)

    result = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert result["provider"] == "gemini"
    assert result["text"] == "gemini-answer"
    assert gemini.calls == 1


@pytest.mark.asyncio
async def test_no_fallback_after_tool_execution(monkeypatch):
    """After any tool executed, a provider failure must NOT trigger Gemini replay."""
    orch = AIOrchestrator()
    executed = {"count": 0}

    async def qwen_behave(executor):
        # Simulate: Qwen called a tool (mutation), then the provider died.
        if executor:
            executed["count"] += 1
            await executor("record_cash_sale", {"amount": 100})
        raise ProviderError("connection reset mid-conversation")

    qwen = StubClient(qwen_behave)
    gemini = StubClient(lambda executor: _ok("gemini-answer"))
    monkeypatch.setattr(orch, "_get_qwen", lambda model=None: qwen)
    monkeypatch.setattr(orch, "_get_gemini", lambda: gemini)

    async def executor(tool_name, args):
        return {"success": True}

    with pytest.raises(RuntimeError, match="AFTER tools executed"):
        await orch.generate_with_tools(
            user_message="hi", context=_ctx(), executor=executor
        )
    # Gemini must never have been invoked (no blind replay of a mutation).
    assert gemini.calls == 0
    assert executed["count"] == 1


@pytest.mark.asyncio
async def test_all_providers_down_raises(monkeypatch):
    orch = AIOrchestrator()

    async def fail(executor):
        raise ProviderError("down")

    monkeypatch.setattr(orch, "_get_qwen", lambda model=None: StubClient(fail))
    monkeypatch.setattr(orch, "_get_gemini", lambda: StubClient(fail))

    with pytest.raises(RuntimeError, match="All AI providers failed"):
        await orch.generate_with_tools(user_message="hi", context=_ctx())


@pytest.mark.asyncio
async def test_unconfigured_provider_skipped(monkeypatch):
    """A provider that cannot even be constructed must not crash the dispatch."""
    orch = AIOrchestrator()

    def no_qwen(model=None):
        raise ProviderError("QWEN_API_KEY not configured")

    gemini = StubClient(lambda executor: _ok("gemini-answer"))
    monkeypatch.setattr(orch, "_get_qwen", no_qwen)
    monkeypatch.setattr(orch, "_get_gemini", lambda: gemini)

    result = await orch.generate_with_tools(user_message="hi", context=_ctx())
    assert result["provider"] == "gemini"


async def _ok(text):
    return {"text": text, "tool_calls": [], "tool_results": [], "iteration_count": 1}
