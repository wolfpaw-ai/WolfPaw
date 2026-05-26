"""DAO for `microsoft_links` — per-user Microsoft Graph OAuth tokens
(step 32). Same shape as the Dropbox DAO (refresh tokens, expiry) plus
``tenant_id`` for Microsoft's tenant-scoped APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class MicrosoftLink:
    user_id: UUID
    access_token: str
    refresh_token: str
    expires_at: datetime
    tenant_id: str | None
    account_id: str | None
    scope: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _row_to_link(row: asyncpg.Record) -> MicrosoftLink:
    return MicrosoftLink(
        user_id=row["user_id"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=row["expires_at"],
        tenant_id=row["tenant_id"],
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
    tenant_id: str | None,
    account_id: str | None,
    scope: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO microsoft_links
            (user_id, access_token, refresh_token, expires_at,
             tenant_id, account_id, scope, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                expires_at = EXCLUDED.expires_at,
                tenant_id = EXCLUDED.tenant_id,
                account_id = EXCLUDED.account_id,
                scope = EXCLUDED.scope,
                updated_at = NOW()
        """,
        user_id, access_token, refresh_token, expires_at,
        tenant_id, account_id, scope,
    )


async def update_tokens(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    access_token: str,
    expires_at: datetime,
    refresh_token: str | None = None,
) -> None:
    if refresh_token is None:
        await conn.execute(
            "UPDATE microsoft_links"
            "   SET access_token = $2, expires_at = $3, updated_at = NOW()"
            " WHERE user_id = $1",
            user_id, access_token, expires_at,
        )
    else:
        await conn.execute(
            "UPDATE microsoft_links"
            "   SET access_token = $2, expires_at = $3,"
            "       refresh_token = $4, updated_at = NOW()"
            " WHERE user_id = $1",
            user_id, access_token, expires_at, refresh_token,
        )


async def get(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> MicrosoftLink | None:
    row = await conn.fetchrow(
        "SELECT user_id, access_token, refresh_token, expires_at,"
        "       tenant_id, account_id, scope, created_at, updated_at"
        "  FROM microsoft_links WHERE user_id = $1",
        user_id,
    )
    return _row_to_link(row) if row else None


async def delete(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> bool:
    result = await conn.execute(
        "DELETE FROM microsoft_links WHERE user_id = $1", user_id,
    )
    return result.endswith(" 1")
