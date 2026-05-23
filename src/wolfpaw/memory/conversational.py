"""Conversational memory — `threads` + `messages` access.

Step 10 ships the verbatim recent window. Tiered summaries
(`fetch_summaries`) and per-thread vector recall (`search_relevant`) land
in step 12.5. The interface is shaped to match what step 12.5 will fill
in so callers don't have to change.

Persistence policy: we store visible user turns and final assistant
responses only. Intermediate tool-call / tool-result blocks live
in-process during the agent loop and are not persisted to the messages
table. That keeps the recent-window small and useful as context for the
next turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

MessageRole = Literal["user", "assistant", "system", "tool"]
ChannelName = Literal["web", "telegram", "email", "slack"]


@dataclass(frozen=True)
class Message:
    id: UUID
    thread_id: UUID
    role: MessageRole
    content: str
    metadata: dict[str, Any]
    created_at: datetime


async def get_or_create_thread(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    channel: ChannelName,
    thread_id: UUID | None = None,
) -> UUID:
    """Return the existing thread (validated to belong to `user_id`) or
    create a fresh one for this channel."""
    if thread_id is not None:
        row = await conn.fetchrow(
            "SELECT id FROM threads WHERE id = $1 AND user_id = $2",
            thread_id, user_id,
        )
        if row is not None:
            return row["id"]
        # Stale or cross-user thread_id — silently fall through and create
        # a fresh thread rather than erroring. Avoids exposing whether the
        # id exists for some other user.
    new_id = await conn.fetchval(
        "INSERT INTO threads (user_id, channel) VALUES ($1, $2::channel)"
        " RETURNING id",
        user_id, channel,
    )
    return new_id


async def append(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    role: MessageRole,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Insert one row into `messages`. Embedding-on-append + compaction
    enqueue land in step 12.5; for now this is just the row write."""
    return await conn.fetchval(
        "INSERT INTO messages (thread_id, role, content, metadata)"
        " VALUES ($1, $2::message_role, $3, $4) RETURNING id",
        thread_id, role, content, metadata or {},
    )


async def fetch_recent(
    conn: asyncpg.Connection, *, thread_id: UUID, n: int = 20
) -> list[Message]:
    """Return the most recent `n` messages in chronological order
    (oldest first), capped at `n`."""
    rows = await conn.fetch(
        "SELECT id, thread_id, role::text AS role, content, metadata, created_at"
        "  FROM messages WHERE thread_id = $1"
        " ORDER BY created_at DESC, id DESC LIMIT $2",
        thread_id, n,
    )
    out = [
        Message(
            id=r["id"],
            thread_id=r["thread_id"],
            role=r["role"],
            content=r["content"],
            metadata=dict(r["metadata"] or {}),
            created_at=r["created_at"],
        )
        for r in rows
    ]
    out.reverse()
    return out
