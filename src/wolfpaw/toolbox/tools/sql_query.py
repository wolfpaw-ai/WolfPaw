"""`sql_query` — read-only SELECTs against the user's `user_data_*` schema.

Two layers of defense:
  1. Pre-parse check: query must start with SELECT or WITH (after stripping
     leading whitespace and SQL comments). No DML/DDL through here.
  2. READ ONLY transaction with the user's schema on the front of
     `search_path`. The DB rejects any write attempt at the engine level
     even if the parse check missed something.
"""

from __future__ import annotations

import re
from typing import Any

from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.toolbox.user_data import ensure_schema

_LEADING_COMMENT = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/|\s)+", re.DOTALL)
_MAX_ROWS_DEFAULT = 200
_MAX_ROWS_HARD = 1000


def _strip_leading_comments(sql: str) -> str:
    while True:
        m = _LEADING_COMMENT.match(sql)
        if not m:
            return sql.lstrip()
        sql = sql[m.end() :]


def _validate_select(sql: str) -> None:
    stripped = _strip_leading_comments(sql).upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        raise ToolError(
            "sql_query only accepts SELECT / WITH statements — use"
            " sql_insert / sql_update / sql_delete for writes"
        )


@register_tool
class SqlQueryTool(Tool):
    name = "sql_query"
    description = (
        "Run a read-only SELECT against the user's private SQL workspace."
        " Tables created via `create_table` are visible here. Limited to"
        f" {_MAX_ROWS_DEFAULT} rows by default; pass `limit` (max"
        f" {_MAX_ROWS_HARD}) to override."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A single SELECT or CTE."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_ROWS_HARD,
                "default": _MAX_ROWS_DEFAULT,
            },
        },
        "required": ["query"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        query = inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("`query` must be a non-empty string")
        _validate_select(query)
        limit = max(1, min(_MAX_ROWS_HARD, int(inputs.get("limit") or _MAX_ROWS_DEFAULT)))

        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            async with conn.transaction(readonly=True):
                await conn.execute(
                    f'SET LOCAL search_path TO "{schema}", public'
                )
                rows = await conn.fetch(query)

        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "row_count": len(rows),
            "truncated": truncated,
            "rows": [dict(r) for r in rows],
        }
