"""Pure-unit input validation for sql_insert / sql_update / sql_delete.

DB-backed end-to-end checks live in test_tool_sql_db.py."""

from __future__ import annotations

from uuid import uuid4

import pytest

from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry


@pytest.fixture
def ctx():
    return ToolContext(user_id=uuid4())


# --- sql_insert -----------------------------------------------------------


async def test_sql_insert_rejects_empty_rows(ctx):
    tool = get_registry().get("sql_insert")
    with pytest.raises(ToolError):
        await tool.run(ctx, table_name="t", rows=[])


async def test_sql_insert_rejects_mixed_column_sets(ctx):
    tool = get_registry().get("sql_insert")
    with pytest.raises(ToolError):
        await tool.run(
            ctx,
            table_name="t",
            rows=[{"a": 1, "b": 2}, {"a": 3}],
        )


async def test_sql_insert_rejects_bad_column_name(ctx):
    tool = get_registry().get("sql_insert")
    with pytest.raises(ValueError):
        await tool.run(ctx, table_name="t", rows=[{"DROP TABLE": 1}])


async def test_sql_insert_rejects_bad_table_name(ctx):
    tool = get_registry().get("sql_insert")
    with pytest.raises(ValueError):
        await tool.run(ctx, table_name="bad-name", rows=[{"a": 1}])


# --- sql_update -----------------------------------------------------------


async def test_sql_update_requires_where_or_where_all(ctx):
    tool = get_registry().get("sql_update")
    with pytest.raises(ToolError):
        await tool.run(ctx, table_name="t", set={"a": 1})


async def test_sql_update_rejects_both_where_and_where_all(ctx):
    tool = get_registry().get("sql_update")
    with pytest.raises(ToolError):
        await tool.run(
            ctx, table_name="t", set={"a": 1},
            where={"a": 1}, where_all=True,
        )


async def test_sql_update_rejects_empty_set(ctx):
    tool = get_registry().get("sql_update")
    with pytest.raises(ToolError):
        await tool.run(ctx, table_name="t", set={}, where_all=True)


# --- sql_delete -----------------------------------------------------------


async def test_sql_delete_requires_where_or_where_all(ctx):
    tool = get_registry().get("sql_delete")
    with pytest.raises(ToolError):
        await tool.run(ctx, table_name="t")


async def test_sql_delete_rejects_both_where_and_where_all(ctx):
    tool = get_registry().get("sql_delete")
    with pytest.raises(ToolError):
        await tool.run(
            ctx, table_name="t", where={"a": 1}, where_all=True,
        )
