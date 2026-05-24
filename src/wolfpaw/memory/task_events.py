"""Append-only DAO over `task_events`.

Step 14 shipped the writer; step 15 adds the reader and the status-
transition convenience emitters used by the TaskService.

`task_id` is nullable starting in migration 005 because the Post-Evaluator
emits scoring events even for plans that aren't yet wrapped in a Task.
The originating plan's id is carried in `content` so we can always join
back to the plan even when `task_id IS NULL`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class TaskEvent:
    id: UUID
    task_id: UUID | None
    event_type: str
    content: dict[str, Any]
    created_at: datetime


def _row_to_event(row: asyncpg.Record) -> TaskEvent:
    content_raw = row["content"]
    if isinstance(content_raw, str):
        content_raw = json.loads(content_raw)
    return TaskEvent(
        id=row["id"],
        task_id=row["task_id"],
        event_type=row["event_type"],
        content=dict(content_raw or {}),
        created_at=row["created_at"],
    )


async def append_event(
    conn: asyncpg.Connection,
    *,
    task_id: UUID | None,
    event_type: str,
    content: dict[str, Any] | None = None,
) -> UUID:
    """Insert one task_events row. Returns the row id."""
    return await conn.fetchval(
        """
        INSERT INTO task_events (task_id, event_type, content)
        VALUES ($1, $2, $3::jsonb)
        RETURNING id
        """,
        task_id,
        event_type,
        json.dumps(content or {}),
    )


async def fetch_for_task(
    conn: asyncpg.Connection,
    *,
    task_id: UUID,
    limit: int = 100,
) -> list[TaskEvent]:
    """Return events for a task in chronological order (oldest first)."""
    rows = await conn.fetch(
        """
        SELECT id, task_id, event_type, content, created_at
          FROM task_events
         WHERE task_id = $1
         ORDER BY created_at, id
         LIMIT $2
        """,
        task_id, limit,
    )
    return [_row_to_event(r) for r in rows]
