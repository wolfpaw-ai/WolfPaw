"""Postgres connection pool (asyncpg) + helpers.

The pool is initialized lazily on first acquire and explicitly closed on app
shutdown via the FastAPI lifespan hook. pgvector type registration happens
once per connection via the asyncpg `init` callback so we can pass/receive
`numpy`-shaped vectors directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg
from pgvector.asyncpg import register_vector

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _setup_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    # asyncpg requires explicit type codecs for jsonb / json — otherwise
    # passing a Python dict into a jsonb column fails with
    # "expected str, got dict". DAOs that already json.dumps() explicitly
    # are unaffected; this just makes the bare-dict case work too.
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


def _redact_dsn(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", dsn)


async def get_pool() -> asyncpg.Pool:
    """Return the process-wide pool, creating it on first call."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        settings = get_settings()
        log.info("db.pool.init", dsn=_redact_dsn(settings.database_url))
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=10,
            init=_setup_connection,
        )
        return _pool


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    log.info("db.pool.close")
    await _pool.close()
    _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


# Migration helpers ---------------------------------------------------------


def migrations_dir() -> Path:
    """Return the absolute path to the `migrations/` directory.

    Honors `WOLFPAW_MIGRATIONS_DIR` first — set this when the wolfpaw
    package is installed somewhere other than the source tree (Docker
    images, system-wide install, etc.). Falls back to the in-source
    location for dev + tests.
    """
    override = os.getenv("WOLFPAW_MIGRATIONS_DIR")
    if override:
        return Path(override).resolve()
    # src/wolfpaw/memory/db.py → repo root → migrations
    return Path(__file__).resolve().parents[3] / "migrations"


async def apply_sql_file(conn: asyncpg.Connection, path: Path) -> None:
    """Execute every statement in a SQL file as a single batch.

    No outer transaction — callers wrap as needed. `DO $$...$$` blocks and
    multi-statement scripts work fine through asyncpg's `execute()`.
    """
    sql = path.read_text()
    await conn.execute(sql)
