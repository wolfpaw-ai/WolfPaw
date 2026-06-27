"""Per-user `user_data_<uuid_hex>` schema management.

Each user gets a private schema where their `create_table` / `sql_query`
tools operate. The hex form of the UUID is a valid Postgres identifier
(letters + digits), and 32 chars + the `user_data_` prefix fits inside
the 63-char identifier limit.
"""

from __future__ import annotations

import re
from typing import Any
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


# information_schema data_type → Postgres cast target. A string value bound
# to one of these columns (e.g. 'today', '2026-05-27') fails asyncpg's strict
# temporal binding. Build the placeholder as `$N::text::<type>` so asyncpg
# sends the value as text and Postgres parses it — a bare `$N::<type>` makes
# Postgres infer the param as the temporal type, and asyncpg rejects the str.
_TEMPORAL_CASTS = {
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
}


def temporal_cast(pg_data_type: str | None) -> str | None:
    """Cast target for a temporal column type, or None if not temporal."""
    return _TEMPORAL_CASTS.get(pg_data_type or "")


def value_placeholder(idx: int, value: Any, pg_data_type: str | None) -> str:
    """`$idx`, or `$idx::text::<type>` when binding a string to a temporal
    column so Postgres parses it instead of asyncpg rejecting the str."""
    cast = temporal_cast(pg_data_type)
    if cast and isinstance(value, str):
        return f"${idx}::text::{cast}"
    return f"${idx}"


async def ensure_schema(conn: asyncpg.Connection, user_id: UUID) -> str:
    """Create the per-user schema if missing; return the schema name."""
    schema = user_data_schema(user_id)
    # Schema name comes from validated hex UUID — safe to interpolate.
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    return schema


async def describe_columns(
    conn: asyncpg.Connection, schema: str, table_name: str,
) -> list[dict[str, object]]:
    """Return one dict per column: {name, type, not_null}. Empty if no
    such table. Caller validates `table_name` if it came from user input."""
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema, table_name,
    )
    return [
        {
            "name": r["column_name"],
            "type": r["data_type"],
            "not_null": r["is_nullable"] == "NO",
        }
        for r in rows
    ]


async def explain_column_error(
    conn: asyncpg.Connection, schema: str, table_name: str,
    pg_error: BaseException,
) -> str:
    """Build a hint that augments a Postgres column/relation error with
    the table's actual current columns. Useful for translating opaque
    asyncpg errors into something the agent can recover from on retry."""
    cols = await describe_columns(conn, schema, table_name)
    if not cols:
        return (
            f"{pg_error} — table {table_name!r} does not exist in the"
            " user's SQL workspace; call `list_tables` first"
        )
    names = ", ".join(str(c["name"]) for c in cols)
    return f"{pg_error} — actual columns on {table_name!r}: [{names}]"


async def list_user_tables(
    conn: asyncpg.Connection, user_id: UUID,
) -> dict[str, list[dict[str, object]]]:
    """Return {table_name: [columns...]} for every table in the user's
    private schema. Empty dict when the user hasn't created any yet."""
    schema = user_data_schema(user_id)
    rows = await conn.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
        """,
        schema,
    )
    tables: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        tables.setdefault(r["table_name"], []).append({
            "name": r["column_name"],
            "type": r["data_type"],
            "not_null": r["is_nullable"] == "NO",
        })
    return tables
