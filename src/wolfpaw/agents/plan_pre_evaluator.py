"""Plan Pre-Evaluator — sanity-check the Planner's output before execution.

Runs synchronously between the Planner and the Executor. Three forced
checks via a single ``evaluate_plan`` tool_use, each a boolean:

1. **achieves_objective** — would executing this plan actually answer
   what the user asked? (Caught: the Planner generated steps that
   tangentially touch the problem but never close the loop.)
2. **simplifiable** — does it have steps that can be merged or
   dropped without losing the result? (Caught: gratuitous parallelism,
   redundant evaluation steps.)
3. **better_than_past_plans** — given the highest-scoring past plan
   the Planner pulled from procedural memory, is the new draft at
   least as good? If a 95-scored past plan exists for the same query
   and the Planner produced something demonstrably worse, that's a
   signal to retry.

A plan is **approved** iff all three checks pass. When rejected, the
``diagnosis`` field is the prose explanation the Planner gets as
additional context on its second-pass attempt — see
:func:`plan_with_pre_evaluation` for the retry shape.

Failure posture: the Pre-Evaluator is best-effort. If the model call
fails or returns no tool_use block, we *approve by default* and ship
the Planner's plan unchanged. The cost of an unreviewed plan is
worse-than-optimal output; the cost of blocking on a misbehaving
evaluator is no output at all.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.agents.planner import PlannerAgent
from wolfpaw.config import get_settings
from wolfpaw.memory import procedural
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.persona.builder import build_for_agent
from wolfpaw.schemas import Plan, PreEvalVerdict
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]


_EVALUATE_PLAN_TOOL = {
    "name": "evaluate_plan",
    "description": (
        "Record the pre-execution assessment of the Planner's draft."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "achieves_objective": {
                "type": "boolean",
                "description": (
                    "True iff executing this plan would actually answer"
                    " the user's request. False if the plan tangentially"
                    " touches the problem but never closes the loop."
                ),
            },
            "simplifiable": {
                "type": "boolean",
                "description": (
                    "True iff there's at least one step that could be"
                    " merged, dropped, or replaced with a simpler step"
                    " without changing the outcome. False if the plan"
                    " is already as lean as it should be."
                ),
            },
            "better_than_past_plans": {
                "type": "boolean",
                "description": (
                    "True iff this plan is at least as good as the"
                    " highest-scoring past plan you were shown (or"
                    " True if no past plans were shown). False only"
                    " when a past plan demonstrably outperforms this"
                    " draft on the same kind of request."
                ),
            },
            "diagnosis": {
                "type": "string",
                "description": (
                    "One-paragraph prose explanation of the verdict."
                    " When approving, a one-sentence note on why."
                    " When rejecting, concrete + actionable feedback"
                    " the Planner can use on a retry: which step is"
                    " wrong, what's missing, what to drop, etc."
                ),
            },
        },
        "required": [
            "achieves_objective", "simplifiable",
            "better_than_past_plans", "diagnosis",
        ],
    },
}


_AGENT_ROLE = """You are the **Plan Pre-Evaluator**. You're handed a fresh plan from the Planning Agent — your job is to vet it *before* the Executor runs it, so we don't burn tokens + compute on a plan that won't work or could be much simpler.

Three checks, each a boolean:

1. **achieves_objective** — would executing this plan actually answer what the user asked? Look at the steps end to end: do they produce the deliverable the user wants, or do they just touch the problem? A plan that's "research X and think about it" probably doesn't achieve the objective if the user wanted a spreadsheet.

2. **simplifiable** — could this plan be tightened without losing the result? Common smells: a `reasoning` step right after a `functional` step whose output is already what the user needs; two parallel branches that do the same lookup; an `evaluation` step that adds nothing the user will see. Be pragmatic — *some* checks are useful. Reject only on gratuitous complexity.

3. **better_than_past_plans** — if you were shown a high-scoring past plan for a similar query (in the "Past plans for context" section), is this new draft at least as good? Past plans with `score >= 80` are the bar. If a past 95-scored plan handled this exact pattern with three steps and the new draft has seven that do the same thing, the new draft loses.

**A plan is approved only if `achieves_objective=True` AND `simplifiable=False` AND `better_than_past_plans=True`.**

