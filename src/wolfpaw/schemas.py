"""Shared cross-package dataclasses.

The Plan / Step shapes the Planner emits live here. The Executor
produces StepResults + an ExecutionPlan from a Plan — those shapes also
live here so the Router and any future consumer can render them without
importing the agents package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal
from uuid import UUID

StepKind = Literal["functional", "reasoning", "evaluation"]


class StepStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


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


@dataclass(frozen=True)
class StepResult:
    """Outcome of running one Step. `output` is the tool result dict for
    functional steps, the model's text for reasoning/evaluation steps,
    and None for skipped/not-started steps."""

    step_id: str
    kind: StepKind
    status: StepStatus
    output: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class ExecutionPlan:
    """What the Executor returns. `results` are in execution order
    (multiple parallel-group steps still slot into the list at their
    sequential position). `final_answer` is the synthesized user-facing
    response (or an error summary on failure)."""

    plan: Plan
    results: list[StepResult]
    final_answer: str
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class PostEvalVerdict:
    """Post-Evaluator's scoring output (step 14).

    `score` is on a 0-100 scale: 100 = the plan + execution served the
    user's request perfectly; 70+ = good; 40-70 = mixed; <40 = poor;
    0 = total failure. The Planner reads `score` (via procedural memory)
    to decide whether to adapt or reject a past plan on similar future
    requests."""

    score: int
    summary: str
    what_went_well: str = ""
    what_went_wrong: str = ""
    improvements: str = ""

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "summary": self.summary,
            "what_went_well": self.what_went_well,
            "what_went_wrong": self.what_went_wrong,
            "improvements": self.improvements,
        }
