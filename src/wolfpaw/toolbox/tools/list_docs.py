"""`list_docs` — enumerate the user's workspace docs.

Returns one entry per filename (latest version) with size + version
metadata. Pure read against `workspace_files` — no Storage round-trip.
Useful as a first step before `read_doc` when the filename isn't
already known from the conversation.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import Tool, ToolContext, register_tool
from wolfpaw.workspace import files as files_dao


@register_tool
class ListDocsTool(Tool):
    name = "list_docs"
    description = (
        "List the user's workspace docs (latest version of each"
        " filename) with size, version, and mime type. Call this when"
        " you need to know what files exist before reading or writing."
        " For finding files by content rather than name, use"
        " `search_docs`."
    )
    input_schema = {"type": "object", "properties": {}, "required": []}

    async def run(self, ctx: ToolContext, **_inputs: Any) -> dict[str, Any]:
        async with acquire() as conn:
            files = await files_dao.list_latest(conn, ctx.user_id)
        return {
            "file_count": len(files),
            "files": [
                {
                    "filename": f.filename,
                    "version": f.version,
                    "size_bytes": f.size_bytes,
                    "mime_type": f.mime_type,
                    "created_at": f.created_at.isoformat(),
                }
                for f in files
            ],
        }
