"""`sql_delete` — DELETE rows in the user's `user_data_*` schema.

Same WHERE shape as `sql_update`: structured equality filters AND'd
together. An empty `where` requires explicit `where_all: true` to
prevent accidental full-table deletes.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.toolbox.tools.sql_update import _row_count_from_status
from wolfpaw.toolbox.user_data import (
    describe_columns,
    ensure_schema,
    explain_column_error,
    quote_ident,
    validate_identifier,
    value_placeholder,
)


@register_tool
class SqlDeleteTool(Tool):
    name = "sql_delete"
    description = (
        "Delete rows from a table in the user's private SQL workspace."
        " `where` is a column → value map AND'd together (equality). To"
        " delete every row, pass `where_all: true` instead of `where`."
        " Returns the number of rows deleted."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string"},
            "where": {
                "type": "object",
                "description": (
                    "Column → value, AND'd together (equality). Required"
                    " unless `where_all` is true."
                ),
            },
            "where_all": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true to delete every row (no WHERE). Required"
                    " when `where` is absent or empty."
                ),
            },
        },
        "required": ["table_name"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        table = inputs.get("table_name")
        if not isinstance(table, str):
            raise ToolError("`table_name` must be a string")
        table = validate_identifier(table)

        where = inputs.get("where") or {}
        where_all = bool(inputs.get("where_all", False))

        if not where and not where_all:
            raise ToolError(
                "`where` is empty — pass `where_all: true` to delete every row"
            )
        if where and where_all:
            raise ToolError("pass either `where` or `where_all`, not both")
        if where and not isinstance(where, dict):
            raise ToolError("`where` must be an object of column → value")
        for c in where:
            validate_identifier(c)

        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            # Column types so a string compared to a temporal column in WHERE
            # gets a `::text::<type>` cast instead of failing asyncpg binding.
            coltypes = {
                col["name"]: col["type"]
                for col in await describe_columns(conn, schema, table)
            }

            params: list[Any] = []
            where_sql = ""
            if where:
                where_clauses: list[str] = []
                for c, v in where.items():
                    params.append(v)
                    ph = value_placeholder(len(params), v, coltypes.get(c))
                    where_clauses.append(f"{quote_ident(c)} = {ph}")
                where_sql = " WHERE " + " AND ".join(where_clauses)

            sql = (
                f'DELETE FROM {quote_ident(schema)}.{quote_ident(table)}'
                f'{where_sql}'
            )
            try:
                status = await conn.execute(sql, *params)
            except (
                asyncpg.UndefinedColumnError,
                asyncpg.UndefinedTableError,
            ) as e:
                hint = await explain_column_error(conn, schema, table, e)
                raise ToolError(hint) from e

        return {
            "deleted": _row_count_from_status(status),
            "schema": schema,
            "table": table,
        }
