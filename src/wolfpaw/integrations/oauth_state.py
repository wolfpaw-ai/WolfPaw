"""Shared state-token issuance for OAuth authorization-code flows.

Mirrors `channels/telegram_tokens.py` exactly in shape — single-use,
TTL-bound, plaintext-in-URL / SHA-256-on-disk — but lives in its own
``integration_state_tokens`` table because integrations aren't
channels (no `channel` enum entry would fit Dropbox or Notion).

Used by Dropbox / Notion / Microsoft / (later) Google OAuth flows:

  1. App route (e.g. `GET /integrations/dropbox/install-url`) calls
     :func:`issue` to mint a fresh token, embeds it as `state` in the
     provider's authorize URL, and redirects.
  2. Provider redirects back to our callback route with `?code=...&state=...`.
  3. Callback route calls :func:`consume` to validate the state token
     atomically + recover the bound Wolfpaw user_id.
  4. Callback exchanges the OAuth code for tokens against the provider
     and writes them into the provider-specific tokens table.

Token lifetime is short (default 15 min). Single-use prevents replay
if the redirect URL leaks. Provider mismatch (token issued for Notion
but consumed at the Dropbox callback) surfaces as ``StateTokenError``
so a misconfigured callback URL can't accidentally complete an OAuth
flow against the wrong provider.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg


def _hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def new_state_token() -> tuple[str, str]:
    plain = secrets.token_urlsafe(32)
    return plain, _hash(plain)


async def issue(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    provider: str,
    ttl_minutes: int,
) -> str:
    """Mint a state token, persist its hash, return the plaintext that
    goes into the provider's authorize-URL `state` parameter."""
    plain, hashed = new_state_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    await conn.execute(
        "INSERT INTO integration_state_tokens"
        " (user_id, provider, token_hash, expires_at)"
        " VALUES ($1, $2, $3, $4)",
        user_id, provider, hashed, expires_at,
    )
    return plain


class StateTokenError(Exception):
    """Validation failure: invalid, expired, already used, or
    cross-provider mismatch."""


async def consume(
    conn: asyncpg.Connection,
    *,
    plain: str,
    provider: str,
) -> UUID:
    """Validate + mark used atomically. Returns the Wolfpaw user_id
    the token was bound to. Raises :class:`StateTokenError` on any
    invalidity — including a token minted for a different provider
    being consumed at the wrong callback."""
    hashed = _hash(plain)
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, user_id, provider, expires_at, used_at"
            "  FROM integration_state_tokens"
            " WHERE token_hash = $1"
            "   FOR UPDATE",
            hashed,
        )
        if row is None:
            raise StateTokenError("invalid state token")
        if row["provider"] != provider:
            raise StateTokenError("state token is for a different provider")
        if row["used_at"] is not None:
            raise StateTokenError("state token already used")
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise StateTokenError("state token expired")
        await conn.execute(
            "UPDATE integration_state_tokens SET used_at = NOW()"
            " WHERE id = $1",
            row["id"],
        )
        return row["user_id"]
