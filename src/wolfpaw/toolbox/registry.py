"""Tool abstraction + global registry.

A `Tool` is anything the agent can call: it has a name, a description,
a JSON-schema input signature (the same shape Anthropic's tool-use API
expects), and an async `run(user_id, **inputs)` that returns a JSON-safe
result dict.

Tools are registered with the global `Registry` via the `@register_tool`
decorator at module load. The Quick Agent (step 10) and the Executor
(step 13) will resolve tools through `registry.get(name)`.

Tool errors come in two shapes:
- `ToolError` — recoverable, the agent gets the message and can retry
- `ToolFatalError` — unrecoverable, executor aborts the step

Tools that touch the user's workspace can raise `WorkspaceCollision`
(from `wolfpaw.workspace.files`); the executor treats that specially
(awaiting_user) once tasks lifecycle lands in step 15.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class ToolError(Exception):
    """Recoverable tool failure (bad input, transient downstream error)."""


class ToolFatalError(Exception):
    """Unrecoverable tool failure — executor should abort the step."""


@dataclass(frozen=True)
class ToolContext:
    """What every tool gets alongside its inputs. Threaded through by the
    executor so tools never have to reach into globals for the trace_id,
    task_id, etc."""

    user_id: UUID
    task_id: UUID | None = None
    trace_id: str | None = None
    # Originating channel of the current turn ('web' | 'telegram' |
    # 'slack' | ...). Threads are channel-agnostic, but each persisted
    # message records where it came from via `messages.metadata.channel`.
    channel: str | None = None


class Tool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]
    # True if this tool can only run inside a Task (needs a `task_id` to
    # pause/resume). Plans containing such a tool are forced onto the Task
    # path — see planner `_requires_task_context`.
    requires_task_context: bool = False

    @abstractmethod
    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        """Execute the tool. Must return a JSON-serializable dict."""

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Convert to the shape Anthropic's `messages.create(tools=...)` expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("tool must have a non-empty name")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as e:
            raise KeyError(f"unknown tool: {name!r}") from e

    def try_get(self, name: str) -> Tool | None:
        """Like `get`, but returns None for an unregistered name instead of
        raising — for callers inspecting possibly-unknown tool references."""
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def catalog_block(self) -> str:
        """Markdown summary of every registered tool, suitable for
        inlining into the Planner's system prompt (step 28+ fix to
        the planner-doesn't-know-inputs problem).

        Format per tool::

            - `tool_name(req_arg: type, opt_arg?: type)` — description
              [continued indented line if the description is long]

        Required vs optional is read from each tool's ``input_schema``.
        Type is the JSON-schema ``type`` value; complex types collapse
        to ``object`` / ``array``. The Planner uses this to choose
        correct tool names AND populate ``inputs`` correctly on
        functional steps."""
        lines: list[str] = ["## Available builtin tools"]
        for tool in self.all():
            sig = _format_tool_signature(tool)
            desc = (tool.description or "").strip().split("\n", 1)[0]
            lines.append(f"- `{tool.name}({sig})` — {desc}")
        return "\n".join(lines)


def _format_tool_signature(tool: Tool) -> str:
    """Produce a `(arg1: type, arg2?: type)` signature string from the
    tool's JSON-schema input_schema. Required args first, optionals
    last; optionals are suffixed with `?`."""
    schema = tool.input_schema or {}
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts: list[str] = []
    # Required first.
    for name in sorted(required):
        if name in props:
            parts.append(f"{name}: {_type_name(props[name])}")
    # Optionals after, alphabetical.
    for name in sorted(set(props.keys()) - required):
        parts.append(f"{name}?: {_type_name(props[name])}")
    return ", ".join(parts)


def _type_name(prop: dict[str, Any]) -> str:
    """Collapse a JSON-schema property to a short type name."""
    t = prop.get("type", "any")
    if isinstance(t, list):
        # JSON schema allows ["string", "null"] etc.
        t = next((x for x in t if x != "null"), "any")
    return str(t)


_registry = Registry()


def get_registry() -> Registry:
    return _registry


def register_tool(tool_cls: type[Tool]) -> type[Tool]:
    """Decorator: instantiate the class and add it to the global registry."""
    _registry.register(tool_cls())
    return tool_cls
