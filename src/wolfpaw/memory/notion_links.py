"""DAO for `notion_links` — per-user Notion OAuth tokens (step 30).

Notion tokens don't expire and don't carry a refresh token, so the
shape is simpler than the Dropbox DAO: just store the access_token +
workspace identity. Re-installing for the same user replaces the row
outright via upsert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class NotionLink:
    user_id: UUID
    access_token: str
    workspace_id: str | None
    workspace_name: str | None
    workspace_icon: str | None = None
    bot_id: str | None = None
    owner: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _row_to_link(row: asyncpg.Record) -> NotionLink:
    owner = row["owner"]
    if isinstance(owner, str):
        owner = json.loads(owner)
    return NotionLink(
        user_id=row["user_id"],
        access_token=row["access_token"],
        workspace_id=row["workspace_id"],
        workspace_name=row["workspace_name"],
        workspace_icon=row["workspace_icon"],
        bot_id=row["bot_id"],
        owner=dict(owner or {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    access_token: str,
    workspace_id: str | None,
    workspace_name: str | None,
    workspace_icon: str | None,
    bot_id: str | None,
    owner: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO notion_links
            (user_id, access_token, workspace_id, workspace_name,
             workspace_icon, bot_id, owner, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET access_token = EXCLUDED.access_token,
                workspace_id = EXCLUDED.workspace_id,
                workspace_name = EXCLUDED.workspace_name,
                workspace_icon = EXCLUDED.workspace_icon,
                bot_id = EXCLUDED.bot_id,
                owner = EXCLUDED.owner,
                updated_at = NOW()
        """,
        user_id, access_token, workspace_id, workspace_name,
        workspace_icon, bot_id, json.dumps(owner),
    )


async def get(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> NotionLink | None:
    row = await conn.fetchrow(
        "SELECT user_id, access_token, workspace_id, workspace_name,"
        "       workspace_icon, bot_id, owner, created_at, updated_at"
        "  FROM notion_links WHERE user_id = $1",
        user_id,
    )
    return _row_to_link(row) if row else None


async def delete(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> bool:
    result = await conn.execute(
        "DELETE FROM notion_links WHERE user_id = $1", user_id,
    )
    return result.endswith(" 1")
