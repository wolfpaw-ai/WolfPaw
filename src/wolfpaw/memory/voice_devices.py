"""DAO over `voice_devices` — paired voice devices and their bearer tokens.

A device token is 32 random bytes (urlsafe), so a plain SHA-256 of it is a
sound stored form: there is nothing to brute-force the way there is with a
password. The plaintext is returned once, at pairing, and never again.

Each device also has a `channel_links` row (channel='voice', external_id =
the device id) so the usual "which channels is this user on" lookups see it.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class VoiceDevice:
    id: UUID
    user_id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


_COLUMNS = "id, user_id, name, created_at, last_seen_at, revoked_at"


def _row(row: asyncpg.Record) -> VoiceDevice:
    return VoiceDevice(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        revoked_at=row["revoked_at"],
    )


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def new_device_token() -> tuple[str, str]:
    """A fresh (plaintext, hash) pair. Plaintext goes to the device once."""
    plain = secrets.token_urlsafe(32)
    return plain, hash_token(plain)


async def create(
    conn: asyncpg.Connection, *, user_id: UUID, name: str,
) -> tuple[VoiceDevice, str]:
    """Insert a device and its channel_links row. Returns the device and the
    plaintext token (the only time it exists outside the device)."""
    plain, hashed = new_device_token()
    async with conn.transaction():
        row = await conn.fetchrow(
            f"INSERT INTO voice_devices (user_id, name, token_hash)"
            f" VALUES ($1, $2, $3) RETURNING {_COLUMNS}",
            user_id, name, hashed,
        )
        assert row is not None
        await conn.execute(
            "INSERT INTO channel_links"
            " (user_id, channel, external_id, external_username)"
            " VALUES ($1, 'voice'::channel, $2, $3)",
            user_id, str(row["id"]), name,
        )
    return _row(row), plain


async def find_by_token(
    conn: asyncpg.Connection, *, plain: str,
) -> VoiceDevice | None:
    """The active (not revoked) device for this token, or None."""
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM voice_devices"
        f" WHERE token_hash = $1 AND revoked_at IS NULL",
        hash_token(plain),
    )
    return _row(row) if row is not None else None


async def list_for_user(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> list[VoiceDevice]:
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM voice_devices"
        f" WHERE user_id = $1 AND revoked_at IS NULL ORDER BY created_at",
        user_id,
    )
    return [_row(r) for r in rows]


async def touch(conn: asyncpg.Connection, *, device_id: UUID) -> None:
    await conn.execute(
        "UPDATE voice_devices SET last_seen_at = NOW() WHERE id = $1",
        device_id,
    )


async def revoke(
    conn: asyncpg.Connection, *, user_id: UUID, device_id: UUID,
) -> bool:
    """Revoke one of the user's devices. Returns False if it isn't theirs or
    is already revoked."""
    async with conn.transaction():
        result = await conn.execute(
            "UPDATE voice_devices SET revoked_at = NOW()"
            " WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
            device_id, user_id,
        )
        if result.split()[-1] == "0":
            return False
        await conn.execute(
            "DELETE FROM channel_links"
            " WHERE channel = 'voice'::channel AND external_id = $1",
            str(device_id),
        )
    return True