The Planner is allowed exactly one retry. So your `diagnosis` matters: if you reject, write feedback that's concrete enough for the Planner to actually fix the plan, not vague hedging. "Drop step s3 — its output is identical to s2's" is useful. "Could be better" is not.

If approved, your diagnosis is a one-sentence note on why — it gets logged for observability.

You MUST call the `evaluate_plan` tool exactly once. Do not respond with prose."""


_HIGH_SCORE_THRESHOLD = 80


class PlanPreEvaluatorAgent:
    AGENT_KIND = "pre_evaluator"
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
                        "agent_role": _AGENT_ROLE,
                        "tool": _EVALUATE_PLAN_TOOL,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode is fine
            log.warning(
                "agents.pre_evaluator.prompt_version_seed_failed",
                exc_info=True,
            )

    async def evaluate(
        self,
        *,
        ctx: ToolContext,
        content: str,
        plan: Plan,
        past_plans: list[procedural.StoredPlan] | None = None,
    ) -> PreEvalVerdict:
        """Vet a freshly-generated plan. Returns an *approving*
        verdict on any failure to call the model — see the module
        docstring for the failure-posture rationale."""
        await self._ensure_prompt_version()
        settings = get_settings()

        try:
            prompt = _build_eval_prompt(content, plan, past_plans or [])
            system = await build_for_agent(
                user_id=ctx.user_id, agent_role=_AGENT_ROLE,
            )
            result = await self.model_client.call(
                user_id=ctx.user_id,
                agent=self.AGENT_KIND,
                model=settings.model_post_evaluator,  # Haiku
                messages=[{"role": "user", "content": prompt}],
                system=system,
                prompt_version_id=self._prompt_version_id,
                task_id=ctx.task_id,
                tools=[_EVALUATE_PLAN_TOOL],
                tool_choice={"type": "tool", "name": "evaluate_plan"},
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "agents.pre_evaluator.model_call_failed",
                user_id=str(ctx.user_id), exc_info=True,
            )
            return _approve_by_default("(pre-evaluator model call failed)")

        for block in getattr(result.raw, "content", []) or []:
            if (
                _block_type(block) == "tool_use"
                and _block_name(block) == "evaluate_plan"
            ):
                payload = _block_input(block) or {}
                achieves = bool(payload.get("achieves_objective", True))
                simplifiable = bool(payload.get("simplifiable", False))
                better = bool(payload.get("better_than_past_plans", True))
                approved = achieves and (not simplifiable) and better
                return PreEvalVerdict(
                    approved=approved,
                    achieves_objective=achieves,
                    simplifiable=simplifiable,
                    better_than_past_plans=better,
                    diagnosis=str(payload.get("diagnosis", "")),
                )

        log.warning(
            "agents.pre_evaluator.no_tool_use_block",
            user_id=str(ctx.user_id),
        )
        return _approve_by_default("(pre-evaluator returned no tool_use)")


# --- plan-and-pre-evaluate helper ------------------------------------------


async def plan_with_pre_evaluation(
    *,
    planner: PlannerAgent,
    pre_evaluator: PlanPreEvaluatorAgent,
    ctx: ToolContext,
    thread_id: UUID | None,
    content: str,
    complexity_hint: str,
    emit: EmitFn | None = None,
) -> tuple[Plan, PreEvalVerdict, bool]:
    """Run the Planner, vet via the Pre-Evaluator, retry once on
    rejection. Returns ``(final_plan, verdict, retried)`` — ``retried``
    is True when the helper made a second-pass Planner call.

    The retry is deliberately capped at one. The spec wants the second
    plan shipped to the Executor unconditionally, even if it would
    also fail re-evaluation: more retries trade money for marginal
    quality, and a misbehaving Planner could otherwise loop on a
    Pre-Evaluator that's stuck rejecting.

    Both rounds emit a ``pre_eval`` SSE event with the verdict so the
    UI can surface what happened."""
    plan, plan_ctx = await planner.plan(
        ctx=ctx, thread_id=thread_id,
        content=content, complexity_hint=complexity_hint,
    )

    verdict = await pre_evaluator.evaluate(
        ctx=ctx, content=content, plan=plan,
        past_plans=plan_ctx.past_plans,
    )
    await _maybe_emit(emit, "pre_eval", _summarize_verdict(verdict, attempt=1))

    if verdict.approved:
        return plan, verdict, False

    log.info(
        "agents.pre_evaluator.retry",
        user_id=str(ctx.user_id),
        diagnosis=verdict.diagnosis,
    )

    second_plan, _second_ctx = await planner.plan(
        ctx=ctx, thread_id=thread_id,
        content=content, complexity_hint=complexity_hint,
        revision_diagnosis=verdict.diagnosis,
    )
    # Don't re-evaluate the second pass — ship whatever the Planner
    # produced. Emit a follow-up pre_eval event so the SSE consumer
    # sees the retry happened.
    await _maybe_emit(
        emit, "pre_eval",
        f"retry (attempt 2): shipping Planner's revised draft unchecked",
    )
    return second_plan, verdict, True


# --- helpers ---------------------------------------------------------------


def _approve_by_default(reason: str) -> PreEvalVerdict:
    """Failure posture: when the Pre-Evaluator itself can't produce a
    structured verdict, approve the Planner's plan so the user still
    gets an answer."""
    return PreEvalVerdict(
        approved=True,
        achieves_objective=True,
        simplifiable=False,
        better_than_past_plans=True,
        diagnosis=reason,
    )


def _summarize_verdict(verdict: PreEvalVerdict, *, attempt: int) -> str:
    if verdict.approved:
        return f"approved (attempt {attempt}): {verdict.diagnosis}"
    failed_checks = []
    if not verdict.achieves_objective:
        failed_checks.append("doesn't-achieve-objective")
    if verdict.simplifiable:
        failed_checks.append("simplifiable")
    if not verdict.better_than_past_plans:
        failed_checks.append("worse-than-past-plan")
    return (
        f"rejected (attempt {attempt}): {','.join(failed_checks) or '?'}"
        f" — {verdict.diagnosis}"
    )


def _build_eval_prompt(
    content: str, plan: Plan, past_plans: list[procedural.StoredPlan],
) -> str:
    lines: list[str] = [
        "# User request", content, "",
        "# Plan to evaluate",
        f"Summary: {plan.summary}",
        f"is_task: {plan.is_task}",
        f"model_used: {plan.model_used}",
        "",
        "## Steps",
    ]
    for s in plan.steps:
        tool_part = f" — `{s.tool}`" if s.tool else ""
        para_part = (
            f" (parallel group {s.parallel_group})"
            if s.parallel_group is not None else ""
        )
        lines.append(
            f"- {s.id} [{s.kind}]{tool_part}: {s.description}{para_part}"
        )
        if s.inputs:
            lines.append(f"  inputs: {json.dumps(s.inputs, default=str)}")

    # Surface the relevant past plans the Planner could have adapted —
    # the better_than_past_plans check needs them.
    relevant_past = [
        p for p in past_plans
        if p.score is not None and p.score >= _HIGH_SCORE_THRESHOLD
    ]
    lines.append("")
    if relevant_past:
        lines.append("# Past plans for context (score ≥ 80)")
        for p in relevant_past:
            lines.append(
                f"- past plan {p.id}: score={p.score}, similarity={p.similarity:.2f}"
                f"\n  query: {p.query}"
                f"\n  steps: {json.dumps(p.steps, default=str)}"
            )
    else:
        lines.append(
            "# Past plans for context"
            "\n(none with score ≥ 80 — pass `better_than_past_plans=True`)"
        )

    lines.extend([
        "",
        "Call `evaluate_plan` now.",
    ])
    return "\n".join(lines)


async def _maybe_emit(emit: EmitFn | None, event: str, data: str) -> None:
    if emit is None:
        return
    result = emit(event, data)
    if hasattr(result, "__await__"):
        await result


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


_agent: PlanPreEvaluatorAgent | None = None


def get_pre_evaluator_agent() -> PlanPreEvaluatorAgent:
    global _agent
    if _agent is None:
        _agent = PlanPreEvaluatorAgent()
    return _agent


def reset_pre_evaluator_agent() -> None:
    global _agent
    _agent = None
