"""`describe_table` — show one table's column layout.

Narrower than `list_tables`: takes a table name and returns just that
table's columns. Raises ToolError if the table doesn't exist in the
user's private SQL workspace.
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
    describe_columns,
    ensure_schema,
    validate_identifier,
)


@register_tool
class DescribeTableTool(Tool):
    name = "describe_table"
    description = (
        "Show the columns of one table in the user's private SQL"
        " workspace. Call this before sql_insert/update/delete if the"
        " exact column list isn't already known from this conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {"table_name": {"type": "string"}},
        "required": ["table_name"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        table = inputs.get("table_name")
        if not isinstance(table, str):
            raise ToolError("`table_name` must be a string")
        table = validate_identifier(table)

        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            cols = await describe_columns(conn, schema, table)

        if not cols:
            raise ToolError(
                f"table {table!r} does not exist in the user's SQL"
                " workspace — call `list_tables` to see what's available"
            )
        return {"schema": schema, "table": table, "columns": cols}
