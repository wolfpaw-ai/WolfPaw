"""`sandbox_write_file` — write text bytes to a path inside the sandbox.

For binary writes, use `run_python` to compose the bytes and `open(...).write()`.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.sandbox import get_manager
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)


@register_tool
class SandboxWriteFileTool(Tool):
    name = "sandbox_write_file"
    description = (
        "Write UTF-8 text to a file inside the task's sandbox. Use"
        " `run_python` if you need to write binary bytes."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        path = inputs.get("path")
        content = inputs.get("content")
        if not isinstance(path, str) or not path:
            raise ToolError("`path` must be a non-empty string")
        if not isinstance(content, str):
            raise ToolError("`content` must be a string")
        sandbox = await get_manager().get(ctx.user_id, ctx.task_id)
        data = content.encode("utf-8")
        await sandbox.write_file(path, data)
        return {"path": path, "size_bytes": len(data)}
