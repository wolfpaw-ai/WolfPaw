"""DAO for `integration_links` — generic per-user credentials for
integrations built on the integration framework.

`credentials` is provider-defined JSON; `display` holds non-secret facts
the UI may show (key prefix, account label). Reconnecting replaces the
row via upsert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class IntegrationLink:
    user_id: UUID
    provider: str
    auth_kind: str
    credentials: dict[str, Any] = field(repr=False)  # never printed
    scopes: list[str] = field(default_factory=list)
    display: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


def _row_to_link(row: asyncpg.Record) -> IntegrationLink:
    return IntegrationLink(
        user_id=row["user_id"],
        provider=row["provider"],
        auth_kind=row["auth_kind"],
        credentials=_as_dict(row["credentials"]),
        scopes=list(row["scopes"] or []),
        display=_as_dict(row["display"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    provider: str,
    auth_kind: str,
    credentials: dict[str, Any],
    scopes: list[str] | None = None,
    display: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO integration_links
            (user_id, provider, auth_kind, credentials, scopes, display,
             expires_at, updated_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7, NOW())
        ON CONFLICT (user_id, provider) DO UPDATE
            SET auth_kind = EXCLUDED.auth_kind,
                credentials = EXCLUDED.credentials,
                scopes = EXCLUDED.scopes,
                display = EXCLUDED.display,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW()
        """,
        user_id, provider, auth_kind, json.dumps(credentials),
        list(scopes or []), json.dumps(display or {}), expires_at,
    )


async def get(
    conn: asyncpg.Connection, *, user_id: UUID, provider: str,
) -> IntegrationLink | None:
    row = await conn.fetchrow(
        "SELECT user_id, provider, auth_kind, credentials, scopes, display,"
        "       expires_at, created_at, updated_at"
        "  FROM integration_links WHERE user_id = $1 AND provider = $2",
        user_id, provider,
    )
    return _row_to_link(row) if row else None


async def delete(
    conn: asyncpg.Connection, *, user_id: UUID, provider: str,
) -> bool:
    result = await conn.execute(
        "DELETE FROM integration_links WHERE user_id = $1 AND provider = $2",
        user_id, provider,
    )
    return result.endswith(" 1")
