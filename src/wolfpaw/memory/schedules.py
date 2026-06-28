"""DAO over `schedules` — recurring/one-shot triggers bound to an instruction.

A schedule is a template the dispatcher turns into Tasks (see
``workers/jobs/dispatch_schedules.py``). This module owns:

- the :class:`Schedule` dataclass + row mapping,
- CRUD scoped to ``user_id`` (so one user can't read/cancel another's),
- the concurrency-safe "claim due rows and advance them" operation the
  dispatcher calls each minute, and
- the pure :func:`compute_next_run_at` helper (no DB) that turns a
  recurrence spec into the next firing instant — unit-testable in
  isolation.

Recurrence kinds:
- ``once``     — fires at ``next_run_at`` then goes ``done``.
- ``interval`` — fires every ``interval_seconds``; if the worker was down
  and several slots are overdue, we fire once and skip ahead to the next
  future slot rather than replaying the backlog.
- ``cron``     — fires per ``cron_expr`` evaluated in ``timezone`` (so a
  "08:00 daily" stays 08:00 across DST).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from wolfpaw.memory.tasks import ChannelName

Recurrence = Literal["once", "interval", "cron"]
ScheduleStatus = Literal["active", "paused", "done", "cancelled"]


@dataclass(frozen=True)
class Schedule:
    id: UUID
    user_id: UUID
    instruction: str
    context: dict[str, Any]
    state: dict[str, Any]
    recurrence: Recurrence
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    run_count: int
    max_runs: int | None
    until: datetime | None
    channel: ChannelName | None
    status: ScheduleStatus
    title: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _loads(value: Any) -> dict[str, Any]:
    """JSONB normally arrives as a dict; tolerate a raw string (seen in
    prod) like the other DAOs do."""
    if isinstance(value, str):
        value = json.loads(value) if value else {}
    return dict(value or {})


def _row_to_schedule(row: asyncpg.Record) -> Schedule:
    return Schedule(
        id=row["id"],
        user_id=row["user_id"],
        instruction=row["instruction"],
        context=_loads(row["context"]),
        state=_loads(row["state"]),
        recurrence=row["recurrence"],
        cron_expr=row["cron_expr"],
        interval_seconds=row["interval_seconds"],
        timezone=row["timezone"] or "UTC",
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        run_count=row["run_count"],
        max_runs=row["max_runs"],
        until=row["until"],
        channel=row["channel"],
        status=row["status"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SELECT_COLS = (
    "id, user_id, instruction, context, state, recurrence, cron_expr,"
    " interval_seconds, timezone, next_run_at, last_run_at, run_count,"
    " max_runs, until, channel::text AS channel, status::text AS status,"
    " title, created_at, updated_at"
)


# --- pure scheduling math (no DB) ------------------------------------------


def compute_next_run_at(
    *,
    recurrence: Recurrence,
    after: datetime,
    cron_expr: str | None = None,
    interval_seconds: int | None = None,
    timezone_name: str = "UTC",
) -> datetime | None:
    """Return the next firing instant strictly after ``after`` (UTC-aware),
    or ``None`` for a one-shot (which has no "next").

    ``after`` must be timezone-aware. ``interval`` skips ahead past any
    overdue backlog so a long outage doesn't queue a burst of catch-up
    runs. ``cron`` is evaluated in ``timezone_name`` then normalized to UTC.
    """
    if after.tzinfo is None:
        raise ValueError("`after` must be timezone-aware")
    after = after.astimezone(timezone.utc)

    if recurrence == "once":
        return None

    if recurrence == "interval":
        if not interval_seconds or interval_seconds <= 0:
            raise ValueError("interval recurrence needs interval_seconds > 0")
        return after + timedelta(seconds=interval_seconds)

    if recurrence == "cron":
        if not cron_expr:
            raise ValueError("cron recurrence needs cron_expr")
        from croniter import croniter

        try:
            tz = ZoneInfo(timezone_name)
        except Exception:  # noqa: BLE001 — bad tz falls back to UTC
            tz = timezone.utc
        local_after = after.astimezone(tz)
        nxt = croniter(cron_expr, local_after).get_next(datetime)
        return nxt.astimezone(timezone.utc)

    raise ValueError(f"unknown recurrence: {recurrence!r}")


def next_after_fire(schedule: Schedule, *, now: datetime) -> tuple[datetime | None, ScheduleStatus]:
    """Given a schedule that just fired at ``now``, return its
    ``(next_run_at, status)``. Terminal when one-shot, ``max_runs`` reached,
    or the computed next time is past ``until``."""
    runs_after = schedule.run_count + 1
    if schedule.max_runs is not None and runs_after >= schedule.max_runs:
        return None, "done"

    nxt = compute_next_run_at(
        recurrence=schedule.recurrence,
        after=now,
        cron_expr=schedule.cron_expr,
        interval_seconds=schedule.interval_seconds,
        timezone_name=schedule.timezone,
    )
    if nxt is None:
        return None, "done"
    if schedule.until is not None and nxt > schedule.until:
        return None, "done"
    return nxt, "active"


# --- writes ----------------------------------------------------------------


async def create(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    instruction: str,
    recurrence: Recurrence,
    next_run_at: datetime,
    cron_expr: str | None = None,
    interval_seconds: int | None = None,
    timezone_name: str = "UTC",
    context: dict[str, Any] | None = None,
    channel: ChannelName | None = None,
    title: str | None = None,
    max_runs: int | None = None,
    until: datetime | None = None,
) -> Schedule:
    """Insert an active schedule with its first ``next_run_at`` precomputed
    by the caller (the tool computes it via :func:`compute_next_run_at`)."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO schedules
            (user_id, instruction, context, recurrence, cron_expr,
             interval_seconds, timezone, next_run_at, channel, title,
             max_runs, until)
        VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9::channel, $10,
                $11, $12)
        RETURNING {_SELECT_COLS}
        """,
        user_id, instruction, json.dumps(context or {}), recurrence,
        cron_expr, interval_seconds, timezone_name, next_run_at, channel,
        title, max_runs, until,
    )
    assert row is not None
    return _row_to_schedule(row)


async def get(
    conn: asyncpg.Connection, *, user_id: UUID, schedule_id: UUID,
) -> Schedule | None:
    row = await conn.fetchrow(
        f"SELECT {_SELECT_COLS} FROM schedules WHERE id = $1 AND user_id = $2",
        schedule_id, user_id,
    )
    return _row_to_schedule(row) if row else None


async def list_for_user(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    include_terminal: bool = False,
    limit: int = 100,
) -> list[Schedule]:
    """List a user's schedules, active/paused first. Terminal (done/
    cancelled) rows are excluded unless ``include_terminal``."""
    if include_terminal:
        rows = await conn.fetch(
            f"SELECT {_SELECT_COLS} FROM schedules WHERE user_id = $1"
            f" ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
    else:
        rows = await conn.fetch(
            f"SELECT {_SELECT_COLS} FROM schedules"
            f" WHERE user_id = $1 AND status IN ('active', 'paused')"
            f" ORDER BY next_run_at ASC NULLS LAST LIMIT $2",
            user_id, limit,
        )
    return [_row_to_schedule(r) for r in rows]


async def cancel(
    conn: asyncpg.Connection, *, user_id: UUID, schedule_id: UUID,
) -> Schedule | None:
    """Cancel a non-terminal schedule. Scoped to user_id. Clears
    ``next_run_at`` so the dispatcher never picks it up again."""
    row = await conn.fetchrow(
        f"""
        UPDATE schedules
           SET status = 'cancelled', next_run_at = NULL, updated_at = NOW()
         WHERE id = $1 AND user_id = $2 AND status IN ('active', 'paused')
        RETURNING {_SELECT_COLS}
        """,
        schedule_id, user_id,
    )
    return _row_to_schedule(row) if row else None


async def update_state(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    schedule_id: UUID,
    state: dict[str, Any],
) -> bool:
    """Replace a schedule's cross-run ``state`` blob. Returns True if a row
    matched (scoped to user_id). Used by scheduled runs to remember what
    they've already done (e.g. dedup notifications)."""
    result = await conn.execute(
        "UPDATE schedules SET state = $3::jsonb, updated_at = NOW()"
        " WHERE id = $1 AND user_id = $2",
        schedule_id, user_id, json.dumps(state or {}),
    )
    try:
        return int(result.split()[-1]) > 0
    except (ValueError, IndexError):
        return False


