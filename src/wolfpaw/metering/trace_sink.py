"""Model-call trace sink — writes one `model_call_logs` row per attempt.

This replaces the LangSmith forwarder that used to sit at this seam. Same
shape (a context manager wrapping the Anthropic call, injected into
`ModelClient`), but the runs land in our own Postgres instead of a vendor's
API, so prompts and completions never leave the deployment.

The run model is deliberately the same one LangSmith uses, because it's the
right one: each call is a node carrying inputs, outputs, timing and error,
keyed by `run_id` with a `parent_run_id` pointing at whatever spawned it.
That's what lets a sub-agent's calls render as a tree under the executor call
that fanned them out rather than as a flat list.

Two rules this module holds to:

- **A trace write must never break a model call.** Every DB touch is wrapped;
  a failure here logs and moves on. Observability that can take down the thing
  it observes is worse than no observability.
- **Failed calls are the point.** The `finally` path writes a row whether the
  call succeeded, raised, or timed out — `token_usage` only ever sees the
  successes, which is precisely why failures have been invisible.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol
from uuid import UUID, uuid4

from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.metering.types import TokenCounts
from wolfpaw.tracing import get_logger, get_trace_id

log = get_logger()

# The currently-open run, so a nested model call can find its parent without
# every agent having to plumb a run id through its call signature. Same trick
# as `trace_id` in tracing.py.
_parent_run_ctx: ContextVar[UUID | None] = ContextVar("parent_run_id", default=None)


def get_parent_run_id() -> UUID | None:
    return _parent_run_ctx.get()


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of SDK objects into JSON-serializable data.

    Anthropic's blocks are pydantic models; test fakes are plain objects. We
    don't want either to be able to raise inside a logging path, so anything
    unrecognized degrades to its `repr`.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump(mode="json"))
        except Exception:  # noqa: BLE001 — never raise from a log path
            pass
    if hasattr(value, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return repr(value)


def _dump_capped(value: Any, max_bytes: int) -> tuple[str, bool]:
    """Serialize to JSON, clipped to `max_bytes`. Returns (json, truncated).

    When over budget the payload is replaced by a marker object rather than a
    sliced string, so the column always holds valid JSON — a half-cut JSON
    blob would make the whole row unreadable to the monitoring UI.
    """
    try:
        text = json.dumps(_jsonable(value))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"_unserializable": repr(exc)}), True
    if len(text.encode("utf-8")) <= max_bytes:
        return text, False
    preview = text[: max(0, max_bytes // 2)]
    return json.dumps({"_truncated": True, "_bytes": len(text), "_preview": preview}), True


@dataclass(slots=True)
class ModelRun:
    """Mutable handle for one in-flight call. The sink reads it on close."""

    run_id: UUID
    parent_run_id: UUID | None
    user_id: UUID
    agent: str
    model: str
    trace_id: str | None = None
    request_id: str | None = None
    task_id: UUID | None = None
    prompt_version_id: UUID | None = None
    attempt: int = 1

    system_prompt: str | None = None
    request_messages: Any = field(default_factory=list)
    request_params: dict[str, Any] = field(default_factory=dict)

    response_content: Any = None
    response_text: str | None = None
    stop_reason: str | None = None
    usage: TokenCounts = field(default_factory=TokenCounts)
    cost_cents: int = 0
    token_usage_id: UUID | None = None

    latency_ms: int | None = None
    status: str = "ok"
    error_type: str | None = None
    error_message: str | None = None

    _started_at: float = field(default_factory=time.perf_counter)

    def mark_response(self, raw: Any, *, text: str | None = None) -> None:
        """Stamp the response and freeze latency at the moment the model
        returned — before any pricing or DB work, so `latency_ms` measures the
        Anthropic round-trip and nothing else."""
        self.latency_ms = int((time.perf_counter() - self._started_at) * 1000)
        self.response_content = getattr(raw, "content", None)
        self.stop_reason = getattr(raw, "stop_reason", None)
        self.response_text = text

    def mark_cost(
        self, *, usage: TokenCounts, cost_cents: int, token_usage_id: UUID | None
    ) -> None:
        self.usage = usage
        self.cost_cents = cost_cents
        self.token_usage_id = token_usage_id

    def mark_error(self, exc: BaseException) -> None:
        if self.latency_ms is None:
            self.latency_ms = int((time.perf_counter() - self._started_at) * 1000)
        self.status = "error"
        self.error_type = type(exc).__name__
        self.error_message = str(exc)[:2000]


class TraceSink(Protocol):
    """The seam `ModelClient` depends on. Swap implementations to send runs
    somewhere else (a vendor, a file, /dev/null) without touching agents."""

    def trace(self, **kwargs: Any) -> Any: ...


class NullTraceSink:
    """No-op sink — used when tracing is disabled and by unit tests that
    don't want a database."""

    enabled = False

    @asynccontextmanager
    async def trace(self, **kwargs: Any) -> AsyncIterator[ModelRun]:
        run = ModelRun(
            run_id=uuid4(),
            parent_run_id=None,
            user_id=kwargs.get("user_id"),
            agent=kwargs.get("agent", ""),
            model=kwargs.get("model", ""),
        )
        try:
            yield run
        except BaseException as exc:
            run.mark_error(exc)
            raise


