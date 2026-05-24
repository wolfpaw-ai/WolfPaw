"""DAO over `channel_links` — per-user mapping to a channel-side identity.

Step 18 introduces Telegram (channel='telegram', external_id=Telegram user
id). Same shape will hold Slack workspace ids in step 25 and similar.

The (channel, external_id) UNIQUE constraint means: a single Telegram
account can link to at most one Wolfpaw user. A Wolfpaw user can have
many links (web, telegram, eventually slack).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from wolfpaw.memory.conversational import ChannelName


@dataclass(frozen=True)
class ChannelLink:
    id: UUID
    user_id: UUID
    channel: ChannelName
    external_id: str
    external_username: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


def _row_to_link(row: asyncpg.Record) -> ChannelLink:
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    return ChannelLink(
        id=row["id"],
        user_id=row["user_id"],
        channel=row["channel"],
        external_id=row["external_id"],
        external_username=row["external_username"],
        metadata=dict(meta or {}),
        created_at=row["created_at"],
    )


async def create(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    channel: ChannelName,
    external_id: str,
    external_username: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChannelLink:
    """Insert a new link. Raises asyncpg.UniqueViolationError if the
    (channel, external_id) pair is already linked to some user."""
    row = await conn.fetchrow(
        """
        INSERT INTO channel_links
            (user_id, channel, external_id, external_username, metadata)
        VALUES ($1, $2::channel, $3, $4, $5::jsonb)
        RETURNING id, user_id, channel::text AS channel, external_id,
                  external_username, metadata, created_at
        """,
        user_id, channel, external_id, external_username,
        json.dumps(metadata or {}),
    )
    assert row is not None
    return _row_to_link(row)


async def find_user(
    conn: asyncpg.Connection,
    *,
    channel: ChannelName,
    external_id: str,
) -> UUID | None:
    """Return the Wolfpaw user_id for a given channel identity, or None
    if not linked. The Telegram webhook calls this on every inbound."""
    return await conn.fetchval(
        "SELECT user_id FROM channel_links"
        " WHERE channel = $1::channel AND external_id = $2",
        channel, external_id,
    )


async def list_for_user(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> list[ChannelLink]:
    rows = await conn.fetch(
        "SELECT id, user_id, channel::text AS channel, external_id,"
        "       external_username, metadata, created_at"
        "  FROM channel_links WHERE user_id = $1"
        " ORDER BY created_at DESC",
        user_id,
    )
    return [_row_to_link(r) for r in rows]


async def delete(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    channel: ChannelName,
) -> int:
    """Remove all of a user's links for one channel (e.g. unlink Telegram).
    Returns the count deleted."""
    result = await conn.execute(
        "DELETE FROM channel_links"
        " WHERE user_id = $1 AND channel = $2::channel",
        user_id, channel,
    )
    # asyncpg returns "DELETE <n>"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
