"""`sandbox_read_file` — read bytes (returned as text) from a path inside
the task's sandbox.

For binary files, prefer `run_python` to base64-encode + return. This
tool's main job is letting the agent inspect a CSV/JSON/log file it
wrote earlier in the same task.
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
class SandboxReadFileTool(Tool):
    name = "sandbox_read_file"
    description = (
        "Read a UTF-8 text file from the task's sandbox by path."
        " Use `run_python` if you need binary I/O."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path inside the sandbox (relative or absolute under the workdir).",
            }
        },
        "required": ["path"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        path = inputs.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError("`path` must be a non-empty string")
        sandbox = await get_manager().get(ctx.user_id, ctx.task_id)
        try:
            data = await sandbox.read_file(path)
        except FileNotFoundError as e:
            raise ToolError(f"no such file in sandbox: {path}") from e
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ToolError(
                f"{path!r} is not UTF-8 text; use `run_python` for binary I/O"
            ) from e
        return {"path": path, "size_bytes": len(data), "text": text}
