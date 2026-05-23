"""Per-user `user_data_<uuid_hex>` schema management.

Each user gets a private schema where their `create_table` / `sql_query`
tools operate. The hex form of the UUID is a valid Postgres identifier
(letters + digits), and 32 chars + the `user_data_` prefix fits inside
the 63-char identifier limit.
"""

from __future__ import annotations

import re
from uuid import UUID

import asyncpg

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def user_data_schema(user_id: UUID) -> str:
    return f"user_data_{user_id.hex}"


def validate_identifier(name: str) -> str:
    """Lowercase, ASCII-only, identifier-style. Returns the validated name."""
    if not _IDENT_RE.match(name):
        raise ValueError(
            f"invalid identifier {name!r}: must match {_IDENT_RE.pattern}"
        )
    return name


def quote_ident(name: str) -> str:
    """Postgres-style double-quote escaping. Validate the input first."""
    return '"' + name.replace('"', '""') + '"'


async def ensure_schema(conn: asyncpg.Connection, user_id: UUID) -> str:
    """Create the per-user schema if missing; return the schema name."""
    schema = user_data_schema(user_id)
    # Schema name comes from validated hex UUID — safe to interpolate.
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    return schema
