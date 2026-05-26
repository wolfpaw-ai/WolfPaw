"""DAO for `dropbox_links` — per-user Dropbox OAuth tokens (step 29).

One row per Wolfpaw user, keyed by user_id. The integration uses the
**App folder** OAuth scope, so the access_token grants access only to
``/Apps/Wolfpaw/`` inside the user's Dropbox — nothing outside that
folder is reachable regardless of how the agent behaves.

Dropbox tokens are short-lived (~4h). ``expires_at`` carries the
absolute expiry; the client refreshes via ``refresh_token`` before
each API call when within a small buffer of expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class DropboxLink:
    user_id: UUID
    access_token: str
    refresh_token: str
    expires_at: datetime
    account_id: str | None
    scope: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _row_to_link(row: asyncpg.Record) -> DropboxLink:
    return DropboxLink(
        user_id=row["user_id"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=row["expires_at"],
        account_id=row["account_id"],
        scope=row["scope"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    account_id: str | None,
    scope: str | None,
) -> None:
    """Insert or replace the user's Dropbox token bundle. A re-install
    overwrites the previous row outright — no history kept; we trust
    Dropbox to be the source of truth."""
    await conn.execute(
        """
        INSERT INTO dropbox_links
            (user_id, access_token, refresh_token, expires_at,
             account_id, scope, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                expires_at = EXCLUDED.expires_at,
                account_id = EXCLUDED.account_id,
                scope = EXCLUDED.scope,
                updated_at = NOW()
        """,
        user_id, access_token, refresh_token, expires_at,
        account_id, scope,
    )


async def update_tokens(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    access_token: str,
    expires_at: datetime,
    refresh_token: str | None = None,
) -> None:
    """Update just the access_token + expiry after a successful refresh.
    Dropbox normally returns the same refresh_token on refresh, but
    occasionally rotates — pass ``refresh_token`` to overwrite when
    that happens."""
    if refresh_token is None:
        await conn.execute(
            "UPDATE dropbox_links"
            "   SET access_token = $2, expires_at = $3, updated_at = NOW()"
            " WHERE user_id = $1",
            user_id, access_token, expires_at,
        )
    else:
        await conn.execute(
            "UPDATE dropbox_links"
            "   SET access_token = $2, expires_at = $3,"
            "       refresh_token = $4, updated_at = NOW()"
            " WHERE user_id = $1",
            user_id, access_token, expires_at, refresh_token,
        )


async def get(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> DropboxLink | None:
    row = await conn.fetchrow(
        "SELECT user_id, access_token, refresh_token, expires_at,"
        "       account_id, scope, created_at, updated_at"
        "  FROM dropbox_links WHERE user_id = $1",
        user_id,
    )
    return _row_to_link(row) if row else None


async def delete(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> bool:
    """Disconnect Dropbox for this user. Returns True iff a row was
    deleted (False = already disconnected / never connected)."""
    result = await conn.execute(
        "DELETE FROM dropbox_links WHERE user_id = $1",
        user_id,
    )
    return result.endswith(" 1")
