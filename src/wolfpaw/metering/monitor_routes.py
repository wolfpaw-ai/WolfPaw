"""JSON HTTP API behind the monitoring tab.

    GET /monitor/summary?window=24h        — health: error rate, latency, spend
    GET /monitor/traces?window=&errors=    — one row per inbound prompt
    GET /monitor/traces/{trace_id}         — every call in one trace, with payloads
    GET /monitor/errors?window=            — raw failure feed

All user-scoped from the session cookie. Reads only; the writer is
`trace_sink.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from wolfpaw.auth.deps import require_user_id
from wolfpaw.metering import monitor

router = APIRouter(prefix="/monitor", tags=["monitor"])


class AgentStatResponse(BaseModel):
    agent: str
    calls: int
    errors: int
    cost_cents: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None


class ErrorStatResponse(BaseModel):
    error_type: str
    error_message: str
    agent: str
    count: int
    last_seen: str


class SummaryResponse(BaseModel):
    window: str
    since: str
    calls: int
    errors: int
    error_rate: float
    cost_cents: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    by_agent: list[AgentStatResponse]
    top_errors: list[ErrorStatResponse]


class TraceResponse(BaseModel):
    trace_id: str
    started_at: str
    ended_at: str
    calls: int
    errors: int
    cost_cents: int
    total_latency_ms: int
    agents: list[str]
    task_id: str | None


class CallResponse(BaseModel):
    id: str
    run_id: str
    parent_run_id: str | None
    trace_id: str | None
    task_id: str | None
    agent: str
    model: str
    status: str
    attempt: int
    created_at: str
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

    @classmethod
    def from_row(cls, c: monitor.CallRow) -> "CallResponse":
        return cls(
            id=str(c.id),
            run_id=str(c.run_id),
            parent_run_id=str(c.parent_run_id) if c.parent_run_id else None,
            trace_id=c.trace_id,
            task_id=str(c.task_id) if c.task_id else None,
            agent=c.agent,
            model=c.model,
            status=c.status,
            attempt=c.attempt,
            created_at=c.created_at.isoformat(),
            latency_ms=c.latency_ms,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
            cost_cents=c.cost_cents,
            stop_reason=c.stop_reason,
            error_type=c.error_type,
            error_message=c.error_message,
            truncated=c.truncated,
            system_prompt=c.system_prompt,
            request_messages=c.request_messages,
            request_params=c.request_params,
            response_content=c.response_content,
            response_text=c.response_text,
        )


def _window(window: str) -> str:
    try:
        monitor.parse_window(window)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return window


@router.get("/summary", response_model=SummaryResponse)
async def get_summary(
    window: str = monitor.DEFAULT_WINDOW,
    user_id: UUID = Depends(require_user_id),
) -> SummaryResponse:
    s = await monitor.build_summary(user_id, _window(window))
    return SummaryResponse(
        window=s.window,
        since=s.since.isoformat(),
        calls=s.calls,
        errors=s.errors,
        error_rate=s.error_rate,
        cost_cents=s.cost_cents,
        p50_latency_ms=s.p50_latency_ms,
        p95_latency_ms=s.p95_latency_ms,
        # Explicit, not `vars(a)` — AgentStat is a slots dataclass and has no
        # __dict__ for vars() to read.
        by_agent=[
            AgentStatResponse(
                agent=a.agent,
                calls=a.calls,
                errors=a.errors,
                cost_cents=a.cost_cents,
                p50_latency_ms=a.p50_latency_ms,
                p95_latency_ms=a.p95_latency_ms,
            )
            for a in s.by_agent
        ],
        top_errors=[
            ErrorStatResponse(
                error_type=e.error_type,
                error_message=e.error_message,
                agent=e.agent,
                count=e.count,
                last_seen=e.last_seen.isoformat(),
            )
            for e in s.top_errors
        ],
    )


@router.get("/traces", response_model=list[TraceResponse])
async def get_traces(
    window: str = monitor.DEFAULT_WINDOW,
    errors: bool = False,
    agent: str | None = None,
    limit: int = Query(50, ge=1, le=monitor.MAX_LIMIT),
    user_id: UUID = Depends(require_user_id),
) -> list[TraceResponse]:
    rows = await monitor.list_traces(
        user_id, _window(window), only_errors=errors, agent=agent, limit=limit
    )
    return [
        TraceResponse(
            trace_id=r.trace_id,
            started_at=r.started_at.isoformat(),
            ended_at=r.ended_at.isoformat(),
            calls=r.calls,
            errors=r.errors,
            cost_cents=r.cost_cents,
            total_latency_ms=r.total_latency_ms,
            agents=r.agents,
            task_id=str(r.task_id) if r.task_id else None,
        )
        for r in rows
    ]


@router.get("/traces/{trace_id}", response_model=list[CallResponse])
async def get_trace(
    trace_id: str,
    user_id: UUID = Depends(require_user_id),
) -> list[CallResponse]:
    calls = await monitor.get_trace(user_id, trace_id)
    if not calls:
        # Also the response when the trace belongs to another user — the query
        # is user-scoped, so a wrong guess is indistinguishable from a miss.
        raise HTTPException(404, "trace not found")
    return [CallResponse.from_row(c) for c in calls]


@router.get("/errors", response_model=list[CallResponse])
async def get_errors(
    window: str = monitor.DEFAULT_WINDOW,
    limit: int = Query(50, ge=1, le=monitor.MAX_LIMIT),
    user_id: UUID = Depends(require_user_id),
) -> list[CallResponse]:
    rows = await monitor.recent_errors(user_id, _window(window), limit=limit)
    return [CallResponse.from_row(r) for r in rows]
