"""Calculator AST-whitelist tests. The hostile cases matter as much as
the happy path here — eval-based tools are easy to get wrong."""

from __future__ import annotations

from uuid import uuid4

import pytest

from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry


def _ctx():
    return ToolContext(user_id=uuid4())


async def test_basic_arithmetic():
    calc = get_registry().get("calculator")
    out = await calc.run(_ctx(), expression="(3 + 4) * 2 - 1")
    assert out["result"] == 13


async def test_division_and_power():
    calc = get_registry().get("calculator")
    assert (await calc.run(_ctx(), expression="2 ** 10"))["result"] == 1024
    assert (await calc.run(_ctx(), expression="7 / 2"))["result"] == 3.5
    assert (await calc.run(_ctx(), expression="7 // 2"))["result"] == 3
    assert (await calc.run(_ctx(), expression="7 % 2"))["result"] == 1


async def test_unary_minus():
    calc = get_registry().get("calculator")
    assert (await calc.run(_ctx(), expression="-5 + 2"))["result"] == -3


async def test_blocks_name_lookups():
    """`x` would resolve to a variable. Whitelist rejects it before eval."""
    calc = get_registry().get("calculator")
    with pytest.raises(ToolError):
        await calc.run(_ctx(), expression="x + 1")


async def test_blocks_function_calls():
    calc = get_registry().get("calculator")
    with pytest.raises(ToolError):
        await calc.run(_ctx(), expression="__import__('os').system('ls')")
    with pytest.raises(ToolError):
        await calc.run(_ctx(), expression="abs(-5)")


async def test_blocks_attribute_access():
    calc = get_registry().get("calculator")
    with pytest.raises(ToolError):
        await calc.run(_ctx(), expression="(1).__class__")


async def test_division_by_zero():
    calc = get_registry().get("calculator")
    with pytest.raises(ToolError, match="arithmetic"):
        await calc.run(_ctx(), expression="1 / 0")


async def test_syntax_error():
    calc = get_registry().get("calculator")
    with pytest.raises(ToolError, match="parse"):
        await calc.run(_ctx(), expression="3 +")


async def test_requires_string_expression():
    calc = get_registry().get("calculator")
    with pytest.raises(ToolError):
        await calc.run(_ctx(), expression=42)
