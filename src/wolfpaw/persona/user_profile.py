"""DAO for `user_profiles` — the per-user persona + preferences.

Schema (`001_init.sql`):
    user_profiles(user_id PK, version int, persona_md text, preferences jsonb,
                  timezone text, updated_at timestamptz)

Default row is created on first sign-in by `auth.users.get_or_create_user_by_email`
with `version=1, persona_md='', preferences={}, timezone='UTC'`.

`update(...)` bumps `version` on every change so `threads.user_profile_version`
remains a meaningful snapshot for procedural-memory scoping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class UserProfile:
    user_id: UUID
    version: int
    persona_md: str
    preferences: dict[str, Any] = field(default_factory=dict)
    timezone: str = "UTC"
    updated_at: datetime | None = None


# Returned when a user has no profile row yet (e.g. during tests that
# bypass the auth-creates-profile path). Keep the version at 0 so
# real rows always sort above it.
DEFAULT_PROFILE = UserProfile(
    user_id=UUID("00000000-0000-0000-0000-000000000000"),
    version=0,
    persona_md="",
    preferences={},
    timezone="UTC",
)


def _row_to_profile(row: asyncpg.Record) -> UserProfile:
    prefs = row["preferences"]
    if isinstance(prefs, str):
        prefs = json.loads(prefs)
    return UserProfile(
        user_id=row["user_id"],
        version=row["version"],
        persona_md=row["persona_md"] or "",
        preferences=dict(prefs or {}),
        timezone=row["timezone"] or "UTC",
        updated_at=row["updated_at"],
    )


async def get(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> UserProfile | None:
    row = await conn.fetchrow(
        "SELECT user_id, version, persona_md, preferences, timezone, updated_at"
        "  FROM user_profiles WHERE user_id = $1",
        user_id,
    )
    return _row_to_profile(row) if row else None


async def update(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    persona_md: str | None = None,
    preferences: dict[str, Any] | None = None,
    timezone: str | None = None,
) -> UserProfile:
    """Partial update — only the fields you pass are written. `version`
    always bumps so downstream version-stamped reads see a fresh
    snapshot. Returns the updated row.

    Raises if the user has no profile row (shouldn't happen — sign-in
    creates one — but catches programming errors fast)."""
    sets: list[str] = ["version = user_profiles.version + 1",
                       "updated_at = NOW()"]
    args: list[Any] = [user_id]
    if persona_md is not None:
        sets.append(f"persona_md = ${len(args) + 1}")
        args.append(persona_md)
    if preferences is not None:
        sets.append(f"preferences = ${len(args) + 1}::jsonb")
        args.append(json.dumps(preferences))
    if timezone is not None:
        sets.append(f"timezone = ${len(args) + 1}")
        args.append(timezone)

    row = await conn.fetchrow(
        f"""
        UPDATE user_profiles SET {", ".join(sets)}
         WHERE user_id = $1
        RETURNING user_id, version, persona_md, preferences, timezone, updated_at
        """,
        *args,
    )
    if row is None:
        raise LookupError(f"no user_profile row for user {user_id}")
    return _row_to_profile(row)
