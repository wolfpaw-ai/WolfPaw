"""Short-lived single-use tokens for the Telegram deep-link onboarding.

Mirrors the magic-link pattern (`auth.tokens` + `magic_link_tokens`):
the plaintext token is given to the user (embedded in a `t.me/<bot>?start=link_<token>`
URL), only its SHA-256 hash is stored. Verification checks signature,
expiry, and single-use atomically in a transaction.

The token only carries the user_id binding — when the user clicks the
deep link and the bot receives `/start link_<token>`, validation maps
the token to the Wolfpaw user_id so the bot can write the channel_links
row.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import asyncpg

from wolfpaw.memory.conversational import ChannelName


def _hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def new_link_token() -> tuple[str, str]:
    """Generate a fresh (plaintext, hash) pair. Plaintext goes into the
    deep-link URL; hash is what gets stored."""
    plain = secrets.token_urlsafe(32)
    return plain, _hash(plain)


async def issue(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    channel: ChannelName,
    ttl_minutes: int,
) -> str:
    """Mint a token, persist its hash, return the plaintext for the URL."""
    plain, hashed = new_link_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    await conn.execute(
        "INSERT INTO channel_link_tokens (user_id, channel, token_hash, expires_at)"
        " VALUES ($1, $2::channel, $3, $4)",
        user_id, channel, hashed, expires_at,
    )
    return plain


class LinkTokenError(Exception):
    """Validation failure (invalid, expired, or already used)."""


async def consume(
    conn: asyncpg.Connection,
    *,
    plain: str,
    channel: ChannelName,
) -> UUID:
    """Validate + mark used atomically. Returns the user_id the token was
    bound to. Raises LinkTokenError if the token is invalid, expired, or
    already redeemed."""
    hashed = _hash(plain)
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, user_id, expires_at, used_at, channel::text AS channel"
            "  FROM channel_link_tokens WHERE token_hash = $1 FOR UPDATE",
            hashed,
        )
        if row is None:
            raise LinkTokenError("invalid token")
        if row["channel"] != channel:
            raise LinkTokenError("token is for a different channel")
        if row["used_at"] is not None:
            raise LinkTokenError("token already used")
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise LinkTokenError("token expired")
        await conn.execute(
            "UPDATE channel_link_tokens SET used_at = NOW() WHERE id = $1",
            row["id"],
        )
        return row["user_id"]
