"""`create_slides` — build a .pptx deck via python-pptx.

Each slide has a `title` and either a `bullets` list (one paragraph per
bullet) or a `body` string. The first matching content type wins; you
can leave both unset for a title-only slide.
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

OUTPUT_BASENAME = "output.pptx"
MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

SCRIPT = """
import json
from pptx import Presentation

with open("inputs.json") as f:
    spec = json.load(f)

prs = Presentation()
title_layout = prs.slide_layouts[0]      # title slide
content_layout = prs.slide_layouts[1]    # title + content

for i, slide_def in enumerate(spec["slides"]):
    layout = title_layout if i == 0 and not (slide_def.get("bullets") or slide_def.get("body")) else content_layout
    slide = prs.slides.add_slide(layout)
    if slide.shapes.title and slide_def.get("title"):
        slide.shapes.title.text = slide_def["title"]
    # Content placeholder is shape index 1 on the content_layout.
    if len(slide.placeholders) > 1:
        body = slide.placeholders[1]
        tf = body.text_frame
        bullets = slide_def.get("bullets")
        if bullets:
            tf.text = bullets[0]
            for line in bullets[1:]:
                p = tf.add_paragraph()
                p.text = line
        elif slide_def.get("body"):
            tf.text = slide_def["body"]

prs.save("output.pptx")
"""


@register_tool
class CreateSlidesTool(Tool):
    name = "create_slides"
    description = (
        "Create a .pptx deck in the user's workspace. Each slide takes a"
        " `title` plus either a `bullets` array or a free-form `body`."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "slides": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "body": {"type": "string"},
                    },
                },
            },
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["filename", "slides"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        filename = inputs.get("filename")
        slides = inputs.get("slides")
        if not isinstance(filename, str) or not filename.endswith(".pptx"):
            raise ToolError("`filename` must end with .pptx")
        if not isinstance(slides, list) or not slides:
            raise ToolError("`slides` must be a non-empty list")
        for s in slides:
            if not isinstance(s, dict):
                raise ToolError("each slide must be an object")
        return await emit_artifact(
            ctx,
            ArtifactSpec(
                filename=filename,
                mime_type=MIME,
                sandbox_inputs={"slides": slides},
                sandbox_script=SCRIPT,
                output_basename=OUTPUT_BASENAME,
                overwrite=bool(inputs.get("overwrite")),
            ),
        )