class PostgresTraceSink:
    """Writes runs to `model_call_logs`."""

    enabled = True

    def __init__(self, *, max_payload_bytes: int | None = None) -> None:
        self._max_bytes = (
            max_payload_bytes
            if max_payload_bytes is not None
            else get_settings().trace_payload_max_bytes
        )

    @asynccontextmanager
    async def trace(
        self,
        *,
        user_id: UUID,
        agent: str,
        model: str,
        messages: Any = None,
        system: str | None = None,
        params: dict[str, Any] | None = None,
        prompt_version_id: UUID | None = None,
        task_id: UUID | None = None,
        request_id: str | None = None,
        attempt: int = 1,
    ) -> AsyncIterator[ModelRun]:
        run = ModelRun(
            run_id=uuid4(),
            parent_run_id=_parent_run_ctx.get(),
            user_id=user_id,
            agent=agent,
            model=model,
            trace_id=get_trace_id(),
            request_id=request_id,
            task_id=task_id,
            prompt_version_id=prompt_version_id,
            attempt=attempt,
            system_prompt=system,
            request_messages=messages or [],
            request_params=params or {},
        )
        # Nested calls made while this one is open become its children.
        token = _parent_run_ctx.set(run.run_id)
        try:
            yield run
        except BaseException as exc:
            run.mark_error(exc)
            raise
        finally:
            _parent_run_ctx.reset(token)
            await self._write(run)

    async def _write(self, run: ModelRun) -> None:
        try:
            messages_json, t1 = _dump_capped(run.request_messages, self._max_bytes)
            content_json, t2 = _dump_capped(run.response_content, self._max_bytes)
            params_json, _ = _dump_capped(run.request_params, 8_000)
            system_prompt = run.system_prompt
            truncated = t1 or t2
            if system_prompt is not None and len(system_prompt) > self._max_bytes:
                system_prompt = system_prompt[: self._max_bytes]
                truncated = True
            async with acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO model_call_logs
                      (run_id, parent_run_id, trace_id, request_id, user_id,
                       task_id, token_usage_id, agent, model, prompt_version_id,
                       system_prompt, request_messages, request_params,
                       response_content, response_text, stop_reason,
                       input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, cost_cents, latency_ms, attempt,
                       status, error_type, error_message, truncated)
                    VALUES ($1, $2, $3, $4, $5,
                            $6, $7, $8, $9, $10,
                            $11, $12::jsonb, $13::jsonb,
                            $14::jsonb, $15, $16,
                            $17, $18, $19,
                            $20, $21, $22, $23,
                            $24, $25, $26, $27)
                    """,
                    run.run_id, run.parent_run_id, run.trace_id, run.request_id,
                    run.user_id, run.task_id, run.token_usage_id, run.agent,
                    run.model, run.prompt_version_id,
                    system_prompt, messages_json, params_json,
                    content_json, run.response_text, run.stop_reason,
                    run.usage.input_tokens, run.usage.output_tokens,
                    run.usage.cache_read_tokens, run.usage.cache_write_tokens,
                    run.cost_cents, run.latency_ms, run.attempt,
                    run.status, run.error_type, run.error_message, truncated,
                )
        except Exception as exc:  # noqa: BLE001
            # Never let a logging failure surface as a model-call failure.
            log.warning(
                "trace.write.failed",
                error=str(exc),
                agent=run.agent,
                model=run.model,
                run_id=str(run.run_id),
            )


def get_trace_sink() -> TraceSink:
    """Build the sink from config. Not cached: `trace_sink_enabled` is read
    per call site so tests can flip it without a reset hook."""
    settings = get_settings()
    if not settings.trace_sink_enabled:
        return NullTraceSink()
    return PostgresTraceSink(max_payload_bytes=settings.trace_payload_max_bytes)
