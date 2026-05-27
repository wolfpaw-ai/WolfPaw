"""Tool registry + base abstractions."""

from __future__ import annotations

import pytest

# Importing the package triggers registration of the v1 tool set.
import wolfpaw.toolbox  # noqa: F401
from wolfpaw.toolbox.registry import Registry, Tool, get_registry


def test_v1_tool_set_registered():
    names = set(get_registry().names())
    expected_v1 = {
        # step 7 (info & data + docs)
        "calculator", "create_table", "http_get", "read_doc",
        "sql_query", "web_search", "write_doc",
        # step 8 (sandbox)
        "run_python", "install_package",
        "sandbox_read_file", "sandbox_write_file",
        # step 9 (artifact production)
        "create_spreadsheet", "create_chart",
        "create_slides", "create_pdf",
        # step 15 (HITL)
        "ask_user",
    }
    # Subset check so Phase C+ integration tools (Dropbox, Notion,
    # Microsoft Calendar) registered as a side-effect of other test
    # imports don't break this assertion. The v1 set must always be
    # present.
    assert expected_v1.issubset(names), (
        f"missing v1 tools: {expected_v1 - names}"
    )


def test_anthropic_schema_shape():
    calc = get_registry().get("calculator")
    schema = calc.to_anthropic_schema()
    assert schema["name"] == "calculator"
    assert "description" in schema
    assert schema["input_schema"]["type"] == "object"


def test_register_rejects_empty_name():
    class NoName(Tool):
        name = ""
        description = "x"
        input_schema = {"type": "object", "properties": {}}

        async def run(self, ctx, **inputs):
            return {}

    r = Registry()
    with pytest.raises(ValueError):
        r.register(NoName())


def test_unknown_tool_raises():
    with pytest.raises(KeyError):
        get_registry().get("not-a-real-tool")


# --- catalog_block (planner-prompt fix) ----------------------------------


def test_catalog_block_marks_required_vs_optional_inputs():
    """`required` schema fields appear without `?`, optionals get `?`.
    Required ones come first in the signature."""
    r = Registry()

    class _T(Tool):
        name = "x"
        description = "demo"
        input_schema = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["filename"],
        }

        async def run(self, ctx, **inputs):
            return {}

    r.register(_T())
    block = r.catalog_block()
    assert "## Available builtin tools" in block
    # Required first, no `?`. Optional with `?`.
    assert "`x(filename: string, limit?: integer)`" in block
    assert "demo" in block


def test_catalog_block_renders_every_registered_tool():
    """The full builtin registry should be coverable via the catalog."""
    names = set(get_registry().names())
    block = get_registry().catalog_block()
    for name in names:
        assert f"`{name}(" in block, f"missing {name!r} in catalog"


def test_catalog_block_truncates_multiline_description():
    """If a tool's description spans multiple lines, only the first
    line should land in the catalog (keep the prompt compact)."""
    r = Registry()

    class _T(Tool):
        name = "multi"
        description = "first line\nlonger explanation\nthird line"
        input_schema = {"type": "object", "properties": {}}

        async def run(self, ctx, **inputs):
            return {}

    r.register(_T())
    block = r.catalog_block()
    assert "first line" in block
    assert "longer explanation" not in block
