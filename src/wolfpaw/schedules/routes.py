"""JSON HTTP API for schedules — read-only listing for the web app's
Scheduled tab.

    GET   /schedules                    — list user's active/paused schedules

User-scoped: the route passes `user_id` from the session and the DAO
filters on it, so one user can't read another's schedules. Cancellation
stays in chat (`cancel_schedule` tool) for now.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory import schedules as schedules_dao
from wolfpaw.memory.db import acquire

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _humanize_interval(seconds: int) -> str:
    """"every 90s" / "every 5m" / "every 2h" / "every 1d" — pick the
    largest whole unit that divides evenly, else fall back to seconds."""
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds % unit_seconds == 0:
            return f"every {seconds // unit_seconds}{suffix}"
    return f"every {seconds}s"


def _cadence(s: schedules_dao.Schedule) -> str:
    """One-line human summary of when a schedule fires, for the table."""
    if s.recurrence == "once":
        return "once"
    if s.recurrence == "interval" and s.interval_seconds:
        return _humanize_interval(s.interval_seconds)
    if s.recurrence == "cron" and s.cron_expr:
        return f"cron: {s.cron_expr} ({s.timezone})"
    return s.recurrence


class ScheduleResponse(BaseModel):
    id: str
    title: str | None
    instruction: str
    recurrence: str
    cadence: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    next_run_at: str | None
    last_run_at: str | None
    run_count: int
    max_runs: int | None
    channel: str | None
    status: str
    created_at: str | None

    @classmethod
    def from_dao(cls, s: schedules_dao.Schedule) -> "ScheduleResponse":
        return cls(
            id=str(s.id),
            title=s.title,
            instruction=s.instruction,
            recurrence=s.recurrence,
            cadence=_cadence(s),
            cron_expr=s.cron_expr,
            interval_seconds=s.interval_seconds,
            timezone=s.timezone,
            next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
            last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
            run_count=s.run_count,
            max_runs=s.max_runs,
            channel=s.channel,
            status=s.status,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )


@router.get("")
async def list_schedules(
    user_id: UUID = Depends(require_user_id),
    limit: int = 100,
) -> dict[str, list[ScheduleResponse]]:
    """List ALL the user's schedules, newest first — active, paused, done,
    and cancelled — each with its real status, mirroring the Tasks tab. The
    `status` field lets the client style terminal rows; fired one-shots
    (done) stay visible as history rather than vanishing."""
    limit = max(1, min(200, limit))
    async with acquire() as conn:
        rows = await schedules_dao.list_for_user(
            conn, user_id=user_id, limit=limit, include_terminal=True,
        )
    return {"schedules": [ScheduleResponse.from_dao(s) for s in rows]}
