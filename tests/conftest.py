"""Shared test fixtures.

Two things live here: the trace-sink kill switch, and the fresh-database
helper that DB-backed tests use to wipe the schema safely.

**Trace sink.** `ModelClient` defaults to the Postgres trace sink, and the
unit suite constructs `ModelClient` in dozens of places without a database —
so without this every one of those tests would try to open a connection,
fail, and log a warning on each model call. The sink swallows its own write
errors, so nothing would *break*; it would just be slow and noisy for no
reason. Tests that actually exercise tracing inject a sink explicitly (or
construct `PostgresTraceSink` directly), which bypasses this.

**Fresh database.** See `fresh_db_conn` below — the short version is that
`DROP SCHEMA public CASCADE` will block forever behind the app's own
connection pool unless the pool is closed first.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from wolfpaw.memory.db import close_pool
from wolfpaw.metering.trace_sink import NullTraceSink


@pytest.fixture(autouse=True)
def _no_trace_sink(monkeypatch):
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_trace_sink", lambda: NullTraceSink()
    )


def _force_reset_pool() -> None:
    """Drop the module-global pool reference without closing it.

    Used when `close_pool()` itself raised — the object is unusable, so the
    only way forward is to forget it and let the next `get_pool()` build a
    fresh one. The orphaned server-side connections are cleaned up by
    `_terminate_leftover_backends`.
    """
    import wolfpaw.memory.db as db_mod

    db_mod._pool = None


async def _terminate_leftover_backends() -> None:
    """Kill any connection to the test database other than our own."""
    conn = await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _drain_pool_between_tests():
    """Keep one test's leaked Postgres connections from hanging the next one.

    The failure mode this exists to prevent: route tests drive the app through
    Starlette's `TestClient`, which runs it in its *own* event loop. The
    asyncpg pool gets created inside that loop, and by teardown time that loop
    is closed — so `close_pool()` raises `RuntimeError: Event loop is closed`
    and the pool's server-side connections are never released.

    Those orphans still hold locks. The next DB-backed test file opens with
    `DROP SCHEMA public CASCADE` (27 files do), which needs an ACCESS
    EXCLUSIVE lock on every table and therefore queues behind them — forever,
    because Postgres has no lock timeout by default. The suite doesn't fail,
    it hangs, which is why this survived: it only bites when the test database
    is actually reachable, and with pytest-randomly shuffling file order,
    whether it bites at all depends on the seed.

    So: sweep any orphaned backend before each test, and close the pool after.
    The sweep is unconditional rather than triggered by a "did the last close
    fail" flag, because the failing close happens in the *test file's* own
    teardown fixture — it never reaches this one, so a flag set here would
    stay false exactly when it matters.

    This treats the symptom. The underlying problem is that route tests share
    a process-global pool across two event loops, which also produces the
    `InterfaceError: another operation is in progress` failures visible in the
    persona and tasks route tests. Fixing that properly means giving those
    tests an ASGI transport that runs in the test's own loop (httpx
    `ASGITransport` + `LifespanManager`) instead of `TestClient`. Worth doing;
    out of scope for the change that uncovered it.
    """
    if os.getenv("WOLFPAW_TEST_DATABASE_URL"):
        await _terminate_leftover_backends()
    yield
    try:
        await close_pool()
    except Exception:  # noqa: BLE001 — see docstring; the loop is already gone
        _force_reset_pool()


async def fresh_db_conn() -> asyncpg.Connection:
    """Connect to the test database and wipe `public` for a deterministic start.

    Two non-obvious steps, both learned the hard way — without them the whole
    suite hangs rather than failing:

    1. **Close the app's asyncpg pool first.** Any DB-backed test that went
       through `acquire()` leaves the process-wide pool open with live
       connections holding locks on the tables it touched. `DROP SCHEMA
       public CASCADE` needs an ACCESS EXCLUSIVE lock on every one of them,
       so it queues behind those connections.

    2. **Set a `lock_timeout`.** Postgres waits on a blocked lock forever by
       default. With pytest-randomly shuffling file order, any future leak
       turns into a suite that appears to run for hours instead of one that
       fails in five seconds with a legible error. Fail fast, loudly.
    """
    await close_pool()
    conn = await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])
    await conn.execute("SET lock_timeout = '5s'")
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return conn
