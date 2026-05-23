"""Tool registry + base abstractions."""

from __future__ import annotations

import pytest

# Importing the package triggers registration of the v1 tool set.
import wolfpaw.toolbox  # noqa: F401
from wolfpaw.toolbox.registry import Registry, Tool, get_registry


def test_v1_tool_set_registered():
    names = get_registry().names()
    assert set(names) == {
        # step 7 (info & data + docs)
        "calculator", "create_table", "http_get", "read_doc",
        "sql_query", "web_search", "write_doc",
        # step 8 (sandbox)
        "run_python", "install_package",
        "sandbox_read_file", "sandbox_write_file",
        # step 9 (artifact production)
        "create_spreadsheet", "create_chart",
        "create_slides", "create_pdf",
    }


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
