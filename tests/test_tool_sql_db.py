"""DB-backed tests for create_table + sql_query against a real Postgres.

Confirms the per-user `user_data_<hex>` schema is created on demand,
DDL writes there, the READ ONLY tx blocks writes, and cross-user isolation
holds (user A's tables are invisible from user B's search_path)."""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry
from wolfpaw.toolbox.user_data import user_data_schema

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    from wolfpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await apply_sql_file(conn, migrations_dir() / "001_init.sql")
        await apply_sql_file(conn, migrations_dir() / "002_auth.sql")
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def test_create_table_then_query():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    query = get_registry().get("sql_query")

    out = await create.run(
        ctx,
        table_name="receipts",
        columns=[
            {"name": "id", "type": "integer", "not_null": True},
            {"name": "vendor", "type": "text"},
            {"name": "amount", "type": "numeric"},
        ],
    )
    assert out["table"] == "receipts"
    assert out["schema"] == user_data_schema(uid)

    # Insert a row directly so sql_query has something to find.
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            f'INSERT INTO "{user_data_schema(uid)}"."receipts"'
            " (id, vendor, amount) VALUES (1, 'Acme', 9.99)"
        )
    finally:
        await conn.close()

    result = await query.run(ctx, query="SELECT vendor, amount FROM receipts")
    assert result["row_count"] == 1
    assert result["rows"][0]["vendor"] == "Acme"


async def test_sql_query_blocks_write_at_engine_level():
    """Even if the parse check is bypassed, READ ONLY tx must reject DML."""
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    query = get_registry().get("sql_query")
    await create.run(ctx, table_name="t",
                     columns=[{"name": "x", "type": "integer"}])

    # Sneak the write past the SELECT-only validator with a SELECT prefix.
    with pytest.raises(ToolError):
        await query.run(
            ctx,
            query="SELECT 1; INSERT INTO t (x) VALUES (1)",
        )


async def test_create_table_rejects_unsupported_type():
    uid = uuid4()
    create = get_registry().get("create_table")
    with pytest.raises(ToolError):
        await create.run(
            ToolContext(user_id=uid),
            table_name="t",
            columns=[{"name": "x", "type": "xml"}],
        )


async def test_create_table_rejects_bad_identifier():
    uid = uuid4()
    create = get_registry().get("create_table")
    with pytest.raises(ValueError):
        await create.run(
            ToolContext(user_id=uid),
            table_name="DROP TABLE",
            columns=[{"name": "x", "type": "integer"}],
        )


async def test_per_user_isolation():
    """User B cannot see user A's table via the search_path-scoped query."""
    a, b = uuid4(), uuid4()
    create = get_registry().get("create_table")
    query = get_registry().get("sql_query")

    await create.run(
        ToolContext(user_id=a),
        table_name="alices_table",
        columns=[{"name": "x", "type": "integer"}],
    )

    # B's search_path is user_data_<b>, public — no alices_table there.
    with pytest.raises(Exception):
        await query.run(
            ToolContext(user_id=b),
            query="SELECT * FROM alices_table",
        )
