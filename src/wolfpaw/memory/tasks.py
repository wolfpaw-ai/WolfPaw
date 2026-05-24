"""DAO over `tasks` — persistent units of long-running work.

State machine:
    pending → running → (blocked | awaiting_user) → running → completed
                                                     ↘ failed | cancelled

Step 15 ships the synchronous path: a single process picks up the task,
runs it inline, transitions through states, emits `task_events` rows for
each transition. The arq-driven async worker is a follow-up (deferred —
needs Redis + a separate worker process; sync execution covers the
correctness substrate for now).

The state-transition helpers are idempotent on terminal states: calling
`mark_completed` on an already-completed task is a no-op. Cancellation
respects this: you can't cancel a task that's already terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg

TaskStatus = Literal[
    "pending", "running", "blocked", "awaiting_user",
    "completed", "failed", "cancelled",
]
ChannelName = Literal["web", "telegram", "email", "slack"]

TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {"completed", "failed", "cancelled"}
)


@dataclass(frozen=True)
class Task:
    id: UUID
    user_id: UUID
    parent_task_id: UUID | None
    title: str
    description: str | None
    status: TaskStatus
    current_plan_id: UUID | None
    budget_cents: int | None
    spent_cents: int
    blocking_reason: str | None
    channel_for_completion: ChannelName | None
    schedule_pattern: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_active_at: datetime | None


def _row_to_task(row: asyncpg.Record) -> Task:
    return Task(
        id=row["id"],
        user_id=row["user_id"],
        parent_task_id=row["parent_task_id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        current_plan_id=row["current_plan_id"],
        budget_cents=row["budget_cents"],
        spent_cents=row["spent_cents"],
        blocking_reason=row["blocking_reason"],
        channel_for_completion=row["channel_for_completion"],
        schedule_pattern=row["schedule_pattern"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        last_active_at=row["last_active_at"],
    )


_SELECT_COLS = (
    "id, user_id, parent_task_id, title, description, status::text AS status,"
    " current_plan_id, budget_cents, spent_cents, blocking_reason,"
    " channel_for_completion::text AS channel_for_completion,"
    " schedule_pattern, created_at, started_at, completed_at, last_active_at"
)


async def create(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    title: str,
    description: str | None = None,
    channel_for_completion: ChannelName | None = None,
    parent_task_id: UUID | None = None,
    budget_cents: int | None = None,
    schedule_pattern: str | None = None,
) -> Task:
    """Insert a new task in `pending` status."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO tasks
            (user_id, parent_task_id, title, description,
             channel_for_completion, budget_cents, schedule_pattern)
        VALUES ($1, $2, $3, $4, $5::channel, $6, $7)
        RETURNING {_SELECT_COLS}
        """,
        user_id, parent_task_id, title, description,
        channel_for_completion, budget_cents, schedule_pattern,
    )
    assert row is not None
    return _row_to_task(row)


async def get_by_id(
    conn: asyncpg.Connection, *, user_id: UUID, task_id: UUID,
) -> Task | None:
    """Look up by id, scoped to user (avoids cross-user information leak)."""
    row = await conn.fetchrow(
        f"SELECT {_SELECT_COLS} FROM tasks"
        f" WHERE id = $1 AND user_id = $2",
        task_id, user_id,
    )
    return _row_to_task(row) if row else None


async def list_for_user(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    statuses: tuple[TaskStatus, ...] | None = None,
    limit: int = 50,
) -> list[Task]:
    if statuses:
        rows = await conn.fetch(
            f"SELECT {_SELECT_COLS} FROM tasks"
            f" WHERE user_id = $1 AND status::text = ANY($2::text[])"
            f" ORDER BY created_at DESC LIMIT $3",
            user_id, list(statuses), limit,
        )
    else:
        rows = await conn.fetch(
            f"SELECT {_SELECT_COLS} FROM tasks"
            f" WHERE user_id = $1"
            f" ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
    return [_row_to_task(r) for r in rows]


async def mark_started(
    conn: asyncpg.Connection, *, task_id: UUID,
) -> Task | None:
    row = await conn.fetchrow(
        f"""
        UPDATE tasks
           SET status = 'running'::task_status,
               started_at = COALESCE(started_at, NOW()),
               last_active_at = NOW(),
               blocking_reason = NULL
         WHERE id = $1 AND status::text NOT IN ('completed','failed','cancelled')
        RETURNING {_SELECT_COLS}
        """,
        task_id,
    )
    return _row_to_task(row) if row else None


async def mark_awaiting_user(
    conn: asyncpg.Connection, *, task_id: UUID, reason: str | None = None,
) -> Task | None:
    row = await conn.fetchrow(
        f"""
        UPDATE tasks
           SET status = 'awaiting_user'::task_status,
               blocking_reason = $2,
               last_active_at = NOW()
         WHERE id = $1 AND status::text NOT IN ('completed','failed','cancelled')
        RETURNING {_SELECT_COLS}
        """,
        task_id, reason,
    )
    return _row_to_task(row) if row else None


async def mark_blocked(
    conn: asyncpg.Connection, *, task_id: UUID, reason: str,
) -> Task | None:
    row = await conn.fetchrow(
        f"""
        UPDATE tasks
           SET status = 'blocked'::task_status,
               blocking_reason = $2,
               last_active_at = NOW()
         WHERE id = $1 AND status::text NOT IN ('completed','failed','cancelled')
        RETURNING {_SELECT_COLS}
        """,
        task_id, reason,
    )
    return _row_to_task(row) if row else None


async def mark_completed(
    conn: asyncpg.Connection, *, task_id: UUID,
) -> Task | None:
    row = await conn.fetchrow(
        f"""
        UPDATE tasks
           SET status = 'completed'::task_status,
               completed_at = NOW(),
               last_active_at = NOW(),
               blocking_reason = NULL
         WHERE id = $1 AND status::text NOT IN ('completed','failed','cancelled')
        RETURNING {_SELECT_COLS}
        """,
        task_id,
    )
    return _row_to_task(row) if row else None


async def mark_failed(
    conn: asyncpg.Connection, *, task_id: UUID, reason: str,
) -> Task | None:
    row = await conn.fetchrow(
        f"""
        UPDATE tasks
           SET status = 'failed'::task_status,
               completed_at = NOW(),
               last_active_at = NOW(),
               blocking_reason = $2
         WHERE id = $1 AND status::text NOT IN ('completed','failed','cancelled')
        RETURNING {_SELECT_COLS}
        """,
        task_id, reason,
    )
    return _row_to_task(row) if row else None


async def cancel(
    conn: asyncpg.Connection, *, user_id: UUID, task_id: UUID,
) -> Task | None:
    """Mark cancelled. Only succeeds if the task is non-terminal. Scoped
    to user_id so one user can't cancel another's task."""
    row = await conn.fetchrow(
        f"""
        UPDATE tasks
           SET status = 'cancelled'::task_status,
               completed_at = NOW(),
               last_active_at = NOW()
         WHERE id = $1 AND user_id = $2
           AND status::text NOT IN ('completed','failed','cancelled')
        RETURNING {_SELECT_COLS}
        """,
        task_id, user_id,
    )
    return _row_to_task(row) if row else None


async def attach_plan(
    conn: asyncpg.Connection, *, task_id: UUID, plan_id: UUID,
) -> None:
    await conn.execute(
        "UPDATE tasks SET current_plan_id = $2, last_active_at = NOW()"
        " WHERE id = $1",
        task_id, plan_id,
    )


_MAX_DEPTH_WALK = 16  # safety cap; production depth is bounded at 3 by step 16


async def get_depth(
    conn: asyncpg.Connection, *, task_id: UUID,
) -> int:
    """Count ancestors via `parent_task_id`. Root tasks have depth 0.

    Used by the Executor's subagent step to enforce the depth limit
    (3 levels) without an explicit `depth` column on `tasks`. The walk
    is capped at `_MAX_DEPTH_WALK` so a corrupt parent chain can't loop
    forever — anything beyond returns the cap value."""
    depth = 0
    current = task_id
    for _ in range(_MAX_DEPTH_WALK):
        parent = await conn.fetchval(
            "SELECT parent_task_id FROM tasks WHERE id = $1",
            current,
        )
        if parent is None:
            return depth
        depth += 1
        current = parent
    return depth
