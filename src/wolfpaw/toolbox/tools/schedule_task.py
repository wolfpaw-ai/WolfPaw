"""`schedule_task` — register a one-shot or recurring instruction to run later.

This is the agent's entry point for "remind me…", "every morning…",
"every 10 minutes check…". It writes one `schedules` row and returns
immediately — no blocking, no held worker. A per-minute dispatcher
(workers/jobs/dispatch_schedules.py) later turns due rows into Tasks.

The caller (the agent) is responsible for splitting the user's request
into the *cadence* (recurrence args) and the *work* (``instruction`` — a
self-contained imperative for ONE run, with the cadence removed and any
condition + "stay silent otherwise" made explicit). Resolve ambient
context (location, who "me" is) into ``context`` so the headless run never
has to ask.

Recurrence:
- ``once``     — give ``run_at`` (ISO 8601).
- ``interval`` — give ``interval_seconds`` (>= the configured minimum).
- ``cron``     — give ``cron_expr`` (5-field), evaluated in the timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from wolfpaw.config import get_settings
from wolfpaw.memory import schedules as schedules_dao
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)

_VALID_CHANNELS = {"telegram"}  # only channel wired for proactive outbound


def _parse_dt(raw: str, tz_name: str) -> datetime:
    """Parse an ISO-8601 string to an aware UTC datetime. A naive value is
    interpreted in ``tz_name`` (the schedule's timezone)."""
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"could not parse datetime {raw!r}: {e}") from e
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _resolve_timezone(user_id: UUID, override: str | None) -> str:
    if override:
        try:
            ZoneInfo(override)
            return override
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"invalid timezone {override!r}") from e
    from wolfpaw.persona import user_profile

    async with acquire() as conn:
        profile = await user_profile.get(conn, user_id=user_id)
    return profile.timezone if profile else "UTC"


@register_tool
class ScheduleTaskTool(Tool):
    name = "schedule_task"
    description = (
        "Schedule an instruction to run later — once, on an interval, or on a"
        " cron schedule. Use for reminders and recurring checks ('remind me at"
        " 8pm', 'every 10 minutes check the weather'). Split the user's"
        " request: put the cadence in the recurrence args and a self-contained"
        " single-run imperative in `instruction` (cadence removed, any"
        " 'message me if…' condition and 'otherwise stay silent' made"
        " explicit). Returns immediately; the run happens at the scheduled"
        " time."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": (
                    "Self-contained imperative for ONE run, cadence removed."
                ),
            },
            "recurrence": {
                "type": "string",
                "enum": ["once", "interval", "cron"],
            },
            "run_at": {
                "type": "string",
                "description": "ISO-8601 time for recurrence='once'.",
            },
            "interval_seconds": {
                "type": "integer",
                "description": "Seconds between runs for recurrence='interval'.",
            },
            "cron_expr": {
                "type": "string",
                "description": "5-field cron for recurrence='cron' (e.g. '0 8 * * *').",
            },
            "timezone": {
                "type": "string",
                "description": "IANA tz (defaults to the user's profile tz).",
            },
            "title": {"type": "string", "description": "Short human label."},
            "context": {
                "type": "object",
                "description": "Resolved params for the run (location, etc.).",
            },
            "channel": {
                "type": "string",
                "description": "Delivery channel; defaults to 'telegram'.",
            },
            "max_runs": {
                "type": "integer",
                "description": "Optional cap on total runs.",
            },
            "until": {
                "type": "string",
                "description": "Optional ISO-8601 end time for recurring schedules.",
            },
        },
        "required": ["instruction", "recurrence"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        settings = get_settings()
        instruction = inputs.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ToolError("`instruction` must be a non-empty string")

        recurrence = inputs.get("recurrence")
        if recurrence not in ("once", "interval", "cron"):
            raise ToolError("`recurrence` must be once|interval|cron")

        channel = (inputs.get("channel") or "telegram").lower()
        if channel not in _VALID_CHANNELS:
            raise ToolError(
                f"`channel` must be one of {sorted(_VALID_CHANNELS)}"
            )

        tz_name = await _resolve_timezone(ctx.user_id, inputs.get("timezone"))
        now = datetime.now(timezone.utc)

        cron_expr: str | None = None
        interval_seconds: int | None = None

        if recurrence == "once":
            run_at = inputs.get("run_at")
            if not run_at:
                raise ToolError("recurrence='once' requires `run_at`")
            next_run_at = _parse_dt(str(run_at), tz_name)
            if next_run_at <= now:
                raise ToolError("`run_at` must be in the future")

        elif recurrence == "interval":
            interval_seconds = inputs.get("interval_seconds")
            if not isinstance(interval_seconds, int) or interval_seconds <= 0:
                raise ToolError(
                    "recurrence='interval' requires positive `interval_seconds`"
                )
            if interval_seconds < settings.schedules_min_interval_seconds:
                raise ToolError(
                    "`interval_seconds` must be >= "
                    f"{settings.schedules_min_interval_seconds}"
                )
            run_at = inputs.get("run_at")
            next_run_at = (
                _parse_dt(str(run_at), tz_name) if run_at
                else schedules_dao.compute_next_run_at(
                    recurrence="interval", after=now,
                    interval_seconds=interval_seconds,
                )
            )

        else:  # cron
            cron_expr = inputs.get("cron_expr")
            if not cron_expr or not isinstance(cron_expr, str):
                raise ToolError("recurrence='cron' requires `cron_expr`")
            from croniter import croniter

            if not croniter.is_valid(cron_expr):
                raise ToolError(f"invalid cron expression {cron_expr!r}")
            next_run_at = schedules_dao.compute_next_run_at(
                recurrence="cron", after=now,
                cron_expr=cron_expr, timezone_name=tz_name,
            )

        until_raw = inputs.get("until")
        until = _parse_dt(str(until_raw), tz_name) if until_raw else None
        max_runs = inputs.get("max_runs")
        if max_runs is not None and (not isinstance(max_runs, int) or max_runs <= 0):
            raise ToolError("`max_runs` must be a positive integer")

        assert next_run_at is not None  # only 'once' can be None, handled above
        async with acquire() as conn:
            schedule = await schedules_dao.create(
                conn,
                user_id=ctx.user_id,
                instruction=instruction,
                recurrence=recurrence,  # type: ignore[arg-type]
                next_run_at=next_run_at,
                cron_expr=cron_expr,
                interval_seconds=interval_seconds,
                timezone_name=tz_name,
                context=inputs.get("context") or {},
                channel=channel,  # type: ignore[arg-type]
                title=inputs.get("title"),
                max_runs=max_runs,
                until=until,
            )

        return {
            "schedule_id": str(schedule.id),
            "recurrence": schedule.recurrence,
            "next_run_at": schedule.next_run_at.isoformat()
            if schedule.next_run_at else None,
            "timezone": schedule.timezone,
            "title": schedule.title,
        }
