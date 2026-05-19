"""User row creation + lookup. New users are seeded with a default
`tier = 'dev'` subscription and an empty user_profile."""

from __future__ import annotations

from uuid import UUID

import asyncpg


async def get_or_create_user_by_email(
    conn: asyncpg.Connection, email: str
) -> tuple[UUID, bool]:
    """Returns (user_id, created). `created` is True iff a new row was inserted.

    Runs inside its own transaction so users + user_profiles + subscriptions
    stay consistent for first-time sign-ups.
    """
    email = email.strip().lower()
    async with conn.transaction():
        existing = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1", email
        )
        if existing is not None:
            return existing, False
        user_id = await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            email,
        )
        await conn.execute(
            "INSERT INTO user_profiles (user_id) VALUES ($1)"
            " ON CONFLICT (user_id) DO NOTHING",
            user_id,
        )
        await conn.execute(
            "INSERT INTO subscriptions (user_id, tier, status, allowance_cents)"
            " VALUES ($1, 'dev', 'active',"
            "         (SELECT allowance_cents FROM tier_limits WHERE tier = 'dev'))"
            " ON CONFLICT (user_id) DO NOTHING",
            user_id,
        )
        return user_id, True
