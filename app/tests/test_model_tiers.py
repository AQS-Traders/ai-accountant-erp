"""
MODEL TIER ROUTING — measured model selection per task class (no network).

The workspace endpoint is an AGGREGATED catalog (Qwen + DeepSeek + Kimi +
GLM, 169 models). Probing every reachable model with the REAL prompts
(2026-09-20) produced the tier table recorded in ``app/config.py``; these
tests pin the MECHANISM that acts on it:

* the fast tier is resolved from config and can be switched off;
* the fast tier is what the mechanical extraction stage actually calls;
* an explicit chain IS the candidate list — a tier model that is not in the
  standard chain must still be used (before this fix the orchestrator
  filtered the standard list by the requested chain, so such a tier
  collapsed to the standard chain and the whole tiering was inert);
* the client tolerates per-model parameter quirks (kimi-k3 rejects
  ``temperature``; small Qwen3 models demand ``enable_thinking=false``),
  because a model that fails on every call is a model the ERP cannot use.
"""

from __future__ import annotations

import json

import pytest

from app.ai_orchestrator import AIOrchestrator
from app.config import Settings
from app.qwen_client import ProviderError, QwenClient

FAST_HEAD = "qwen3-30b-a3b-instruct-2507"


class StubClient:
    """Records which model handled the call."""

    def __init__(self, name, sink):
        self.model_name = name
        self._sink = sink

    async def generate_text(self, *, prompt, context=None):
        self._sink.append(("text", self.model_name))
        return json.dumps({"activities": ["sale"], "facts": []})


def _orch(monkeypatch, sink, known=None):
    """Orchestrator whose providers are stubs; ``known`` = usable models."""
    orch = AIOrchestrator()
    known = set(known or [])

    def factory(model=None):
        if known and model not in known:
            raise ProviderError(f"Qwen API error HTTP 404: model_not_found ({model})")
        return StubClient(model, sink)

    monkeypatch.setattr(orch, "_get_qwen", factory)
    monkeypatch.setattr(orch, "_get_gemini", lambda: StubClient("gemini", sink))
    return orch


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


def test_tier_defaults_are_the_measured_models():
    """The SHIPPED defaults, asserted from the field definitions so a local
    .env (which legitimately overrides chains) can never mask a regression."""
    fields = Settings.model_fields
    assert fields["accounting_fast_model_chain"].default == (
        f"{FAST_HEAD},qwen-flash,qwen-max"
    )
    # Deep tier: the two models that answered the real 10.9 KB reasoning prompt
    # with the correct disposal decision in ~10.7s.
    assert fields["accounting_reasoning_model_chain"].default == "qwen3-max,qwen-max"
    # Standard (tool planning) chain: measured-fastest tool model first.
    assert fields["qwen_model_chain"].default == "qwen-max,qwen3-max,qwen3.6-plus"


def test_fast_tier_resolves_from_config():
    st = Settings(accounting_fast_model_chain="qwen-flash,qwen-plus-latest")
    assert st.accounting_fast_chain_list == ["qwen-flash", "qwen-plus-latest"]


def test_fast_tier_can_be_switched_off_by_config():
    """``standard`` is the documented kill switch: the mechanical stages run on
    the standard chain again."""
    st = Settings(accounting_fast_model_chain="standard")
    assert st.accounting_fast_chain_list == st.qwen_chain_list


def test_blank_fast_chain_is_treated_as_unset_not_as_a_kill_switch():
    """A blank value never reaches the field: ``Settings._empty_env_means_unset``
    REMOVES it (the serverless deploy fix — Vercel materialises optional
    variables as empty strings), so the field DEFAULT applies and the fast tier
    stays ON. ``ACCOUNTING_FAST_MODEL_CHAIN=""`` therefore cannot disable the
    tier; ``standard`` is the documented kill switch."""
    default_fast = Settings.model_fields["accounting_fast_model_chain"].default
    for value in ("", "   "):
        st = Settings(accounting_fast_model_chain=value)
        assert st.accounting_fast_model_chain == default_fast
        assert st.accounting_fast_chain_list == Settings(
            accounting_fast_model_chain=default_fast
        ).accounting_fast_chain_list


def test_fast_chain_naming_no_usable_model_degrades_to_the_standard_chain():
    """A value that IS present but names no usable model (``","``) is not blank,
    so it does reach the field and the fast chain degrades to the standard chain
    rather than leaving a mechanical stage without a model."""
    st = Settings(accounting_fast_model_chain=",")
    assert st.accounting_fast_chain_list == st.qwen_chain_list


def test_fast_tier_excludes_banned_models():
    st = Settings(accounting_fast_model_chain="qwen3.7-plus,qwen-flash")
    assert st.accounting_fast_chain_list == ["qwen-flash"]


def test_all_banned_fast_chain_degrades_to_the_standard_chain():
    """If the config names only banned models the fast chain degrades to the
    standard chain — which itself can never contain a banned model."""
    banned = Settings().qwen_banned_models.split(",")[0].strip()
    assert banned  # a real ban must be configured
    st = Settings(accounting_fast_model_chain=banned)
    assert st.accounting_fast_chain_list == st.qwen_chain_list
    assert banned not in st.accounting_fast_chain_list


# ---------------------------------------------------------------------------
# Tier routing actually reaches the transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_light_entry_point_uses_the_fast_tier(monkeypatch):
    sink = []
    orch = _orch(monkeypatch, sink)
    await orch.generate_text_light(prompt="extract facts from this request")
    assert sink and sink[0] == ("text", FAST_HEAD)


