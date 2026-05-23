"""`create_chart` — render a chart from structured data via matplotlib.

Supports `line`, `bar`, and `scatter` for v1. Output format inferred
from the filename extension (`.png` or `.svg`).
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

_VALID_TYPES = {"line", "bar", "scatter"}

SCRIPT = """
import json
import matplotlib
matplotlib.use("Agg")  # no display, no GUI backend
import matplotlib.pyplot as plt

with open("inputs.json") as f:
    spec = json.load(f)

fig, ax = plt.subplots(figsize=(8, 5))
chart_type = spec["type"]
for s in spec["series"]:
    x = s["x"]
    y = s["y"]
    label = s.get("name", "")
    if chart_type == "line":
        ax.plot(x, y, label=label)
    elif chart_type == "bar":
        ax.bar(x, y, label=label)
    elif chart_type == "scatter":
        ax.scatter(x, y, label=label)

if spec.get("title"):    ax.set_title(spec["title"])
if spec.get("x_label"):  ax.set_xlabel(spec["x_label"])
if spec.get("y_label"):  ax.set_ylabel(spec["y_label"])
if any(s.get("name") for s in spec["series"]):
    ax.legend()

fig.tight_layout()
fig.savefig(spec["output"])
"""


def _mime_for(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".svg"):
        return "image/svg+xml"
    raise ToolError("`filename` must end with .png or .svg")


@register_tool
class CreateChartTool(Tool):
    name = "create_chart"
    description = (
        "Render a line, bar, or scatter chart from data, save to the"
        " user's workspace as PNG or SVG."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "type": {"type": "string", "enum": sorted(_VALID_TYPES)},
            "title": {"type": "string"},
            "x_label": {"type": "string"},
            "y_label": {"type": "string"},
            "series": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "x": {"type": "array"},
                        "y": {"type": "array"},
                    },
                    "required": ["x", "y"],
                },
            },
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["filename", "type", "series"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        filename = inputs.get("filename")
        chart_type = inputs.get("type")
        series = inputs.get("series")
        if not isinstance(filename, str):
            raise ToolError("`filename` must be a string")
        mime = _mime_for(filename)
        if chart_type not in _VALID_TYPES:
            raise ToolError(f"`type` must be one of {sorted(_VALID_TYPES)}")
        if not isinstance(series, list) or not series:
            raise ToolError("`series` must be a non-empty list")
        for s in series:
            if (
                not isinstance(s, dict)
                or not isinstance(s.get("x"), list)
                or not isinstance(s.get("y"), list)
                or len(s["x"]) != len(s["y"])
            ):
                raise ToolError("each series needs equal-length `x` and `y` arrays")

        output_basename = "output.png" if mime == "image/png" else "output.svg"
        sandbox_inputs = {
            "type": chart_type,
            "title": inputs.get("title"),
            "x_label": inputs.get("x_label"),
            "y_label": inputs.get("y_label"),
            "series": series,
            "output": output_basename,
        }
        return await emit_artifact(
            ctx,
            ArtifactSpec(
                filename=filename,
                mime_type=mime,
                sandbox_inputs=sandbox_inputs,
                sandbox_script=SCRIPT,
                output_basename=output_basename,
                overwrite=bool(inputs.get("overwrite")),
            ),
        )
