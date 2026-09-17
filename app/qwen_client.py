"""
ERP AI Agent — Qwen (Alibaba Model Studio) Client — PRIMARY provider
=====================================================================
Handles all communication with Alibaba Cloud Qwen via the Model Studio
OpenAI-compatible endpoint.

Connection details come from the provisioned workspace (Ai Accountant):
  * OpenAI-compatible base : {QWEN_BASE_URL}  (default: workspace /compatible-mode/v1)
  * DashScope native base  : {QWEN_DASHSCOPE_URL} (unused; compatible mode preferred)
  * Model codes available on this workspace (LLM tab): qwen3.7-plus,
    qwen3.6-plus, qwen-max, qwen-plus-2025-07-28, qwen-mt-flash, and
    qwen3-vl-* thinking models. Only exact codes from that list are used.

Responsibilities (mirrors gemini_client.GeminiClient):
* Call the OpenAI-compatible /chat/completions API
* System instructions from the shared prompts module (Constitution)
* Inject only relevant runtime ERP context
* Native OpenAI-style tool/function calling with a manual loop
* Raise ProviderError on genuine provider failures (network/auth/5xx)

The client does NOT execute SQL, Python, or Supabase mutations — tools
are routed through the trusted tool_router → Validator → Services stack.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.models.schemas import AgentContext, ToolCall, ToolResult
from app.prompts import build_system_instructions, build_user_content, load_constitution
from app.tool_execution import execute_planned_tool_calls
from app.tool_router import get_gemini_tool_definitions

log = structlog.get_logger(__name__)


class ProviderError(Exception):
    """Raised for genuine AI-provider failures (network, auth, HTTP 5xx).

    The orchestrator uses this to decide whether a fallback is safe.
    Business/validation errors are NOT ProviderError — they travel inside
    ToolResult and must never trigger a provider fallback.
    """


class QwenClient:
    """Wrapper around the Qwen OpenAI-compatible API for the ERP agent.

    Exposes the same interface as GeminiClient so the orchestrator can
    dispatch to either provider without the agent knowing the difference.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.1,
        max_output_tokens: int = 2048,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ProviderError("Qwen API key is not configured (set QWEN_API_KEY)")
        if not base_url:
            raise ProviderError("Qwen base URL is not configured (set QWEN_BASE_URL)")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._timeout = httpx.Timeout(timeout_seconds, connect=15.0)

        # Shared prompt pipeline — identical to Gemini's.
        self._constitution = load_constitution()
        self._system_instructions = build_system_instructions(self._constitution)

        log.info(
            "qwen_client.initialised",
            model=self.model_name,
            base_url=self.base_url,
            constitution_chars=len(self._constitution),
        )

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def generate_with_tools(
        self,
        *,
        user_message: str,
        context: AgentContext,
        max_tool_iterations: int = 10,
        executor: Optional[Any] = None,
        excluded_tools: Optional[set] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a message to Qwen with tool-calling support.

        Same contract as GeminiClient.generate_with_tools: when *executor*
        is provided, tools run through it and results feed back into the
        conversation; otherwise proposed tool calls are returned unexecuted.

        ``excluded_tools`` is a live set maintained by the agent: tools the
        agent has deterministically disabled mid-execution (e.g. party
        creation once an existing party was resolved) are removed from the
        offered tool set on every subsequent iteration — the model cannot
        attempt what it is not offered.

        ``max_output_tokens`` overrides the client default for THIS request
        (Work Stream B tiered routing: simple lookups get a lighter budget).

        Returns ``{"text": str, "tool_calls": [ToolCall, ...],
        "tool_results": [ToolResult, ...], "iteration_count": int}``.
        """
        user_content = build_user_content(user_message, context)

        # Tool definitions (JSON-Schema from ai.tool_parameters — already
        # OpenAI-compatible shape, read-only metadata, no secrets).
        tool_defs = await get_gemini_tool_definitions()
        base_tools_payload = self._to_openai_tools(tool_defs)

        def _active_tools() -> List[Dict[str, Any]]:
            if not excluded_tools:
                return base_tools_payload
            return [
                t
                for t in base_tools_payload
                if t.get("function", {}).get("name") not in excluded_tools
            ]

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._system_instructions},
            {"role": "user", "content": user_content},
        ]

        all_tool_calls: List[ToolCall] = []
        tool_results: List[ToolResult] = []
        text_parts: List[str] = []
        iteration = 0

        while iteration < max_tool_iterations:
            iteration += 1
            response = await self._chat_completion(
                messages, _active_tools(), max_output_tokens=max_output_tokens
            )
            choice = self._first_choice(response)
            if choice is None:
                break
            message = choice.get("message", {}) or {}

            content = message.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)

            tool_calls_raw = message.get("tool_calls") or []
            if not tool_calls_raw:
                break  # Final text-only response — done

            iteration_tool_calls: List[ToolCall] = []
            for tc_raw in tool_calls_raw:
                fn = tc_raw.get("function", {}) or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (TypeError, ValueError):
                    args = {}
                tc = ToolCall(tool_name=fn.get("name", ""), arguments=args)
                iteration_tool_calls.append(tc)
                all_tool_calls.append(tc)

            if not executor:
                # Planning call — return proposals unexecuted.
                break

            # Execute tools via the trusted router and feed results back.
            # The assistant message with tool_calls MUST be appended first
            # (OpenAI-compatible contract), then one tool message per call.
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls_raw,
                }
            )
            # Work Stream A1: independent read-only calls run
            # CONCURRENTLY; mutations stay strictly sequential in plan
            # order (see app/tool_execution.py).
            result_data_list = await execute_planned_tool_calls(
                iteration_tool_calls, executor
            )
            for tc in iteration_tool_calls:
                log.info(
                    "qwen_client.tool_call",
                    tool=tc.tool_name,
                    iteration=iteration,
                )

            # Feed every result back as its own tool message, IN ORDER
            # (OpenAI-compatible contract).
            for tc, tc_raw, result_data in zip(
                iteration_tool_calls,
                tool_calls_raw,
                result_data_list,
            ):
                tool_results.append(
                    ToolResult(
                        tool_name=tc.tool_name,
                        success=(
                            result_data.get("success", True)
                            if isinstance(result_data, dict)
                            else True
                        ),
                        data=result_data if isinstance(result_data, dict) else None,
                        error=(
                            result_data.get("error")
                            if isinstance(result_data, dict)
                            else None
                        ),
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_raw.get("id", ""),
                        "content": json.dumps(
                            result_data if isinstance(result_data, dict)
                            else {"data": str(result_data)},
                            default=str,
                        ),
                    }
                )

        result_text = "\n".join(text_parts) if text_parts else ""
        return {
            "text": result_text,
            "tool_calls": all_tool_calls,
            "tool_results": tool_results,
            "iteration_count": iteration,
        }

    async def generate_text(
        self,
        *,
        prompt: str,
        context: Optional[AgentContext] = None,
    ) -> str:
        """Simple text generation without tool-calling."""
        content = (
            build_user_content(prompt, context) if context else prompt
        )
        messages = [
            {"role": "system", "content": self._system_instructions},
            {"role": "user", "content": content},
        ]
        response = await self._chat_completion(messages, None)
        choice = self._first_choice(response)
        if not choice:
            return ""
        msg = choice.get("message", {}) or {}
        return msg.get("content") or ""

    async def chat_raw(
        self,
        *,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Raw chat completion for specialised pipelines (e.g. vision
        extraction). The caller owns the message contract; this client
        only transports (with the standard bounded retry + ProviderError
        semantics so callers can fall back to the next vision model).
        """
        response = await self._chat_completion(messages, None)
        choice = self._first_choice(response)
        if not choice:
            raise ProviderError(f"{self.model_name} returned no choices")
        msg = choice.get("message", {}) or {}
        return msg.get("content") or ""

    async def health_check(self) -> Dict[str, Any]:
        """Cheap liveness probe — a 1-token chat completion.

        Returns ``{"ok": bool, "detail": str}``. Never raises.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": self.model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0,
                    },
                )
            if resp.status_code == 200:
                return {"ok": True, "detail": f"{self.model_name} reachable"}
            return {
                "ok": False,
                "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        except Exception as exc:  # noqa: BLE001 — health check must not raise
            return {"ok": False, "detail": str(exc)[:200]}

    # -------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools_payload: Optional[List[Dict[str, Any]]],
        max_output_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_output_tokens or self.max_output_tokens,
        }
        if tools_payload:
            payload["tools"] = tools_payload
            payload["tool_choice"] = "auto"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("qwen_client.transport_error", error=str(exc))
            raise ProviderError(f"Qwen endpoint unreachable: {exc}") from exc

        if resp.status_code != 200:
            log.warning(
                "qwen_client.http_error",
                status=resp.status_code,
                body=resp.text[:500],
            )
            raise ProviderError(
                f"Qwen API error HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"Qwen returned invalid JSON: {exc}") from exc

    @staticmethod
    def _first_choice(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        choices = response.get("choices") or []
        return choices[0] if choices else None

    @staticmethod
    def _to_openai_tools(
        tool_defs: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert our tool definitions to OpenAI function declarations.

        ai.tool_parameters stores JSON-Schema-ish types (string, integer,
        number, boolean, array, object) which map 1:1 to OpenAI's schema —
        no conversion beyond wrapping is required.
        """
        tools: List[Dict[str, Any]] = []
        for td in tool_defs:
            fn: Dict[str, Any] = {
                "name": td["name"],
                "description": td.get("description", ""),
            }
            params = td.get("parameters")
            # Only include a parameters schema when the tool actually has
            # parameters — mirrors the Gemini convention.
            if params and params.get("properties"):
                fn["parameters"] = params
            tools.append({"type": "function", "function": fn})
        return tools
