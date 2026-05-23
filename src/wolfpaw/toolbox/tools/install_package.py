"""`install_package` — `pip install` into the task's sandbox.

Whether installs actually succeed depends on the provider:
  - subprocess (dev): installs to a sandbox-local `site-packages/` dir
  - docker (self-host): currently disabled (network_disabled=True)
  - e2b (hosted): supported

The tool returns the provider's log on failure so the agent can see why.
"""

from __future__ import annotations

import re
from typing import Any

from wolfpaw.sandbox import get_manager
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)

# Conservative pattern matching valid PyPI distribution names + optional
# version constraints. Rejects shell metacharacters outright.
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_.,\-]+\])?([<>=!~]=?[A-Za-z0-9_.\-]+)?$")


@register_tool
class InstallPackageTool(Tool):
    name = "install_package"
    description = (
        "Install a PyPI package into the task's sandbox. Package becomes"
        " importable for subsequent `run_python` calls."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "package": {
                "type": "string",
                "description": "PyPI distribution name, optionally with version (e.g. 'numpy==1.26').",
            }
        },
        "required": ["package"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        package = inputs.get("package")
        if not isinstance(package, str) or not _PACKAGE_RE.match(package):
            raise ToolError("`package` must be a valid PyPI distribution spec")
        sandbox = await get_manager().get(ctx.user_id, ctx.task_id)
        result = await sandbox.install_package(package)
        return {"package": result.package, "ok": result.ok, "log": result.log}
