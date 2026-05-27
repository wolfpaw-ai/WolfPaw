"""`sql_update` — UPDATE rows in the user's `user_data_*` schema.

WHERE is structured (column → value, AND'd together), not raw SQL. To
update every row, the agent must pass `where_all: true` explicitly —
empty filters are rejected so a missing WHERE can never silently rewrite
the whole table. All identifiers are validated; all values pass through
asyncpg parameters.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.toolbox.user_data import (
    ensure_schema,
    quote_ident,
    validate_identifier,
)


def _normalize_assignments(assignments: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(assignments, dict) or not assignments:
        raise ToolError(f"`{field}` must be a non-empty object")
    for c in assignments:
        validate_identifier(c)
    return assignments


@register_tool
class SqlUpdateTool(Tool):
    name = "sql_update"
    description = (
        "Update rows in a table in the user's private SQL workspace."
        " `set` is a column → new-value map. `where` is a column → value"
        " map AND'd together (equality only). To update every row, pass"
        " `where_all: true` instead of `where`. Returns the number of"
        " rows updated."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string"},
            "set": {
                "type": "object",
                "description": "Column → new value. At least one entry.",
            },
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
                    "Set true to update every row (no WHERE). Required"
                    " when `where` is absent or empty."
                ),
            },
        },
        "required": ["table_name", "set"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        table = inputs.get("table_name")
        if not isinstance(table, str):
            raise ToolError("`table_name` must be a string")
        table = validate_identifier(table)

        set_map = _normalize_assignments(inputs.get("set"), field="set")
        where = inputs.get("where") or {}
        where_all = bool(inputs.get("where_all", False))

        if not where and not where_all:
            raise ToolError(
                "`where` is empty — pass `where_all: true` to update every row"
            )
        if where and where_all:
            raise ToolError("pass either `where` or `where_all`, not both")
        if where and not isinstance(where, dict):
            raise ToolError("`where` must be an object of column → value")
        for c in where:
            validate_identifier(c)

        params: list[Any] = []
        set_clauses: list[str] = []
        for c, v in set_map.items():
            params.append(v)
            set_clauses.append(f"{quote_ident(c)} = ${len(params)}")

        where_sql = ""
        if where:
            where_clauses: list[str] = []
            for c, v in where.items():
                params.append(v)
                where_clauses.append(f"{quote_ident(c)} = ${len(params)}")
            where_sql = " WHERE " + " AND ".join(where_clauses)

        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            sql = (
                f'UPDATE {quote_ident(schema)}.{quote_ident(table)}'
                f' SET {", ".join(set_clauses)}{where_sql}'
            )
            status = await conn.execute(sql, *params)

        return {
            "updated": _row_count_from_status(status),
            "schema": schema,
            "table": table,
        }


def _row_count_from_status(status: str) -> int:
    """asyncpg returns status strings like 'UPDATE 3' / 'DELETE 0'."""
    parts = status.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        return int(parts[-1])
    return 0
