"""Postgres connection pool (asyncpg) + helpers.

The pool is initialized lazily on first acquire and explicitly closed on app
shutdown via the FastAPI lifespan hook. pgvector type registration happens
once per connection via the asyncpg `init` callback so we can pass/receive
`numpy`-shaped vectors directly.
"""

from __future__ import annotations

import asyncio
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
    """Return the absolute path to the repo's `migrations/` directory."""
    # src/wolfpaw/memory/db.py → repo root → migrations
    return Path(__file__).resolve().parents[3] / "migrations"


async def apply_sql_file(conn: asyncpg.Connection, path: Path) -> None:
    """Execute every statement in a SQL file as a single batch.

    No outer transaction — callers wrap as needed. `DO $$...$$` blocks and
    multi-statement scripts work fine through asyncpg's `execute()`.
    """
    sql = path.read_text()
    await conn.execute(sql)
