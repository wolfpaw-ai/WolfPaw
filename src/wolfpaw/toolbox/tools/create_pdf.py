"""`create_pdf` — render HTML to a PDF via weasyprint.

weasyprint has system dependencies (Pango, Cairo, GDK-PixBuf) so it
ships behind the optional `[pdf]` extra. The tool registers
unconditionally, but the runtime call will surface a clear ToolError if
weasyprint isn't installed in the sandbox.
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

OUTPUT_BASENAME = "output.pdf"
MIME = "application/pdf"

SCRIPT = """
import json
try:
    from weasyprint import HTML
except ImportError as e:
    raise SystemExit(
        "weasyprint is not installed in this sandbox."
        " Install with `pip install wolfpaw[pdf]` (host)"
        " or bake into the sandbox image (docker/e2b)."
    )

with open("inputs.json") as f:
    spec = json.load(f)

HTML(string=spec["html"]).write_pdf("output.pdf")
"""


@register_tool
class CreatePdfTool(Tool):
    name = "create_pdf"
    description = (
        "Render an HTML document to a PDF in the user's workspace."
        " Use http/css for layout; the agent is responsible for"
        " converting markdown to HTML if needed."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "html": {"type": "string"},
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["filename", "html"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        filename = inputs.get("filename")
        html = inputs.get("html")
        if not isinstance(filename, str) or not filename.endswith(".pdf"):
            raise ToolError("`filename` must end with .pdf")
        if not isinstance(html, str) or not html.strip():
            raise ToolError("`html` must be a non-empty string")
        return await emit_artifact(
            ctx,
            ArtifactSpec(
                filename=filename,
                mime_type=MIME,
                sandbox_inputs={"html": html},
                sandbox_script=SCRIPT,
                output_basename=OUTPUT_BASENAME,
                overwrite=bool(inputs.get("overwrite")),
            ),
        )