@pytest.mark.asyncio
async def test_tier_model_outside_the_standard_chain_is_actually_used(monkeypatch):
    """The requested chain IS the candidate list: a tier model the standard chain
    does not contain must still be the model that runs. Before the fix the
    requested chain only FILTERED the standard list, so this call silently ran
    on the standard head and the whole tiering was inert."""
    tier_model = "qwen-flash"
    if tier_model in Settings().qwen_chain_list:
        # A local .env (or a future default) may legitimately add it; the point
        # of this test is the FILTER, so skip instead of asserting nothing.
        pytest.skip(f"{tier_model} is part of this deployment's standard chain")
    sink = []
    orch = _orch(monkeypatch, sink)
    await orch.generate_text(prompt="x", model_chain=[tier_model])
    assert sink[0] == ("text", tier_model)


@pytest.mark.asyncio
async def test_tier_chain_order_is_honoured(monkeypatch):
    """A tier chain is traversed in the order it names, and a model the provider
    does not serve fails only its own candidate: the next TIER model runs before
    the transport reaches Gemini."""
    sink = []
    orch = _orch(monkeypatch, sink, known={"qwen-flash"})
    await orch.generate_text(
        prompt="x", model_chain=["qwen3.7-plus", "qwen-flash", "qwen-max"]
    )
    assert sink == [("text", "qwen-flash")]


@pytest.mark.asyncio
async def test_unknown_tier_model_falls_through_to_the_next_provider(monkeypatch):
    sink = []
    orch = _orch(monkeypatch, sink, known={"qwen-max"})
    await orch.generate_text(prompt="x", model_chain=["not-a-real-model"])
    assert sink == [("text", "gemini")]


# ---------------------------------------------------------------------------
# Mechanical stage uses the fast tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_extraction_runs_on_the_fast_tier():
    from app.semantic_layer import extract_semantic_facts

    calls = []

    class Orch:
        async def generate_text_light(self, *, prompt, context=None):
            calls.append("light")
            return json.dumps({"activities": ["sale"], "facts": []})

        async def generate_text(self, *, prompt, context=None):
            calls.append("standard")
            return ""

    await extract_semantic_facts("sold 2 chairs for 23000", orchestrator=Orch())
    assert calls[:1] == ["light"]


@pytest.mark.asyncio
async def test_semantic_extraction_still_works_without_the_light_entry_point():
    from app.semantic_layer import extract_semantic_facts

    calls = []

    class Orch:
        async def generate_text(self, *, prompt, context=None):
            calls.append("standard")
            return json.dumps({"activities": ["sale"], "facts": []})

    await extract_semantic_facts("sold 2 chairs for 23000", orchestrator=Orch())
    assert calls[:1] == ["standard"]


# ---------------------------------------------------------------------------
# Per-model parameter quirks (aggregated catalog)
# ---------------------------------------------------------------------------

_KIMI_400 = (
    '{"error": {"message": "Parameter \'temperature\'=0.1 is not supported '
    'for kimi-k3 model."}}'
)
_THINKING_400 = (
    '{"error": {"message": "parameter.enable_thinking must be set to false '
    'for non-streaming calls"}}'
)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _fake_httpx(monkeypatch, responses, sent):
    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            return responses.pop(0)

    import app.qwen_client as qc

    monkeypatch.setattr(qc.httpx, "AsyncClient", FakeAsyncClient)


def test_only_capability_400s_are_treated_as_quirks():
    assert QwenClient._quirk_from_error(_KIMI_400) == "no_temperature"
    assert QwenClient._quirk_from_error(_THINKING_400) == "thinking_off"
    # Genuine failures must keep raising so the caller can fall back.
    assert QwenClient._quirk_from_error('{"error": "Arrearage quota"}') is None
    assert QwenClient._quirk_from_error('{"error": "Model not exist."}') is None


def test_payload_omits_learned_quirks():
    c = QwenClient(
        api_key="k", base_url="https://example.test/v1", model="m", temperature=0.3
    )
    body = c._payload([{"role": "user", "content": "hi"}], None, 64)
    assert body["temperature"] == 0.3 and "enable_thinking" not in body

    c._param_quirks.update({"no_temperature", "thinking_off"})
    body = c._payload([{"role": "user", "content": "hi"}], None, 64)
    assert "temperature" not in body
    assert body["enable_thinking"] is False


@pytest.mark.asyncio
async def test_client_retries_without_temperature_when_the_model_rejects_it(monkeypatch):
    sent = []
    _fake_httpx(
        monkeypatch,
        [
            _Resp(400, text=_KIMI_400),
            _Resp(200, payload={"choices": [{"message": {"content": "ok"}}]}),
        ],
        sent,
    )
    client = QwenClient(
        api_key="k", base_url="https://example.test/v1", model="kimi-k3"
    )
    out = await client.generate_text(prompt="hi")
    assert out == "ok"
    assert sent[0]["temperature"] == 0.1
    assert "temperature" not in sent[1]
    assert sent[1]["model"] == "kimi-k3"


@pytest.mark.asyncio
async def test_client_sets_enable_thinking_false_when_required(monkeypatch):
    sent = []
    _fake_httpx(
        monkeypatch,
        [
            _Resp(400, text=_THINKING_400),
            _Resp(200, payload={"choices": [{"message": {"content": "ok"}}]}),
        ],
        sent,
    )
    client = QwenClient(
        api_key="k", base_url="https://example.test/v1", model="qwen3-14b"
    )
    out = await client.generate_text(prompt="hi")
    assert out == "ok"
    assert sent[1]["enable_thinking"] is False
