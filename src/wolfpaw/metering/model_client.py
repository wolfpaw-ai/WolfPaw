"""Central wrapper every agent uses to call a Claude model.

Order of operations:
    1. Enforcer.check_can_spend     (no-op in v1; raises OverCap from step 24)
    2. Trace-sink run context       (writes model_call_logs; no-op if disabled)
    3. anthropic.messages.create
    4. Cost computation via `pricing.get_active_price` + `compute_cost_cents`
    5. token_usage row via `recorder.record_usage`
    6. Return ModelCallResult

Steps 3–5 all run *inside* the trace context, so a failure at any of them —
not just an Anthropic error — is captured as an errored run. `token_usage`
still only records successful calls; `model_call_logs` records every attempt.

The Anthropic client is injectable so tests can pass a fake without making
real API calls. `get_model_client()` builds the production singleton from
config (lazy — errors only if used without an API key).
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.metering.enforcer import Enforcer, get_enforcer
from wolfpaw.metering.pricing import compute_cost_cents, get_active_price
from wolfpaw.metering.recorder import record_usage
from wolfpaw.metering.trace_sink import TraceSink, get_trace_sink
from wolfpaw.metering.types import ModelCallResult, TokenCounts
from wolfpaw.tracing import get_logger

log = get_logger()


class _AnthropicLike(Protocol):
    """Subset of `anthropic.AsyncAnthropic` we depend on. Lets tests pass a fake."""

    messages: Any


def _extract_usage(raw: Any) -> TokenCounts:
    """Normalize the Anthropic SDK's `usage` block into our `TokenCounts`."""
    u = getattr(raw, "usage", None)
    if u is None:
        return TokenCounts()
    return TokenCounts(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _extract_text(raw: Any) -> str:
    """Pull the joined text from Anthropic's content blocks. Tool-use blocks
    are skipped here; callers that need them grab `raw.content` directly."""
    pieces: list[str] = []
    for block in getattr(raw, "content", []) or []:
        if getattr(block, "type", None) == "text":
            pieces.append(getattr(block, "text", ""))
    return "".join(pieces)


def _trace_params(create_kwargs: dict[str, Any]) -> dict[str, Any]:
    """The knobs worth keeping on the trace row — everything except the two
    bulky fields already stored in their own columns (`messages`, `system`)
    and the tool schemas, which are large, static, and identical across every
    call by a given agent. Tool *names* are kept because "which tools was this
    call offered" is a real debugging question."""
    skip = {"messages", "system", "model", "tools"}
    params = {k: v for k, v in create_kwargs.items() if k not in skip}
    tools = create_kwargs.get("tools")
    if isinstance(tools, list):
        params["tool_names"] = [
            t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
            for t in tools
        ]
    return params


class ModelClient:
    def __init__(
        self,
        *,
        anthropic: _AnthropicLike,
        enforcer: Enforcer | None = None,
        traces: TraceSink | None = None,
    ) -> None:
        self._anthropic = anthropic
        self._enforcer = enforcer or get_enforcer()
        self._traces = traces or get_trace_sink()

    async def call(
        self,
        *,
        user_id: UUID,
        agent: str,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 1024,
        prompt_version_id: UUID | None = None,
        task_id: UUID | None = None,
        request_id: str | None = None,
        attempt: int = 1,
        **kwargs: Any,
    ) -> ModelCallResult:
        await self._enforcer.check_can_spend(user_id)

        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            **kwargs,
        }
        if system is not None:
            create_kwargs["system"] = system

        async with self._traces.trace(
            user_id=user_id,
            agent=agent,
            model=model,
            messages=messages,
            system=system,
            params=_trace_params(create_kwargs),
            prompt_version_id=prompt_version_id,
            task_id=task_id,
            request_id=request_id,
            attempt=attempt,
        ) as run:
            raw = await self._anthropic.messages.create(**create_kwargs)
            text = _extract_text(raw)
            run.mark_response(raw, text=text)

            usage = _extract_usage(raw)
            async with acquire() as conn:
                price = await get_active_price(conn, model)
            if price is None:
                log.warning("model.price.missing", model=model)
                cost_cents = 0
            else:
                cost_cents = compute_cost_cents(price, usage)

            usage_id = await record_usage(
                user_id=user_id,
                agent=agent,
                model=model,
                usage=usage,
                cost_cents=cost_cents,
                prompt_version_id=prompt_version_id,
                task_id=task_id,
                request_id=request_id,
            )
            run.mark_cost(
                usage=usage, cost_cents=cost_cents, token_usage_id=usage_id
            )

            return ModelCallResult(
                text=text,
                usage=usage,
                cost_cents=cost_cents,
                model=model,
                raw=raw,
            )


_client: ModelClient | None = None


def get_model_client() -> ModelClient:
    """Build the production singleton lazily from config. Raises if the
    Anthropic API key is not set — tests should construct ModelClient
    directly with a fake."""
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "WOLFPAW_ANTHROPIC_API_KEY is unset — set it or build a"
            " ModelClient with an injected anthropic client for tests."
        )
    # Lazy import: anthropic is a base dep but we don't pay import cost until used.
    from anthropic import AsyncAnthropic

    _client = ModelClient(anthropic=AsyncAnthropic(api_key=settings.anthropic_api_key))
    return _client


def reset_model_client() -> None:
    """Test/dev hook to drop the cached client (e.g. after settings changes)."""
    global _client
    _client = None
