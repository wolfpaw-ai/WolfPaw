"""Post-Evaluator — scores a completed plan execution.

Runs synchronously in the Router after the Executor finishes. Uses
Haiku (cheap + fast) with a *forced* `record_score` tool_use so the
verdict is structured. The score gets written back into procedural
memory so the Planner can use it on future similar requests; a
`task_events` row records the full verdict for observability.

Skills auto-emission (turning a high-scoring reusable plan into a new
named Skill) stays v2 per the implementation plan. v1 ships scoring +
persistence; the marketplace flow + auto-emission land later.

Failure posture: if the Post-Evaluator itself errors out, the Router
returns the Executor's answer anyway and logs a warning. Scoring is
purely for the recipe-box — it must not block user response.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.schemas import ExecutionPlan, Plan, PostEvalVerdict, StepStatus
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()


_RECORD_SCORE_TOOL = {
    "name": "record_score",
    "description": "Record the score + diagnosis for a completed plan execution.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": (
                    "0-100: 100 = served the user's request perfectly,"
                    " 70+ = good, 40-70 = mixed, <40 = poor, 0 = total"
                    " failure."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One-sentence verdict on how the plan went.",
            },
            "what_went_well": {
                "type": "string",
                "description": "Brief — what the plan did right.",
            },
            "what_went_wrong": {
                "type": "string",
                "description": (
                    "Brief — what the plan did wrong / could have done"
                    " differently. Empty if nothing went wrong."
                ),
            },
            "improvements": {
                "type": "string",
                "description": (
                    "Concrete suggestions for how a future plan addressing"
                    " a similar request should differ. Empty if none."
                ),
            },
        },
        "required": ["score", "summary"],
    },
}


_SYSTEM_PROMPT = """You are Wolfpaw's Post-Evaluator. You score how well a completed plan execution served the user's original request, on a 0-100 scale, and write a brief diagnosis.

Scoring guidance:
- **100**: served the request completely + cleanly + efficiently.
- **70–99**: served the request well; small issues or rough edges.
- **40–69**: partially served the request; missing pieces or notable inefficiencies.
- **1–39**: poorly served the request; major gaps.
- **0**: total failure (no usable output, or wrong direction entirely).

What to weigh:
- Did the final answer actually address what the user asked for?
- Were the steps efficient (not over-planned, not under-planned)?
- Did tools succeed cleanly? Were there avoidable failures?
- Was the plan's approach reasonable given the seeded skills + past plans available?

This is feedback for **future plans** about similar requests — be honest. A "good" plan should score 70+. Reserve 90+ for genuinely excellent work. The user has already seen the answer; your job is to teach the Planner.

You MUST call the `record_score` tool exactly once. Do not respond with prose."""


class PostEvaluatorAgent:
    AGENT_KIND = "post_evaluator"
    VERSION_LABEL = "v1"

    def __init__(self, *, model_client: ModelClient | None = None) -> None:
        self._model_client = model_client
        self._prompt_version_id: UUID | None = None
        self._prompt_seeded = False

    @property
    def model_client(self) -> ModelClient:
        return self._model_client or get_model_client()

    async def _ensure_prompt_version(self) -> None:
        if self._prompt_seeded:
            return
        self._prompt_seeded = True
        try:
            async with acquire() as conn:
                row = await bump_prompt_version(
                    conn,
                    agent=self.AGENT_KIND,
                    version_label=self.VERSION_LABEL,
                    content_template={
                        "system": _SYSTEM_PROMPT,
                        "tool": _RECORD_SCORE_TOOL,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode is fine
            log.warning(
                "agents.post_evaluator.prompt_version_seed_failed",
                exc_info=True,
            )

    async def evaluate(
        self,
        *,
        ctx: ToolContext,
        plan: Plan,
        execution: ExecutionPlan,
    ) -> PostEvalVerdict:
        await self._ensure_prompt_version()
        settings = get_settings()

        prompt = _build_eval_prompt(plan, execution)
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_post_evaluator,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM_PROMPT,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
            tools=[_RECORD_SCORE_TOOL],
            tool_choice={"type": "tool", "name": "record_score"},
        )

        for block in getattr(result.raw, "content", []) or []:
            if (
                _block_type(block) == "tool_use"
                and _block_name(block) == "record_score"
            ):
                payload = _block_input(block) or {}
                score = _clamp_int(payload.get("score"), 0, 100, default=0)
                return PostEvalVerdict(
                    score=score,
                    summary=str(payload.get("summary", "")),
                    what_went_well=str(payload.get("what_went_well", "")),
                    what_went_wrong=str(payload.get("what_went_wrong", "")),
                    improvements=str(payload.get("improvements", "")),
                )

        log.warning(
            "agents.post_evaluator.no_tool_use_block",
            user_id=str(ctx.user_id),
        )
        # Fall back to a neutral verdict so the Router still has something
        # to write. Mark as 50 if the executor succeeded, 0 if it didn't.
        fallback_score = 50 if execution.success else 0
        return PostEvalVerdict(
            score=fallback_score,
            summary="(post-evaluator returned no structured verdict; defaulted)",
        )


# --- helpers ---------------------------------------------------------------


def _build_eval_prompt(plan: Plan, execution: ExecutionPlan) -> str:
    lines = [
        "# User request",
        plan.query,
        "",
        "# Plan summary",
        plan.summary,
        "",
        "# Steps the plan took",
    ]
    for s in plan.steps:
        tool_part = f" — `{s.tool}`" if s.tool else ""
        para_part = (
            f" (parallel group {s.parallel_group})"
            if s.parallel_group is not None else ""
        )
        lines.append(f"- {s.id} [{s.kind}]{tool_part}: {s.description}{para_part}")

    lines.extend(["", "# Step outcomes"])
    for r in execution.results:
        if r.status == StepStatus.COMPLETED:
            out_repr = json.dumps(r.output, default=str)
            if len(out_repr) > 1500:
                out_repr = out_repr[:1500] + " …(truncated)"
            lines.append(f"- {r.step_id} ({r.kind}): COMPLETED — {out_repr}")
        elif r.status == StepStatus.FAILED:
            lines.append(f"- {r.step_id} ({r.kind}): FAILED — {r.error or ''}")
        else:
            lines.append(f"- {r.step_id} ({r.kind}): {r.status.value}")

    lines.extend([
        "",
        "# Final answer Wolfpaw returned to the user",
        execution.final_answer,
        "",
        f"# Execution succeeded: {execution.success}",
    ])
    if execution.error:
        lines.append(f"# Execution error: {execution.error}")

    lines.extend([
        "",
        "Score this on the 0-100 scale and call the `record_score` tool now.",
    ])
    return "\n".join(lines)


def _clamp_int(value: Any, lo: int, hi: int, *, default: int) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, i))


def _block_type(block):
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_name(block):
    if isinstance(block, dict):
        return block.get("name")
    return getattr(block, "name", None)


def _block_input(block):
    if isinstance(block, dict):
        return block.get("input")
    return getattr(block, "input", None)


_agent: PostEvaluatorAgent | None = None


def get_post_evaluator_agent() -> PostEvaluatorAgent:
    global _agent
    if _agent is None:
        _agent = PostEvaluatorAgent()
    return _agent


def reset_post_evaluator_agent() -> None:
    global _agent
    _agent = None
