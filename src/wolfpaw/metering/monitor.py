"""Read-side queries over `model_call_logs` — what the monitoring tab shows.

Three views, in the order you'd actually use them when something is wrong:

1. :func:`build_summary` — "is it broken right now?" Call count, error rate,
   latency percentiles, spend, plus the same broken out per agent and the
   most common failure signatures.
2. :func:`list_traces` — "which requests were bad?" One row per `trace_id`
   (i.e. per inbound prompt), with its call count, errors, cost and duration.
3. :func:`get_trace` — "what happened inside this one?" Every call in the
   trace, ordered, with full payloads.

Everything is user-scoped: `user_id` comes from the session and is applied in
SQL, never filtered client-side. All writes live in `trace_sink.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from wolfpaw.memory.db import acquire

# Windows the UI offers. Kept as a fixed set rather than free-form parsing so
# a caller can't ask for a 5-year scan of a partitioned table by accident.
WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_WINDOW = "24h"
MAX_LIMIT = 200


def parse_window(window: str) -> timedelta:
    if window not in WINDOWS:
        raise ValueError(
            f"unknown window {window!r} — expected one of {', '.join(WINDOWS)}"
        )
    return WINDOWS[window]


def _since(window: str) -> datetime:
    return datetime.now(timezone.utc) - parse_window(window)


@dataclass(frozen=True, slots=True)
class AgentStat:
    agent: str
    calls: int
    errors: int
    cost_cents: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None


@dataclass(frozen=True, slots=True)
class ErrorStat:
    error_type: str
    error_message: str
    agent: str
    count: int
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class Summary:
    window: str
    since: datetime
    calls: int
    errors: int
    cost_cents: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    by_agent: list[AgentStat] = field(default_factory=list)
    top_errors: list[ErrorStat] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return (self.errors / self.calls) if self.calls else 0.0


@dataclass(frozen=True, slots=True)
class TraceRow:
    trace_id: str
    started_at: datetime
    ended_at: datetime
    calls: int
    errors: int
    cost_cents: int
    total_latency_ms: int
    agents: list[str]
    task_id: UUID | None


@dataclass(frozen=True, slots=True)
class CallRow:
    """One model call. Payload fields are only populated by `get_trace` —
    the list views deliberately don't select them."""

    id: UUID
    run_id: UUID
    parent_run_id: UUID | None
    trace_id: str | None
    task_id: UUID | None
    agent: str
    model: str
    status: str
    attempt: int
    created_at: datetime
    latency_ms: int | None
    input_tokens: int
    output_tokens: int
    cost_cents: int
    stop_reason: str | None
    error_type: str | None
    error_message: str | None
    truncated: bool
    system_prompt: str | None = None
    request_messages: Any = None
    request_params: Any = None
    response_content: Any = None
    response_text: str | None = None


