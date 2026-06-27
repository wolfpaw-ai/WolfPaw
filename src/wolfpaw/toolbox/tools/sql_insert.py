"""`sql_insert` — INSERT rows into the user's `user_data_*` schema.

Accepts a list of row dicts. All rows must share the same column set;
column names are validated as identifiers and quoted. Values are passed
as `$N` parameters — never interpolated — so SQL injection through
agent-supplied content is structurally impossible.
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
from wolfpaw.toolbox.user_data import (
    describe_columns,
    ensure_schema,
    explain_column_error,
    quote_ident,
    validate_identifier,
)

_MAX_ROWS_PER_CALL = 500

# information_schema data_type → Postgres cast target. String values bound to
# these columns (e.g. 'today', '2026-06-27') fail asyncpg's strict temporal
# binding, so we cast the placeholder and let Postgres parse the string.
_TEMPORAL_CASTS = {
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
}


def _normalize_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ToolError("`rows` must be a non-empty list of row objects")
    if len(rows) > _MAX_ROWS_PER_CALL:
        raise ToolError(
            f"too many rows in one call (max {_MAX_ROWS_PER_CALL})"
        )
    first = rows[0]
    if not isinstance(first, dict) or not first:
        raise ToolError("each row must be a non-empty object")
    cols = list(first.keys())
    for r in rows[1:]:
        if not isinstance(r, dict) or set(r.keys()) != set(cols):
            raise ToolError(
                "all rows must share the same column set"
            )
    for c in cols:
        validate_identifier(c)
    return rows


@register_tool
class SqlInsertTool(Tool):
    name = "sql_insert"
    description = (
        "Insert one or more rows into a table in the user's private SQL"
        " workspace. All rows must share the same columns. Returns the"
        " number of rows inserted."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string"},
            "rows": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_ROWS_PER_CALL,
                "items": {
                    "type": "object",
                    "description": (
                        "Column → value map. Keys must match columns on"
                        " the target table; all rows in one call must"
                        " share the same key set."
                    ),
                },
            },
        },
        "required": ["table_name", "rows"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        table = inputs.get("table_name")
        if not isinstance(table, str):
            raise ToolError("`table_name` must be a string")
        table = validate_identifier(table)
        rows = _normalize_rows(inputs.get("rows"))
        cols = list(rows[0].keys())

        col_sql = ", ".join(quote_ident(c) for c in cols)
        n = len(cols)

        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            # Cast string values targeting temporal columns so Postgres parses
            # them ('today', ISO dates) instead of asyncpg rejecting the str.
            coltypes = {
                c["name"]: c["type"]
                for c in await describe_columns(conn, schema, table)
            }
            casts = [_TEMPORAL_CASTS.get(coltypes.get(c, "")) for c in cols]

            # Single VALUES list with N params per row.
            value_groups = []
            params: list[Any] = []
            for i, r in enumerate(rows):
                placeholders = []
                for j, c in enumerate(cols):
                    idx = i * n + j + 1
                    val = r[c]
                    if casts[j] and isinstance(val, str):
                        placeholders.append(f"${idx}::{casts[j]}")
                    else:
                        placeholders.append(f"${idx}")
                    params.append(val)
                value_groups.append(f"({', '.join(placeholders)})")

            sql = (
                f'INSERT INTO {quote_ident(schema)}.{quote_ident(table)}'
                f' ({col_sql}) VALUES {", ".join(value_groups)}'
            )
            try:
                await conn.execute(sql, *params)
            except (
                asyncpg.UndefinedColumnError,
                asyncpg.UndefinedTableError,
            ) as e:
                hint = await explain_column_error(conn, schema, table, e)
                raise ToolError(hint) from e

        return {"inserted": len(rows), "schema": schema, "table": table}
