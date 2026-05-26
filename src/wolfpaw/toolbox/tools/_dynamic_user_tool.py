"""Sandbox-backed wrapper for user-approved tools (step 28).

A ``DynamicUserTool`` is constructed at dispatch time from a
``memory.tools.UserTool`` row. It satisfies the :class:`Tool`
interface so the Executor's existing ``_run_functional`` path can
dispatch it unchanged — except that resolution falls through to the
DB when the builtin Registry doesn't carry the name.

Execution contract (mirrors ``toolbox/tools/_artifact.py``):

  1. Caller's ``inputs`` dict is JSON-serialized to ``inputs.json``
     inside the user's sandbox.
  2. A wrapper script that loads ``inputs.json``, runs the user's
     implementation, and writes ``result`` to ``output.json`` is
     executed via ``sandbox.run_python``.
  3. The result dict comes back as the tool's return value.

The wrapper script is constant (no user input is interpolated into
Python source). The user's implementation runs as a string body
inside the wrapper. Inputs flow only through the JSON file.

Security is enforced by the sandbox layer (subprocess / docker / e2b):
network egress disabled, CPU + memory caps, ephemeral workdir wiped
on teardown. The Tool Creator's prompt explicitly warns against
designs that need network or secrets.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from wolfpaw.memory.tools import UserTool
from wolfpaw.sandbox import get_manager
from wolfpaw.toolbox.registry import Tool, ToolContext, ToolError


_INPUTS_BASENAME = "inputs.json"
_OUTPUT_BASENAME = "output.json"


_WRAPPER_TEMPLATE = """\
import json

with open({inputs!r}, "r") as _fh:
    inputs = json.load(_fh)

result = None

# --- user implementation begins ---
{body}
# --- user implementation ends ---

if result is None:
    raise SystemExit(
        "user-tool implementation did not set `result`"
    )

with open({outputs!r}, "w") as _fh:
    json.dump(result, _fh, default=str)
"""


class DynamicUserTool(Tool):
    """Adapter that lets the Executor invoke a user-approved tool the
    same way it invokes a builtin."""

    def __init__(self, user_tool: UserTool) -> None:
        self._user_tool = user_tool
        self.name = user_tool.name
        self.description = user_tool.description
        self.input_schema = user_tool.signature or {
            "type": "object", "properties": {},
        }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        if ctx.task_id is None:
            raise ToolError(
                f"user-tool {self.name!r} requires a Task context — the"
                f" Executor only dispatches it inside a task pipeline."
            )

        # 1. JSON-encode the inputs once, fail loudly if a caller
        #    passed something un-serializable.
        try:
            inputs_bytes = json.dumps(inputs).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise ToolError(
                f"user-tool {self.name!r} inputs are not"
                f" JSON-serializable: {e}"
            ) from e

        # 2. Stage in the sandbox.
        sandbox = await get_manager().get(ctx.user_id, ctx.task_id)
        await sandbox.write_file(_INPUTS_BASENAME, inputs_bytes)

        # 3. Execute the wrapped user code. The wrapper is constant;
        #    only `body` varies, and it's the user-approved Python
        #    that the user already reviewed via ask_user.
        script = _WRAPPER_TEMPLATE.format(
            inputs=_INPUTS_BASENAME,
            outputs=_OUTPUT_BASENAME,
            body=textwrap.dedent(self._user_tool.implementation).rstrip(),
        )
        run = await sandbox.run_python(script)
        if run.timed_out:
            raise ToolError(
                f"user-tool {self.name!r} timed out after"
                f" {run.elapsed_seconds:.1f}s"
            )
        if run.exit_code != 0:
            err = run.stderr.strip() or run.stdout.strip() or "no output"
            raise ToolError(
                f"user-tool {self.name!r} crashed (exit_code={run.exit_code}):"
                f" {err}"
            )

        # 4. Read the result back. A missing output.json means the
        #    user's code returned cleanly but never set `result` — the
        #    wrapper script already raises in that case, so this branch
        #    is for truly degenerate failures (sandbox FS issue, etc.).
        try:
            output_bytes = await sandbox.read_file(_OUTPUT_BASENAME)
        except FileNotFoundError as e:
            raise ToolError(
                f"user-tool {self.name!r} produced no {_OUTPUT_BASENAME}"
            ) from e
        try:
            return json.loads(output_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ToolError(
                f"user-tool {self.name!r} wrote non-JSON output: {e}"
            ) from e
