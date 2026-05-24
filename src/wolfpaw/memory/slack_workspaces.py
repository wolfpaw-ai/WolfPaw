"""DAO for `slack_workspaces` — per-workspace bot tokens.

Slack's OAuth handshake gives us a bot token (`xoxb-...`) plus the team
(workspace) id when an admin installs the app. The bot token is what we
pass as `Authorization: Bearer ...` on every `chat.postMessage`. We
store it once at install time and look it up by team_id on every inbound.

Tokens are stored verbatim in v1 (operators can encrypt the column at
the application layer in their own fork; doing it generically would
require a key-management story this repo doesn't have yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class SlackWorkspace:
    team_id: str
    team_name: str | None
    bot_user_id: str
    bot_token: str
    installed_by_user_id: UUID
    installed_at: datetime
    revoked_at: datetime | None


def _row_to_workspace(row: asyncpg.Record) -> SlackWorkspace:
    return SlackWorkspace(
        team_id=row["team_id"],
        team_name=row["team_name"],
        bot_user_id=row["bot_user_id"],
        bot_token=row["bot_token"],
        installed_by_user_id=row["installed_by_user_id"],
        installed_at=row["installed_at"],
        revoked_at=row["revoked_at"],
    )


async def upsert(
    conn: asyncpg.Connection,
    *,
    team_id: str,
    team_name: str | None,
    bot_user_id: str,
    bot_token: str,
    installed_by_user_id: UUID,
) -> SlackWorkspace:
    """Insert or replace the workspace row. Re-installing a workspace
    rotates the bot token + records the new installer; everything else
    on the row is overwritten too. `revoked_at` is cleared so a
    previously-revoked install becomes active again."""
    row = await conn.fetchrow(
        """
        INSERT INTO slack_workspaces
            (team_id, team_name, bot_user_id, bot_token, installed_by_user_id)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (team_id) DO UPDATE SET
            team_name = EXCLUDED.team_name,
            bot_user_id = EXCLUDED.bot_user_id,
            bot_token = EXCLUDED.bot_token,
            installed_by_user_id = EXCLUDED.installed_by_user_id,
            installed_at = NOW(),
            revoked_at = NULL
        RETURNING team_id, team_name, bot_user_id, bot_token,
                  installed_by_user_id, installed_at, revoked_at
        """,
        team_id, team_name, bot_user_id, bot_token, installed_by_user_id,
    )
    assert row is not None
    return _row_to_workspace(row)


async def get(
    conn: asyncpg.Connection, *, team_id: str,
) -> SlackWorkspace | None:
    """Look up by team_id. Returns None for unknown or revoked workspaces."""
    row = await conn.fetchrow(
        "SELECT team_id, team_name, bot_user_id, bot_token,"
        "       installed_by_user_id, installed_at, revoked_at"
        "  FROM slack_workspaces"
        " WHERE team_id = $1 AND revoked_at IS NULL",
        team_id,
    )
    return _row_to_workspace(row) if row else None


async def revoke(
    conn: asyncpg.Connection, *, team_id: str,
) -> None:
    """Soft-delete: mark the workspace revoked. The bot token stays in
    the row for audit but won't be returned by `get()`. A subsequent
    `upsert()` (re-install) reactivates."""
    await conn.execute(
        "UPDATE slack_workspaces SET revoked_at = NOW() WHERE team_id = $1",
        team_id,
    )
