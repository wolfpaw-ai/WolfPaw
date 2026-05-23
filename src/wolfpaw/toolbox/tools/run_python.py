"""`run_python` — execute Python in the task's sandbox.

State (variables, imports, files) persists across calls within the same
task because SandboxManager keeps one Sandbox per (user_id, task_id).
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
class RunPythonTool(Tool):
    name = "run_python"
    description = (
        "Execute Python code in a stateful sandboxed REPL. Globals + files"
        " persist across calls within the same task. Returns stdout, stderr,"
        " and exit_code; non-zero exit_code means the code raised."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
        "required": ["code"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        code = inputs.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ToolError("`code` must be a non-empty string")
        timeout = inputs.get("timeout_seconds")
        sandbox = await get_manager().get(ctx.user_id, ctx.task_id)
        result = await sandbox.run_python(
            code, timeout_seconds=int(timeout) if timeout else None
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "elapsed_seconds": result.elapsed_seconds,
            "timed_out": result.timed_out,
        }
