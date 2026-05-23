"""`write_doc` — write text into the user's workspace.

Collision policy: if a file with that name already exists, raise
`WorkspaceCollision` unless the caller explicitly sets `overwrite=True`.
The executor's overwrite-with-confirmation flow (step 15, riding on the
tasks lifecycle) catches the exception and pings the user; until then
the agent simply gets a tool error and must decide what to do.

Overwrites are versioned: a new `workspace_files` row is inserted with
`version = prior + 1` and `supersedes_id = prior.id`. The prior row stays
for history.
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
from wolfpaw.workspace.files import WorkspaceCollision


@register_tool
class WriteDocTool(Tool):
    name = "write_doc"
    description = (
        "Write a text/markdown file to the user's workspace. By default,"
        " rejects writes when a file with the same name already exists —"
        " set `overwrite=true` to version-bump instead. Returns the new"
        " workspace file row."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "content": {"type": "string"},
            "mime_type": {"type": "string", "default": "text/markdown"},
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["filename", "content"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        filename = inputs.get("filename")
        content = inputs.get("content")
        if not isinstance(filename, str) or not filename:
            raise ToolError("`filename` must be a non-empty string")
        if not isinstance(content, str):
            raise ToolError("`content` must be a string")
        mime_type = inputs.get("mime_type") or "text/markdown"
        overwrite = bool(inputs.get("overwrite"))

        storage = get_storage()
        async with acquire() as conn:
            existing = await files_dao.get_latest_by_filename(
                conn, ctx.user_id, filename
            )
            if existing is not None and not overwrite:
                raise WorkspaceCollision(existing)

            data = content.encode("utf-8")
            try:
                obj = await storage.put(ctx.user_id, filename, data)
            except ValueError as e:
                raise ToolError(str(e)) from e

            version = (existing.version + 1) if existing else 1
            supersedes_id = existing.id if existing else None
            f = await files_dao.register(
                conn,
                user_id=ctx.user_id,
                source="agent_output",
                filename=filename,
                storage_url=obj.storage_url,
                size_bytes=obj.size_bytes,
                mime_type=mime_type,
                sha256=obj.sha256,
                task_id=ctx.task_id,
                version=version,
                supersedes_id=supersedes_id,
            )

        return {
            "file_id": str(f.id),
            "filename": f.filename,
            "version": f.version,
            "size_bytes": f.size_bytes,
            "sha256": f.sha256,
        }
