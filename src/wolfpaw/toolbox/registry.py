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


class Tool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]

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

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def names(self) -> list[str]:
        return sorted(self._tools)


_registry = Registry()


def get_registry() -> Registry:
    return _registry


def register_tool(tool_cls: type[Tool]) -> type[Tool]:
    """Decorator: instantiate the class and add it to the global registry."""
    _registry.register(tool_cls())
    return tool_cls
