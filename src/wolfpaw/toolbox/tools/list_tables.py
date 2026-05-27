"""`list_tables` — enumerate the user's private SQL workspace.

Returns every table in `user_data_<uid>` with its column list. Pure
read against `information_schema` — no DDL, no DML. Useful as a first
step before any write the planner isn't already certain about.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import Tool, ToolContext, register_tool
from wolfpaw.toolbox.user_data import ensure_schema, list_user_tables


@register_tool
class ListTablesTool(Tool):
    name = "list_tables"
    description = (
        "List every table in the user's private SQL workspace, with"
        " each table's column names and types. Call this when you're"
        " about to read or write and you're not already sure what"
        " tables exist or what columns they have."
    )
    input_schema = {"type": "object", "properties": {}, "required": []}

    async def run(self, ctx: ToolContext, **_inputs: Any) -> dict[str, Any]:
        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            tables = await list_user_tables(conn, ctx.user_id)
        return {
            "schema": schema,
            "table_count": len(tables),
            "tables": [
                {"name": name, "columns": cols}
                for name, cols in sorted(tables.items())
            ],
        }
