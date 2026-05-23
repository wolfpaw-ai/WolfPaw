"""`calculator` — safe arithmetic via AST whitelist.

No names, no function calls, no attribute access, no comprehensions —
just numeric literals, the four standard ops, power, modulo, floor div,
and unary +/−. Returning a single number is on purpose; multi-step
calculations the agent does step-by-step.
"""

from __future__ import annotations

import ast
from typing import Any

from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)

_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_UNARY_OPS = (ast.UAdd, ast.USub)
_NODE_WHITELIST = (
    ast.Expression,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
) + _BIN_OPS + _UNARY_OPS


def _check_node(node: ast.AST) -> None:
    if not isinstance(node, _NODE_WHITELIST):
        raise ToolError(f"disallowed expression: {type(node).__name__}")
    if isinstance(node, ast.Constant) and not isinstance(
        node.value, (int, float)
    ):
        raise ToolError(f"only numeric constants allowed, got {node.value!r}")
    for child in ast.iter_child_nodes(node):
        _check_node(child)


def _evaluate(expression: str) -> float:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ToolError(f"could not parse expression: {e.msg}") from e
    _check_node(tree)
    try:
        result = eval(  # noqa: S307 — AST whitelisted above
            compile(tree, "<calculator>", "eval"),
            {"__builtins__": {}},
            {},
        )
    except (ZeroDivisionError, ValueError, OverflowError) as e:
        raise ToolError(f"arithmetic error: {e}") from e
    return result


@register_tool
class CalculatorTool(Tool):
    name = "calculator"
    description = (
        "Evaluate a single arithmetic expression. Supports +, -, *, /, //, %, **,"
        " and unary -/+. No variables, no function calls. Returns one number."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Arithmetic expression, e.g. '(3 + 4) * 2'.",
            }
        },
        "required": ["expression"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        expression = inputs.get("expression")
        if not isinstance(expression, str):
            raise ToolError("`expression` must be a string")
        result = _evaluate(expression)
        return {"expression": expression, "result": result}