async def build_summary(user_id: UUID, window: str = DEFAULT_WINDOW) -> Summary:
    since = _since(window)
    async with acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT count(*)::int AS calls,
                   count(*) FILTER (WHERE status = 'error')::int AS errors,
                   coalesce(sum(cost_cents), 0)::int AS cost_cents,
                   percentile_disc(0.5) WITHIN GROUP (
                       ORDER BY latency_ms) FILTER (
                       WHERE latency_ms IS NOT NULL) AS p50,
                   percentile_disc(0.95) WITHIN GROUP (
                       ORDER BY latency_ms) FILTER (
                       WHERE latency_ms IS NOT NULL) AS p95
              FROM model_call_logs
             WHERE user_id = $1 AND created_at >= $2
            """,
            user_id, since,
        )
        agent_rows = await conn.fetch(
            """
            SELECT agent,
                   count(*)::int AS calls,
                   count(*) FILTER (WHERE status = 'error')::int AS errors,
                   coalesce(sum(cost_cents), 0)::int AS cost_cents,
                   percentile_disc(0.5) WITHIN GROUP (
                       ORDER BY latency_ms) FILTER (
                       WHERE latency_ms IS NOT NULL) AS p50,
                   percentile_disc(0.95) WITHIN GROUP (
                       ORDER BY latency_ms) FILTER (
                       WHERE latency_ms IS NOT NULL) AS p95
              FROM model_call_logs
             WHERE user_id = $1 AND created_at >= $2
             GROUP BY agent
             ORDER BY count(*) FILTER (WHERE status = 'error') DESC,
                      count(*) DESC
            """,
            user_id, since,
        )
        # Group by the message too, not just the type: "RuntimeError" alone
        # doesn't distinguish an overloaded upstream from a bad tool schema.
        error_rows = await conn.fetch(
            """
            SELECT error_type, agent,
                   coalesce(left(error_message, 200), '') AS error_message,
                   count(*)::int AS count,
                   max(created_at) AS last_seen
              FROM model_call_logs
             WHERE user_id = $1 AND created_at >= $2 AND status = 'error'
             GROUP BY error_type, agent, left(error_message, 200)
             ORDER BY count(*) DESC, max(created_at) DESC
             LIMIT 10
            """,
            user_id, since,
        )

    return Summary(
        window=window,
        since=since,
        calls=totals["calls"],
        errors=totals["errors"],
        cost_cents=totals["cost_cents"],
        p50_latency_ms=totals["p50"],
        p95_latency_ms=totals["p95"],
        by_agent=[
            AgentStat(
                agent=r["agent"], calls=r["calls"], errors=r["errors"],
                cost_cents=r["cost_cents"],
                p50_latency_ms=r["p50"], p95_latency_ms=r["p95"],
            )
            for r in agent_rows
        ],
        top_errors=[
            ErrorStat(
                error_type=r["error_type"] or "", agent=r["agent"],
                error_message=r["error_message"], count=r["count"],
                last_seen=r["last_seen"],
            )
            for r in error_rows
        ],
    )


async def list_traces(
    user_id: UUID,
    window: str = DEFAULT_WINDOW,
    *,
    only_errors: bool = False,
    agent: str | None = None,
    limit: int = 50,
) -> list[TraceRow]:
    """One row per `trace_id` — a trace is everything one inbound prompt did."""
    since = _since(window)
    limit = max(1, min(limit, MAX_LIMIT))
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT trace_id,
                   min(created_at) AS started_at,
                   max(created_at) AS ended_at,
                   count(*)::int AS calls,
                   count(*) FILTER (WHERE status = 'error')::int AS errors,
                   coalesce(sum(cost_cents), 0)::int AS cost_cents,
                   coalesce(sum(latency_ms), 0)::int AS total_latency_ms,
                   array_agg(DISTINCT agent) AS agents,
                   max(task_id::text) AS task_id
              FROM model_call_logs
             WHERE user_id = $1
               AND created_at >= $2
               AND trace_id IS NOT NULL
               AND ($3::text IS NULL OR agent = $3)
             GROUP BY trace_id
            HAVING NOT $4::boolean
                OR count(*) FILTER (WHERE status = 'error') > 0
             ORDER BY min(created_at) DESC
             LIMIT $5
            """,
            user_id, since, agent, only_errors, limit,
        )
    return [
        TraceRow(
            trace_id=r["trace_id"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            calls=r["calls"],
            errors=r["errors"],
            cost_cents=r["cost_cents"],
            total_latency_ms=r["total_latency_ms"],
            agents=sorted(r["agents"] or []),
            task_id=UUID(r["task_id"]) if r["task_id"] else None,
        )
        for r in rows
    ]


async def get_trace(user_id: UUID, trace_id: str) -> list[CallRow]:
    """Every call in one trace, oldest first, with full payloads."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, run_id, parent_run_id, trace_id, task_id, agent, model,
                   status, attempt, created_at, latency_ms,
                   input_tokens, output_tokens, cost_cents, stop_reason,
                   error_type, error_message, truncated,
                   system_prompt, request_messages, request_params,
                   response_content, response_text
              FROM model_call_logs
             WHERE user_id = $1 AND trace_id = $2
             ORDER BY created_at, id
            """,
            user_id, trace_id,
        )
    return [_call_row(r) for r in rows]


async def recent_errors(
    user_id: UUID, window: str = DEFAULT_WINDOW, *, limit: int = 50
) -> list[CallRow]:
    """The raw failure feed, newest first. Payload columns are left out — this
    is a list to scan, and pulling prompts for 50 rows to render a table is
    wasted IO. Drill into a trace for the full picture."""
    since = _since(window)
    limit = max(1, min(limit, MAX_LIMIT))
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, run_id, parent_run_id, trace_id, task_id, agent, model,
                   status, attempt, created_at, latency_ms,
                   input_tokens, output_tokens, cost_cents, stop_reason,
                   error_type, error_message, truncated
              FROM model_call_logs
             WHERE user_id = $1 AND created_at >= $2 AND status = 'error'
             ORDER BY created_at DESC
             LIMIT $3
            """,
            user_id, since, limit,
        )
    return [_call_row(r) for r in rows]


def _call_row(r: Any) -> CallRow:
    """Build a CallRow from a record that may or may not carry payload columns."""
    def opt(key: str) -> Any:
        return r[key] if key in r.keys() else None

    return CallRow(
        id=r["id"],
        run_id=r["run_id"],
        parent_run_id=r["parent_run_id"],
        trace_id=r["trace_id"],
        task_id=r["task_id"],
        agent=r["agent"],
        model=r["model"],
        status=r["status"],
        attempt=r["attempt"],
        created_at=r["created_at"],
        latency_ms=r["latency_ms"],
        input_tokens=r["input_tokens"],
        output_tokens=r["output_tokens"],
        cost_cents=r["cost_cents"],
        stop_reason=r["stop_reason"],
        error_type=r["error_type"],
        error_message=r["error_message"],
        truncated=r["truncated"],
        system_prompt=opt("system_prompt"),
        request_messages=opt("request_messages"),
        request_params=opt("request_params"),
        response_content=opt("response_content"),
        response_text=opt("response_text"),
    )
