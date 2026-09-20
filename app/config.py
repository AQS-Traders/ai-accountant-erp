"""
ERP AI Agent — Centralised Configuration
==========================================
All environment variables are loaded from .env via pydantic-settings.
Secrets (Gemini API key) are retrieved from Supabase Vault at runtime,
never stored in environment or config files.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The constitution is the agent's binding governance document. It is loaded
# as system-instruction material by BOTH the semantic understanding layer
# (the reasoning rulebook) and the tool-planning prompt, so the lookup checks
# the documented location as well as the repo root — a missing constitution
# silently weakens the agent's reasoning, which must not depend on where the
# file happens to sit.
CONSTITUTION_PATH = PROJECT_ROOT / "ERP_AGENT_CONSTITUTION.md"
CONSTITUTION_FALLBACK_PATH = PROJECT_ROOT / "docs" / "ERP_AGENT_CONSTITUTION.md"


def constitution_path() -> Path:
    """Resolve the constitution file (repo root first, then ``docs/``)."""
    if CONSTITUTION_PATH.exists():
        return CONSTITUTION_PATH
    if CONSTITUTION_FALLBACK_PATH.exists():
        return CONSTITUTION_FALLBACK_PATH
    return CONSTITUTION_PATH


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _empty_env_means_unset(cls, data):
        """DEPLOY FIX: treat present-but-EMPTY environment variables as unset.

        Serverless platforms (Vercel) materialize optional environment
        variables as EMPTY STRINGS. pydantic then fails numeric parsing
        ("Input should be a valid number, unable to parse string as a
        number [type=float_parsing, input_value='']") at function import,
        crashing every cold start (FUNCTION_INVOCATION_FAILED) and taking
        the whole deployment down. With this validator, an empty var is
        REMOVED from the input so the documented default applies exactly
        as if the variable had never been set. Required credentials that
        are empty surface as the clear "Field required" error instead. """
        if isinstance(data, dict):
            return {
                k: v
                for k, v in data.items()
                if not (isinstance(v, str) and not v.strip())
            }
        return data

    # ---- Supabase --------------------------------------------------------
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_anon_key: str = Field(..., description="Supabase anon/public key")
    supabase_service_role_key: str = Field(
        ..., description="Supabase service-role key (backend only)"
    )

    # ---- Gemini (FALLBACK provider — model config only, key in Vault) ----
    # gemini-2.5-flash was retired by Google for new API users; the API
    # directs new integrations to gemini-3.6-flash.
    gemini_model: str = Field(default="gemini-3.6-flash")
    gemini_temperature: float = Field(default=0.1)
    gemini_max_output_tokens: int = Field(
        default=2048,
        description=(
            "Cap on generated tokens. The agent returns JSON tool calls and a "
            "short summary, so 8192 wasted latency: output tokens dominate "
            "generation time and a stalled turn burns the whole planning "
            "budget. Verified against the light-budget path (600 tokens), "
            "which already returned correct tool calls."
        ),
    )
    gemini_top_p: float = Field(default=0.95)
    gemini_top_k: int = Field(default=40)

    # ---- Qwen (PRIMARY runtime AI provider — Alibaba Model Studio) -------
    # Credentials come ONLY from environment/.env — never hardcoded.
    # Endpoint/model are the exact values from the provisioned workspace:
    #   openAiCompatible: https://ws-n3bn056cf66n6765.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
    #   LLM model codes:  qwen3.7-plus, qwen3.6-plus, qwen-max,
    #                     qwen-plus-2025-07-28, qwen-mt-flash, qwen3-vl-*
    qwen_api_key: str = Field(
        default="",
        description="Alibaba Model Studio workspace API key (sk-ws-...)",
    )
    qwen_base_url: str = Field(
        default=(
            "https://ws-n3bn056cf66n6765.ap-southeast-1.maas.aliyuncs.com"
            "/compatible-mode/v1"
        ),
        description="Qwen OpenAI-compatible base URL",
    )
    qwen_dashscope_url: str = Field(
        default=(
            "https://ws-n3bn056cf66n6765.ap-southeast-1.maas.aliyuncs.com"
            "/api/v1"
        ),
        description="Qwen DashScope native base URL (reference only)",
    )
    qwen_model: str = Field(
        default="qwen3.6-plus",
        description="Exact LLM model code from the Model Studio workspace",
    )
    # Verified LIVE via GET /models + capability probes
    # (scripts/verify_qwen_models.py, scripts/probe_qwen_capabilities.py).
    # Ordered fallback chain of ERP-capable Qwen models (text + tool-calling).
    # NOTE: qwen3.7-plus is BANNED from the active chain (constitution rule);
    # qwen-plus-2025-07-28 does NOT exist on the workspace (stale reference).
    #
    # MEASURED 2026-09-20 on this workspace endpoint, same 2-tool request
    # ("look up supplier ABC and its open payables"):
    #     qwen3-30b-a3b-instruct-2507  3.47s   correct tool calls
    #     qwen-max                     3.75s   correct tool calls
    #     qwen-flash                   6.19s   correct tool calls
    #     qwen3.6-plus                 8.12s   correct tool calls (THINKING)
    #     qwen3-next-80b-a3b-instruct  9.12s   correct tool calls
    #     qwen3-max                    9.11s   correct tool calls
    # qwen3.6-plus was the previous head of this chain: it is a THINKING model
    # that emits reasoning tokens before every answer, so it was the slowest
    # viable option for tool planning while adding no capability the others
    # lack.  It stays in the chain as a last Qwen resort before Gemini.
    qwen_model_chain: str = Field(
        default="qwen-max,qwen3-max,qwen3.6-plus",
        description=(
            "Ordered Qwen fallback chain for text/tool ERP requests. Ordered "
            "by MEASURED latency for the same tool-calling task (see above); "
            "the orchestrator ALREADY falls back to Gemini after this chain, "
            "so extra Qwen entries only multiply the worst-case time (models x "
            "internal retries x timeout) without adding a distinct provider."
        ),
    )
    qwen_vision_model_chain: str = Field(
        default="qwen3-vl-plus,qwen3-vl-flash",
        description="Ordered Qwen vision-capable chain for document extraction",
    )
    qwen_banned_models: str = Field(
        default="qwen3.7-plus",
        description="Models that must NEVER be selected at runtime",
    )
    qwen_temperature: float = Field(default=0.1)
    qwen_max_output_tokens: int = Field(
        default=2048,
        description=(
            "Cap on generated tokens. See gemini_max_output_tokens — the agent "
            "emits JSON tool calls, not prose, so a large cap only adds latency. "
            "Measured impact: every extra 1000 output tokens adds seconds to a "
            "turn, and up to MAX_TOOL_ITERATIONS turns are chained per run."
        ),
    )
    qwen_timeout_seconds: float = Field(
        default=30.0,
        description=(
            "Per-ATTEMPT HTTP timeout. Was 120s, which combined with the "
            "client's internal retries and the multi-model chain allowed a "
            "single stalled provider to consume minutes of a request. The "
            "provider round-trip itself is ~3-5s from the deployed region, so "
            "30s is a generous ceiling for a real generation."
        ),
    )

    # ---- Provider orchestration ------------------------------------------
    ai_primary_provider: str = Field(
        default="qwen",
        description="PRIMARY runtime AI provider (qwen)",
    )
    ai_fallback_provider: str = Field(
        default="gemini",
        description="FALLBACK runtime AI provider (gemini)",
    )
    provider_health_ttl_seconds: int = Field(
        default=60,
        description="How long a provider health result is cached",
    )

    # ---- Application -----------------------------------------------------
    app_env: str = Field(default="development")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    app_log_level: str = Field(default="INFO")

    # ---- Security --------------------------------------------------------
    cors_origins: str = Field(default="http://localhost:3000")
    jwt_secret: str = Field(
        default="change-me",
        description="Legacy HS256 JWT secret (fallback for pre-rotation tokens)",
    )
    jwt_verify_exp: bool = Field(default=True, description="Verify JWT expiration")
    allow_dev_header_auth: bool = Field(
        default=False,
        description=(
            "Permit the legacy X-User-Id header authentication fallback. "
            "Honoured ONLY when app_env == 'development'; ignored in staging "
            "and production. Defaults to false so an unconfigured or "
            "misconfigured deployment fails closed and treats a header-only "
            "request as unauthenticated."
        ),
    )

    # ---- Database --------------------------------------------------------
    database_pool_size: int = Field(default=10)
    database_max_overflow: int = Field(default=20)

    # ---- Semantic understanding layer (Work Stream S2) --------------------
    # This is the PRIMARY semantic interpretation stage of the ERP: the user's
    # natural-language request goes to the LLM FIRST, together with the
    # reasoning rulebook and a bounded, organization-scoped ERP context
    # package, and comes back as a grounded SemanticIntent (see
    # app/semantic_layer.py, app/semantic_context.py, app/semantic_contract.py).
    # The deterministic keyword extractor is the FALLBACK: when the provider
    # is disabled, down, slow or returns unparseable output, the stage
    # degrades to exactly the previous keyword behaviour.
    semantic_llm_enabled: bool = Field(
        default=True,
        description=(
            "Enable the LLM semantic understanding layer (primary interpreter)."
        ),
    )
    semantic_llm_timeout_seconds: float = Field(
        default=8.0,
        description=(
            "Wall-clock cap for the semantic understanding call. It runs before "
            "planning, so the budget must leave room for the planning round."
        ),
    )
    semantic_context_enabled: bool = Field(
        default=True,
        description=(
            "Include the organization-scoped ERP context package (entities, "
            "accounts, capabilities) in the semantic prompt."
        ),
    )
    semantic_max_questions: int = Field(
        default=3,
        description=(
            "Maximum AI-generated clarification questions accepted from the "
            "semantic layer (targeted questions only — never a questionnaire)."
        ),
    )

    # ---- LLM-primary accounting reasoning loop (Work Stream S3) ------------
    # The LLM is the PRIMARY accounting reasoning layer: it inspects the live
    # books (through the closed, permission-checked evidence catalog in
    # app/books_evidence.py), reassesses the request against what the records
    # actually contain, and decides the next step (contextual question,
    # preparatory/correcting transaction, settlement, mutation, refusal).
    # Python remains the enforcement layer (tools, permissions, confirmation,
    # constraints, execution).  When the provider is unavailable the stage
    # degrades to the previous deterministic behaviour — never to a guess.
    accounting_reasoning_enabled: bool = Field(
        default=True,
        description=(
            "Enable the LLM accounting-reasoning loop (primary reasoning "
            "layer). Disabling it restores the previous deterministic "
            "pipeline exactly."
        ),
    )
    accounting_reasoning_timeout: float = Field(
        default=30.0,
        description=(
            "Per-round wall-clock cap for the accounting reasoning call. "
            "MEASURED: the reasoning prompt is the largest call in the "
            "pipeline (~12 KB: rules + evidence catalog + the trusted tool "
            "vocabulary), and the provider needs 6-25s for it — the earlier "
            "12s cap made the FIRST round time out on every request, which "
            "silently degraded every request to the legacy route. Keep this "
            "above the provider's real latency."
        ),
    )
    accounting_reasoning_total_timeout: float = Field(
        default=45.0,
        description=(
            "Wall-clock cap for the WHOLE reasoning loop (all rounds). A "
            "per-round cap alone lets three slow rounds add up; this bounds "
            "the stage the user waits on before the pipeline degrades."
        ),
    )
    accounting_reasoning_model_chain: str = Field(
        default="qwen3-max,qwen-max",
        description=(
            "Comma-separated model chain for the accounting reasoning rounds "
            "(the deep tier). MEASURED 2026-09-20 against the REAL 10.9 KB "
            "reasoning prompt on the live workspace endpoint, using the "
            "disposal case: qwen-max 10.67s and qwen3-max 10.75s both returned "
            "the correct decision (event_type=disposal + a fixed_assets "
            "evidence request); qwen3.7-max 18.6s; deepseek-v4-pro 30.4s; "
            "qwen3.6-plus 57.8s (10 304 thinking chars); qwen3.8-2.4t-a95b "
            "61.5s; qwen3.8-max timed out past 61s. The previous default "
            "(qwen-max alone) is kept as the second entry for quota/pacing "
            "redundancy. Set to an empty string to use the standard "
            "qwen_model_chain; a chain naming a model the deployment does not "
            "have falls back to the standard chain."
        ),
    )
    accounting_reasoning_max_rounds: int = Field(
        default=3,
        description=(
            "Maximum reasoning rounds (evidence request → reassessment "
            "→ decision). Bounded so a confused model can never loop."
        ),
    )
    # ---------------------------------------------------------------------
    # MODEL TIERS (measured, 2026-09-20)
    #
    # The workspace endpoint is an AGGREGATED catalog (169 models: Qwen 3.8,
    # DeepSeek v4, Kimi, GLM). Probing every reachable model showed the tier
    # boundaries are NOT where the marketing names suggest: a small instruct
    # model answers a mechanical extraction FASTER than a "plus" model, while
    # the classic flagship answers the hardest accounting question as fast as
    # anything else. Full table: docs/LLM_PRIMARY_REASONING_ARCHITECTURE.md.
    #
    #   tier          | measured winner                        | use
    #   fast          | qwen3-30b-a3b-instruct-2507   3.5-10s | extraction,
    #                 | qwen-flash / qwen3-next-80b-a3b       | perception,
    #                 |                                       | classification
    #   standard      | qwen-max                      3.8-11s | tool planning
    #   deep          | qwen3-max / qwen-max          10.7s   | accounting
    #                 |                                       | reasoning
    #
    # MODELS THAT MISCLASSIFIED the disposal test (do NOT put them on the
    # accounting reasoning path): qwen3.7-flash and deepseek-v4-flash both
    # returned event_type=new_event for "record sale of fixed asset ...".
    # MODELS THAT ARE TOO SLOW for an interactive request on the reasoning
    # prompt: qwen3.6-plus 57.8s, qwen3.8-2.4t-a95b 61.5s, qwen3.8-max >61s.
    # MODELS THAT REJECT parameters (handled by the client's tolerant
    # retry): kimi-k3 rejects `temperature`; qwen3-14b requires
    # `enable_thinking=false` for non-streaming calls.
    # ---------------------------------------------------------------------
    accounting_fast_model_chain: str = Field(
        default="qwen3-30b-a3b-instruct-2507,qwen-flash,qwen-max",
        description=(
            "Comma-separated model chain for MECHANICAL LLM work that needs "
            "no accounting judgement: semantic fact extraction, perception, "
            "classification and formatting. MEASURED: qwen3-30b-a3b-instruct-"
            "2507 answered the full 10.9 KB accounting prompt with the correct "
            "disposal decision in 9.8s and produced correct tool calls in "
            "3.5s; qwen-flash is the 6.2s / 9.2s equivalent. The chain ends "
            "with qwen-max so a fast-tier quota problem degrades to a known-"
            "good model instead of failing. Set it to 'standard' to run these "
            "stages on the standard qwen_model_chain again (an EMPTY value "
            "cannot be used as the kill switch: the deploy validator treats a "
            "present-but-empty env var as unset)."
        ),
    )

    # Deprecated S1 names. They are still accepted (environment and tests) and
    # resolved through the properties below; code must never read them
    # directly, so the S2 role is never described as a "fallback".
    entity_llm_fallback: Optional[bool] = Field(
        default=None, description="Deprecated S1 alias for semantic_llm_enabled."
    )
    entity_llm_timeout_seconds: Optional[float] = Field(
        default=None,
        description="Deprecated S1 alias for semantic_llm_timeout_seconds.",
    )

    # ---- Helpers ---------------------------------------------------------
    @property
    def semantic_understanding_enabled(self) -> bool:
        """Whether the semantic understanding layer runs (S2 or legacy alias)."""
        if self.entity_llm_fallback is not None:
            return bool(self.entity_llm_fallback)
        return bool(self.semantic_llm_enabled)

    @property
    def semantic_understanding_timeout(self) -> float:
        """Effective wall-clock budget for the semantic understanding call."""
        if self.entity_llm_timeout_seconds is not None:
            return float(self.entity_llm_timeout_seconds)
        return float(self.semantic_llm_timeout_seconds)

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def qwen_chain_list(self) -> List[str]:
        """Ordered, de-duplicated text/tool Qwen chain with banned models removed."""
        banned = {m.strip() for m in self.qwen_banned_models.split(",") if m.strip()}
        seen: List[str] = []
        for m in self.qwen_model_chain.split(","):
            m = m.strip()
            if m and m not in banned and m not in seen:
                seen.append(m)
        return seen or [self.qwen_model]

    @property
    def accounting_fast_chain_list(self) -> List[str]:
        """Ordered chain for MECHANICAL LLM work (see the tier block above).

        Falls back to the standard chain when the configured value names no
        usable model (empty after safety filtering, e.g. only banned entries),
        so a misconfigured tier can never leave a mechanical stage without a
        model.
        """
        banned = {m.strip() for m in self.qwen_banned_models.split(",") if m.strip()}
        seen: List[str] = []
        for m in (self.accounting_fast_model_chain or "").split(","):
            m = m.strip()
            # "standard" is the documented KILL SWITCH: an empty string cannot
            # serve as one, because the deploy validator treats a present-but-
            # empty environment variable as unset (serverless platforms
            # materialize optional variables as empty strings), so
            # ACCOUNTING_FAST_MODEL_CHAIN="" would just restore the default.
            if not m or m.lower() == "standard" or m in banned or m in seen:
                continue
            seen.append(m)
        return seen or self.qwen_chain_list

    @property
    def qwen_vision_chain_list(self) -> List[str]:
        """Ordered, de-duplicated vision Qwen chain with banned models removed."""
        banned = {m.strip() for m in self.qwen_banned_models.split(",") if m.strip()}
        seen: List[str] = []
        for m in self.qwen_vision_model_chain.split(","):
            m = m.strip()
            if m and m not in banned and m not in seen:
                seen.append(m)
        return seen

    @property
    def qwen_banned_set(self) -> set:
        return {m.strip() for m in self.qwen_banned_models.split(",") if m.strip()}

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton settings instance."""
    return Settings()
