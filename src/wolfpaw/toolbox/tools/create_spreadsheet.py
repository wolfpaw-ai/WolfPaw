"""`create_spreadsheet` — build an .xlsx with one or more sheets via openpyxl.

Input shape:
    {
      "filename": "report.xlsx",
      "sheets": [
        {"name": "Sales", "rows": [["Date", "Revenue"], ["2026-01", 1200]]}
      ],
      "overwrite": false
    }

Runs entirely inside the sandbox. openpyxl is a base dep so the
SubprocessSandbox child can `import openpyxl` via the host venv; for
Docker/E2B the libraries must be pre-baked into the image/template.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.toolbox.tools._artifact import ArtifactSpec, emit_artifact

OUTPUT_BASENAME = "output.xlsx"
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SCRIPT = """
import json
import openpyxl

with open("inputs.json") as f:
    spec = json.load(f)

wb = openpyxl.Workbook()
# Drop the default sheet — we add our own from spec.
wb.remove(wb.active)
for sheet in spec["sheets"]:
    ws = wb.create_sheet(title=sheet["name"][:31])  # excel sheet-name cap
    for row in sheet.get("rows", []):
        ws.append(row)
wb.save("output.xlsx")
"""


@register_tool
class CreateSpreadsheetTool(Tool):
    name = "create_spreadsheet"
    description = (
        "Create a .xlsx workbook in the user's workspace. Accepts one or"
        " more sheets, each a name + 2D array of cell values."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "sheets": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                        },
                    },
                    "required": ["name", "rows"],
                },
            },
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["filename", "sheets"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        filename = inputs.get("filename")
        sheets = inputs.get("sheets")
        if not isinstance(filename, str) or not filename.endswith(".xlsx"):
            raise ToolError("`filename` must end with .xlsx")
        if not isinstance(sheets, list) or not sheets:
            raise ToolError("`sheets` must be a non-empty list")
        for s in sheets:
            if not isinstance(s, dict) or "name" not in s or "rows" not in s:
                raise ToolError("each sheet needs `name` and `rows`")
        return await emit_artifact(
            ctx,
            ArtifactSpec(
                filename=filename,
                mime_type=MIME,
                sandbox_inputs={"sheets": sheets},
                sandbox_script=SCRIPT,
                output_basename=OUTPUT_BASENAME,
                overwrite=bool(inputs.get("overwrite")),
            ),
        )
