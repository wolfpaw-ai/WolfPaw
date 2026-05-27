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


async def test_create_table_reports_existed_and_actual_schema():
    """Reproduce the 'meatloaf' failure: a stale table from an earlier
    attempt has a sparser schema than the new plan wants. CREATE TABLE
    IF NOT EXISTS no-ops, and the tool must report the *real* columns
    plus existed=True so the caller can adapt."""
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")

    first = await create.run(
        ctx, table_name="recipes",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "vendor", "type": "text"},
            {"name": "amount", "type": "numeric"},
        ],
    )
    assert first["existed"] is False
    assert {c["name"] for c in first["columns"]} == {"id", "vendor", "amount"}

    second = await create.run(
        ctx, table_name="recipes",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "title", "type": "text"},
            {"name": "ingredients", "type": "jsonb"},
        ],
    )
    # Truth wins: the second call doesn't replace the schema.
    assert second["existed"] is True
    assert {c["name"] for c in second["columns"]} == {"id", "vendor", "amount"}


async def test_list_tables_and_describe_table():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    list_t = get_registry().get("list_tables")
    describe = get_registry().get("describe_table")

    empty = await list_t.run(ctx)
    assert empty["table_count"] == 0
    assert empty["tables"] == []

    await create.run(
        ctx, table_name="recipes",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "title", "type": "text"},
        ],
    )
    await create.run(
        ctx, table_name="receipts",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "amount", "type": "numeric"},
        ],
    )

    listing = await list_t.run(ctx)
    names = [t["name"] for t in listing["tables"]]
    assert names == ["receipts", "recipes"]

    described = await describe.run(ctx, table_name="recipes")
    assert [c["name"] for c in described["columns"]] == ["id", "title"]


async def test_describe_table_missing_raises_with_hint():
    uid = uuid4()
    describe = get_registry().get("describe_table")
    with pytest.raises(ToolError, match="list_tables"):
        await describe.run(ToolContext(user_id=uid), table_name="nope")


async def test_sql_insert_unknown_column_returns_actual_columns():
    """The exact recipes/meatloaf failure: insert references a column
    that doesn't exist. The ToolError must surface the real column list
    so the agent's retry can recover."""
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")

    await create.run(
        ctx, table_name="recipes",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "vendor", "type": "text"},
            {"name": "amount", "type": "numeric"},
        ],
    )
    with pytest.raises(ToolError) as excinfo:
        await insert.run(
            ctx, table_name="recipes",
            rows=[{"id": 1, "title": "Meatloaf"}],
        )
    msg = str(excinfo.value)
    assert "title" in msg
    assert "vendor" in msg and "amount" in msg


async def test_sql_update_unknown_column_returns_actual_columns():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    update = get_registry().get("sql_update")

    await create.run(
        ctx, table_name="t",
        columns=[{"name": "id", "type": "integer"}],
    )
    with pytest.raises(ToolError) as excinfo:
        await update.run(
            ctx, table_name="t",
            set={"missing": 1}, where_all=True,
        )
    assert "missing" in str(excinfo.value)
    assert "id" in str(excinfo.value)


async def test_sql_delete_unknown_column_returns_actual_columns():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    delete = get_registry().get("sql_delete")

    await create.run(
        ctx, table_name="t",
        columns=[{"name": "id", "type": "integer"}],
    )
    with pytest.raises(ToolError) as excinfo:
        await delete.run(
            ctx, table_name="t", where={"missing": 1},
        )
    assert "missing" in str(excinfo.value)


async def test_sql_insert_then_query_round_trip():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")
    query = get_registry().get("sql_query")

    await create.run(
        ctx,
        table_name="receipts",
        columns=[
            {"name": "id", "type": "integer", "not_null": True},
            {"name": "vendor", "type": "text"},
            {"name": "amount", "type": "numeric"},
        ],
    )
    out = await insert.run(
        ctx,
        table_name="receipts",
        rows=[
            {"id": 1, "vendor": "Acme", "amount": 9.99},
            {"id": 2, "vendor": "Globex", "amount": 42.5},
        ],
    )
    assert out["inserted"] == 2

    result = await query.run(
        ctx, query="SELECT vendor FROM receipts ORDER BY id",
    )
    assert [r["vendor"] for r in result["rows"]] == ["Acme", "Globex"]


async def test_sql_update_with_where():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")
    update = get_registry().get("sql_update")
    query = get_registry().get("sql_query")

    await create.run(
        ctx,
        table_name="t",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "label", "type": "text"},
        ],
    )
    await insert.run(
        ctx, table_name="t",
        rows=[{"id": 1, "label": "old"}, {"id": 2, "label": "old"}],
    )

    out = await update.run(
        ctx, table_name="t",
        set={"label": "new"}, where={"id": 1},
    )
    assert out["updated"] == 1

    result = await query.run(
        ctx, query="SELECT id, label FROM t ORDER BY id",
    )
    assert result["rows"] == [
        {"id": 1, "label": "new"},
        {"id": 2, "label": "old"},
    ]


async def test_sql_delete_with_where():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")
    delete = get_registry().get("sql_delete")
    query = get_registry().get("sql_query")

    await create.run(
        ctx, table_name="t",
        columns=[{"name": "id", "type": "integer"}],
    )
    await insert.run(
        ctx, table_name="t",
        rows=[{"id": 1}, {"id": 2}, {"id": 3}],
    )

    out = await delete.run(ctx, table_name="t", where={"id": 2})
    assert out["deleted"] == 1

    result = await query.run(ctx, query="SELECT id FROM t ORDER BY id")
    assert [r["id"] for r in result["rows"]] == [1, 3]


async def test_sql_delete_where_all_clears_table():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")
    delete = get_registry().get("sql_delete")
    query = get_registry().get("sql_query")

    await create.run(
        ctx, table_name="t",
        columns=[{"name": "id", "type": "integer"}],
    )
    await insert.run(
        ctx, table_name="t",
        rows=[{"id": 1}, {"id": 2}],
    )

    out = await delete.run(ctx, table_name="t", where_all=True)
    assert out["deleted"] == 2

    result = await query.run(ctx, query="SELECT id FROM t")
    assert result["row_count"] == 0


async def test_sql_insert_handles_jsonb_values():
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")
    query = get_registry().get("sql_query")

    await create.run(
        ctx, table_name="payloads",
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "data", "type": "jsonb"},
        ],
    )
    await insert.run(
        ctx, table_name="payloads",
        rows=[{"id": 1, "data": {"nested": [1, 2, 3], "ok": True}}],
    )

    result = await query.run(ctx, query="SELECT data FROM payloads")
    assert result["rows"][0]["data"] == {"nested": [1, 2, 3], "ok": True}


async def test_sql_crud_blocks_injection_via_value():
    """Quoted identifiers + parameterized values mean SQL fragments in
    values are inert. A payload that 'looks like' a DROP must end up as
    a literal string column value, not executed."""
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    create = get_registry().get("create_table")
    insert = get_registry().get("sql_insert")
    query = get_registry().get("sql_query")

    await create.run(
        ctx, table_name="canary",
        columns=[{"name": "note", "type": "text"}],
    )
    payload = "'); DROP TABLE canary; --"
    await insert.run(
        ctx, table_name="canary", rows=[{"note": payload}],
    )
    # Table still exists, payload stored verbatim.
    result = await query.run(ctx, query="SELECT note FROM canary")
    assert result["rows"][0]["note"] == payload


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
