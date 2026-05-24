"""JSON HTTP API for usage. Companion to the `/usage` slash command in
`metering.usage_report` — the React app uses this directly so it can
render tables / charts instead of monospace text."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from wolfpaw.auth.deps import require_user_id
from wolfpaw.metering.usage_report import (
    Scope,
    UsageReport,
    build_usage_report,
    parse_scope,
)

router = APIRouter(prefix="/usage", tags=["usage"])


class ModelRow(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    input_cost_cents: int
    output_cost_cents: int
    cache_cost_cents: int
    total_cost_cents: int


class AgentRow(BaseModel):
    agent: str
    cost_cents: int


class PeriodResponse(BaseModel):
    label: str
    start: str
    end: str
    models: list[ModelRow]
    by_agent: list[AgentRow]
    compute_cost_cents: int
    total_cost_cents: int


class UsageResponse(BaseModel):
    scope: str
    generated_at: str
    timezone: str
    include_by_agent: bool
    periods: list[PeriodResponse]

    @classmethod
    def from_report(cls, r: UsageReport) -> "UsageResponse":
        return cls(
            scope=r.scope,
            generated_at=r.generated_at.isoformat(),
            timezone=r.timezone,
            include_by_agent=r.include_by_agent,
            periods=[
                PeriodResponse(
                    label=p.label,
                    start=p.start.isoformat(),
                    end=p.end.isoformat(),
                    models=[
                        ModelRow(
                            model=m.model,
                            input_tokens=m.input_tokens,
                            output_tokens=m.output_tokens,
                            cache_read_tokens=m.cache_read_tokens,
                            cache_write_tokens=m.cache_write_tokens,
                            input_cost_cents=m.input_cost_cents,
                            output_cost_cents=m.output_cost_cents,
                            cache_cost_cents=m.cache_cost_cents,
                            total_cost_cents=m.total_cost_cents,
                        )
                        for m in p.models
                    ],
                    by_agent=[
                        AgentRow(agent=a.agent, cost_cents=a.cost_cents)
                        for a in p.by_agent
                    ],
                    compute_cost_cents=p.compute_cost_cents,
                    total_cost_cents=p.total_cost_cents,
                )
                for p in r.periods
            ],
        )


@router.get("", response_model=UsageResponse)
async def get_usage(
    scope: str = "default",
    user_id: UUID = Depends(require_user_id),
) -> UsageResponse:
    try:
        parsed: Scope = parse_scope(scope)
    except ValueError as e:
        raise HTTPException(400, str(e))
    report = await build_usage_report(user_id, parsed)
    return UsageResponse.from_report(report)
