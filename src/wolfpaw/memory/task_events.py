"""Append-only DAO over `task_events`.

Step 14 ships the bare minimum: a single `append_event` call. Tasks
lifecycle (step 15) will add an event reader (`fetch_for_task`) and the
status-transition emitters (`pending` → `running` → … → `completed`).

`task_id` is nullable starting in migration 005 because the Post-Evaluator
emits scoring events even for plans that aren't yet wrapped in a Task.
The originating plan's id is carried in `content` so we can always join
back to the plan even when `task_id IS NULL`.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg


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
