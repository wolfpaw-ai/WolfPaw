"""Shared cross-package dataclasses.

The Plan / Step shapes the Planner emits and the Executor consumes live
here so neither package has to import the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

StepKind = Literal["functional", "reasoning", "evaluation"]


@dataclass(frozen=True)
class Step:
    """One step in a Plan.

    - `functional` steps invoke a named tool with `inputs` (a dict matching
      the tool's input_schema).
    - `reasoning` steps are model calls (Sonnet by default) prompted with
      `description` + the accumulated step results.
    - `evaluation` steps are model-graded checks (also Sonnet) that decide
      whether to continue, retry, or branch.

    `parallel_group` ties steps that may run concurrently within the same
    plan — the Executor (step 13) dispatches all steps sharing a group id
    in parallel. None = sequential.
    """

    id: str
    kind: StepKind
    description: str
    tool: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    parallel_group: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "tool": self.tool,
            "inputs": dict(self.inputs),
            "parallel_group": self.parallel_group,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        kind = data.get("kind", "functional")
        return cls(
            id=str(data.get("id", "")),
            kind=kind if kind in ("functional", "reasoning", "evaluation") else "functional",
            description=str(data.get("description", "")),
            tool=data.get("tool"),
            inputs=dict(data.get("inputs") or {}),
            parallel_group=data.get("parallel_group"),
        )


@dataclass(frozen=True)
class Plan:
    """The Planner's output. `id` is set once the plan is persisted into
    procedural memory; `None` before that. `summary` is one-paragraph
    prose explaining the plan to the user."""

    query: str
    summary: str
    steps: list[Step]
    is_task: bool = False
    model_used: str = ""
    adapted_from_past_plan_id: UUID | None = None
    applied_skill_name: str | None = None
    id: UUID | None = None

    def to_steps_jsonb(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.steps]
