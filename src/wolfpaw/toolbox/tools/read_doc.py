"""`read_doc` — fetch a file from the user's workspace as text.

Looks up the latest version of `filename` in `workspace_files`, then
pulls bytes via the configured Storage backend. Decodes as UTF-8;
binary files are out of scope for this tool (artifacts tools own those
in step 9).
"""

from __future__ import annotations

from typing import Any

from wolfpaw.memory.db import acquire
from wolfpaw.storage import get_storage
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.workspace import files as files_dao


@register_tool
class ReadDocTool(Tool):
    name = "read_doc"
    description = (
        "Read a text/markdown file from the user's workspace by filename."
        " Returns the latest version's contents as a UTF-8 string."
    )
    input_schema = {
        "type": "object",
        "properties": {"filename": {"type": "string"}},
        "required": ["filename"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        filename = inputs.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ToolError("`filename` must be a non-empty string")
        async with acquire() as conn:
            existing = await files_dao.get_latest_by_filename(
                conn, ctx.user_id, filename
            )
        if existing is None:
            raise ToolError(f"no workspace file named {filename!r}")
        try:
            data = await get_storage().get(ctx.user_id, filename)
        except FileNotFoundError as e:
            raise ToolError(
                f"workspace_files row exists for {filename!r} but storage has no bytes"
            ) from e
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ToolError(
                f"{filename!r} is not UTF-8 text; read_doc is for text files only"
            ) from e
        return {
            "filename": existing.filename,
            "version": existing.version,
            "size_bytes": existing.size_bytes,
            "text": text,
        }