async def claim_and_advance_due(
    conn: asyncpg.Connection, *, now: datetime, limit: int,
) -> list[Schedule]:
    """Atomically claim up to ``limit`` due schedules and advance each to
    its next slot, returning the pre-advance snapshots for the dispatcher
    to spawn Tasks from.

    Concurrency: ``FOR UPDATE SKIP LOCKED`` inside a transaction means two
    dispatcher instances (or overlapping minutes) never claim the same row.
    Advancing ``next_run_at`` in the same transaction closes the
    double-fire window — once committed, the row won't be due again until
    its new time."""
    claimed: list[Schedule] = []
    async with conn.transaction():
        rows = await conn.fetch(
            f"""
            SELECT {_SELECT_COLS} FROM schedules
             WHERE status = 'active' AND next_run_at IS NOT NULL
               AND next_run_at <= $1
             ORDER BY next_run_at ASC
             LIMIT $2
             FOR UPDATE SKIP LOCKED
            """,
            now, limit,
        )
        for r in rows:
            sched = _row_to_schedule(r)
            claimed.append(sched)
            nxt, status = next_after_fire(sched, now=now)
            await conn.execute(
                "UPDATE schedules"
                "   SET next_run_at = $2, status = $3,"
                "       last_run_at = $4, run_count = run_count + 1,"
                "       updated_at = NOW()"
                " WHERE id = $1",
                sched.id, nxt, status, now,
            )
    return claimed
