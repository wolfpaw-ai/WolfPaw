"""`create_table` — DDL into the user's private `user_data_*` schema.

Column types come from a small whitelist. Identifier names (table +
columns) are validated against a strict regex. Constructs the CREATE
TABLE as a single statement with quoted identifiers — no user data ever
flows into the SQL via interpolation.
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

# Mapping is intentionally tight. If the agent needs something exotic,
# graduate it through review — not via the agent's tool input.
_TYPE_WHITELIST = {
    "text": "TEXT",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "double": "DOUBLE PRECISION",
    "boolean": "BOOLEAN",
    "timestamptz": "TIMESTAMPTZ",
    "date": "DATE",
    "jsonb": "JSONB",
    "numeric": "NUMERIC",
}


def _column_clause(col: dict) -> str:
    name = validate_identifier(col["name"])
    raw_type = str(col.get("type", "")).lower()
    if raw_type not in _TYPE_WHITELIST:
        raise ToolError(
            f"unsupported column type {raw_type!r}; "
            f"choose from {sorted(_TYPE_WHITELIST)}"
        )
    pg_type = _TYPE_WHITELIST[raw_type]
    clause = f"{quote_ident(name)} {pg_type}"
    if col.get("not_null"):
        clause += " NOT NULL"
    return clause


@register_tool
class CreateTableTool(Tool):
    name = "create_table"
    description = (
        "Create a table in the user's private SQL workspace. Idempotent"
        " (CREATE TABLE IF NOT EXISTS). Column types restricted to: "
        + ", ".join(sorted(_TYPE_WHITELIST))
        + "."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string"},
            "columns": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "not_null": {"type": "boolean"},
                    },
                    "required": ["name", "type"],
                },
            },
        },
        "required": ["table_name", "columns"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        table = inputs.get("table_name")
        columns = inputs.get("columns")
        if not isinstance(table, str):
            raise ToolError("`table_name` must be a string")
        if not isinstance(columns, list) or not columns:
            raise ToolError("`columns` must be a non-empty list")
        table = validate_identifier(table)

        col_clauses = [_column_clause(c) for c in columns]

        async with acquire() as conn:
            schema = await ensure_schema(conn, ctx.user_id)
            sql = (
                f'CREATE TABLE IF NOT EXISTS {quote_ident(schema)}.{quote_ident(table)}'
                f' ({", ".join(col_clauses)})'
            )
            await conn.execute(sql)

        return {
            "schema": schema,
            "table": table,
            "columns": [
                {"name": validate_identifier(c["name"]), "type": str(c["type"]).lower()}
                for c in columns
            ],
        }
