"""Executor — runs a `Plan` from the Planner.

Walks steps in order, batching contiguous steps that share a
`parallel_group` ID into `asyncio.gather`. Three step kinds:

    - functional : invoke a registered tool with the step's `inputs`
    - reasoning  : Sonnet call prompted with the plan + prior step results
    - evaluation : v1 — treated like a reasoning step. Structured
                   continue/retry/branch verdicts are v2.

On step failure, marks remaining steps SKIPPED and returns an error
summary as the `final_answer`. No retries in v1.

Final-answer synthesis: if the last completed step is a `reasoning`
step, its output IS the final answer (the planner already synthesized).
Otherwise the Executor does one extra Sonnet call to synthesize. Saves
a model call on plans that end in reasoning.

Sandbox lifecycle: closes the task's sandbox in `finally`. Safe to call
even when no sandbox was created (SandboxManager pops by key — no-op if
the sandbox was never spun up).

Persistence: writes `final_answer`, `success`, and `error` back to the
`plans` row via `procedural.update_outcome`. The Post-Evaluator (step
14) follows up with `score` later.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory import procedural, tasks as tasks_dao
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.sandbox import get_manager as get_sandbox_manager
from wolfpaw.schemas import (
    ExecutionPlan,
    Plan,
    Step,
    StepResult,
    StepStatus,
)
from wolfpaw.toolbox.registry import Registry, ToolContext, get_registry
from wolfpaw.tracing import get_logger

# Max ancestor depth for `subagent` steps (step 16). The Executor checks
# `tasks_dao.get_depth` before spawning a child; spawning is rejected when
# the parent's depth is already at MAX_SUBAGENT_DEPTH (child would be 1
# deeper). Keeps recursive plans bounded.
MAX_SUBAGENT_DEPTH = 3

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]

_SYSTEM_PROMPT = """You are Wolfpaw's Executor. You work through one step of a multi-step plan at a time.

For each step you'll see the user's original request, the full plan, results from prior steps, and your current step's description. Produce concrete output for the current step — brief, accurate, focused on this step's task.

If your step is a synthesis step (typically the last reasoning step), write the final user-facing answer in plain markdown. Otherwise produce intermediate output that subsequent steps may use as input."""

_SYNTHESIS_SYSTEM_PROMPT = """You are Wolfpaw's Executor synthesizing the final user-facing answer from a plan execution. Write in plain markdown, be direct, lead with the answer. Cite step results where the user benefits from seeing them; skip the implementation details otherwise."""

# Cap large tool outputs so a single chatty tool result doesn't blow past
# Sonnet's context window when prior-results are inlined into every
# subsequent step prompt.
_PRIOR_RESULT_TRUNCATE = 2_000
_SYNTHESIS_TRUNCATE = 3_000

# Bound how long execution can run, regardless of the planner's step count
# or the model's tool loop. A misbehaving plan should fail loud, not eat
# budget. Per-step `run_python` already has its own sandbox cap.
_MAX_STEPS = 50


class ExecutorAgent:
    AGENT_KIND = "executor"
    VERSION_LABEL = "v1"

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        registry: Registry | None = None,
    ) -> None:
        self._model_client = model_client
        self._registry = registry
        self._prompt_version_id: UUID | None = None
        self._prompt_seeded = False

    @property
    def model_client(self) -> ModelClient:
        return self._model_client or get_model_client()

    @property
    def registry(self) -> Registry:
        return self._registry or get_registry()

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
                        "synthesis_system": _SYNTHESIS_SYSTEM_PROMPT,
                    },
                )
                self._prompt_version_id = row.id
        except Exception:  # noqa: BLE001 — degraded mode is fine
            log.warning("agents.executor.prompt_version_seed_failed", exc_info=True)

    async def execute(
        self,
        *,
        ctx: ToolContext,
        plan: Plan,
        emit: EmitFn | None = None,
    ) -> ExecutionPlan:
        await self._ensure_prompt_version()

        steps = list(plan.steps)
        if len(steps) > _MAX_STEPS:
            return ExecutionPlan(
                plan=plan, results=[],
                final_answer=(
                    f"This plan has {len(steps)} steps which exceeds the"
                    f" per-execution cap of {_MAX_STEPS}. Try a tighter plan."
                ),
                success=False,
                error="step_cap_exceeded",
            )

        results: list[StepResult] = []
        try:
            i = 0
            while i < len(steps):
                cur = steps[i]
                if cur.parallel_group is None:
                    batch_results = [
                        await self._run_step(
                            ctx=ctx, step=cur, plan=plan,
                            prior=results, emit=emit,
                        )
                    ]
                    i += 1
                else:
                    batch: list[Step] = [cur]
                    j = i + 1
                    while j < len(steps) and steps[j].parallel_group == cur.parallel_group:
                        batch.append(steps[j])
                        j += 1
                    batch_results = list(
                        await asyncio.gather(
                            *(
                                self._run_step(
                                    ctx=ctx, step=s, plan=plan,
                                    prior=results, emit=emit,
                                )
                                for s in batch
                            )
                        )
                    )
                    i = j

                results.extend(batch_results)
                if any(r.status == StepStatus.FAILED for r in batch_results):
                    for skipped in steps[i:]:
                        results.append(
                            StepResult(
                                step_id=skipped.id,
                                kind=skipped.kind,
                                status=StepStatus.SKIPPED,
                            )
                        )
                    break

            success = all(r.status == StepStatus.COMPLETED for r in results) and bool(results)
            error_msg: str | None = None
            if not success:
                failed = next(
                    (r for r in results if r.status == StepStatus.FAILED), None,
                )
                error_msg = failed.error if failed else "no steps completed"

            if success:
                final_answer = await self._synthesize_answer(
                    ctx=ctx, plan=plan, results=results,
                )
            else:
                final_answer = _format_failure_summary(results)

            execution = ExecutionPlan(
                plan=plan, results=results,
                final_answer=final_answer, success=success, error=error_msg,
            )
        finally:
            try:
                await get_sandbox_manager().close_for_task(ctx.user_id, ctx.task_id)
            except Exception:  # noqa: BLE001
                log.warning("agents.executor.sandbox_close_failed", exc_info=True)

        if plan.id is not None:
            try:
                async with acquire() as conn:
                    await procedural.update_outcome(
                        conn,
                        plan_id=plan.id,
                        final_answer=execution.final_answer,
                        success=execution.success,
                        error=execution.error,
                    )
            except Exception:  # noqa: BLE001
                log.warning("agents.executor.persist_failed", exc_info=True)

        return execution

    # --- step runners ------------------------------------------------------

    async def _run_step(
        self,
        *,
        ctx: ToolContext,
        step: Step,
        plan: Plan,
        prior: list[StepResult],
        emit: EmitFn | None,
    ) -> StepResult:
        # Snapshot prior at dispatch so concurrently-running siblings in a
        # parallel group see the same context (and not each other's results).
        snapshot = list(prior)
        await _maybe_emit(
            emit, "step.start",
            f"{step.id} [{step.kind}] {step.description}",
        )
        start = time.monotonic()
        try:
            if step.kind == "functional":
                output: Any = await self._run_functional(ctx, step)
            elif step.kind == "reasoning":
                output = await self._run_reasoning(ctx, step, plan, snapshot)
            elif step.kind == "evaluation":
                output = await self._run_evaluation(ctx, step, plan, snapshot)
            elif step.kind == "subagent":
                output = await self._run_subagent(ctx, step)
            else:
                raise ValueError(f"unknown step kind: {step.kind!r}")
        except Exception as e:  # noqa: BLE001 — any failure becomes a step failure
            elapsed = time.monotonic() - start
            log.exception(
                "agents.executor.step_failed",
                step_id=step.id, kind=step.kind, error=str(e),
            )
            await _maybe_emit(emit, "step.error", f"{step.id} failed: {e}")
            return StepResult(
                step_id=step.id, kind=step.kind,
                status=StepStatus.FAILED, error=str(e),
                elapsed_seconds=elapsed,
            )

        elapsed = time.monotonic() - start
        await _maybe_emit(
            emit, "step.end",
            f"{step.id} completed in {elapsed:.2f}s",
        )
        return StepResult(
            step_id=step.id, kind=step.kind,
            status=StepStatus.COMPLETED, output=output,
            elapsed_seconds=elapsed,
        )

    async def _run_functional(self, ctx: ToolContext, step: Step) -> Any:
        if not step.tool:
            raise ValueError(f"functional step {step.id!r} has no `tool`")
        try:
            tool = self.registry.get(step.tool)
        except KeyError as e:
            raise ValueError(f"unknown tool: {step.tool!r}") from e
        return await tool.run(ctx, **step.inputs)

    async def _run_reasoning(
        self, ctx: ToolContext, step: Step, plan: Plan, prior: list[StepResult],
    ) -> str:
        settings = get_settings()
        prompt = _build_step_prompt(step, plan, prior)
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_executor,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM_PROMPT,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
        )
        return result.text or ""

    async def _run_evaluation(
        self, ctx: ToolContext, step: Step, plan: Plan, prior: list[StepResult],
    ) -> str:
        # v1: same shape as reasoning. v2 adds a forced verdict tool_use
        # that drives continue/retry/branch logic.
        return await self._run_reasoning(ctx, step, plan, prior)

    async def _run_subagent(
        self, ctx: ToolContext, step: Step,
    ) -> dict[str, Any]:
        """Spawn a child Task that runs its own Planner→Executor→Post-Eval
        on `step.inputs.query`. Captures the child's final_answer as the
        step's output so subsequent reasoning steps can synthesize across
        multiple subagent outputs.

        Depth check: `MAX_SUBAGENT_DEPTH` ancestors. A subagent at the
        max depth itself can't spawn further subagents.

        Requires `ctx.task_id` — subagent steps only make sense inside a
        Task hierarchy. The plan-path Router would have created a task
        for any plan with `subagent` steps in it.
        """
        if ctx.task_id is None:
            raise ValueError(
                "subagent steps require a parent task — Triage should"
                " have routed this through the task path"
            )

        inputs = step.inputs or {}
        query = inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"subagent step {step.id!r} missing required `inputs.query`"
            )
        title = inputs.get("title") or step.description[:80] or step.id
        budget = inputs.get("budget_cents")
        child_complexity = inputs.get("complexity_hint") or "moderate"

        async with acquire() as conn:
            depth = await tasks_dao.get_depth(conn, task_id=ctx.task_id)
        if depth >= MAX_SUBAGENT_DEPTH:
            raise ValueError(
                f"subagent depth cap reached (parent depth={depth},"
                f" max={MAX_SUBAGENT_DEPTH}) — flatten the plan"
            )

        # Lazy import: `tasks.service` imports back into the agents
        # package, so a top-level import here would create a cycle.
        from wolfpaw.tasks.service import get_task_service

        service = get_task_service()
        outcome = await service.create_and_run(
            user_id=ctx.user_id,
            thread_id=None,                 # subagent gets fresh context
            content=query,
            title=title,
            description=(
                f"Subagent spawned by parent task {ctx.task_id} for"
                f" step {step.id!r}: {step.description}"
            ),
            parent_task_id=ctx.task_id,
            budget_cents=budget,
            complexity_hint=child_complexity,
            # Don't propagate emit — multiple parallel subagents would
            # interleave step events into the parent's SSE stream and
            # make the timeline confusing. The parent's step.start /
            # step.end events show that the subagent ran.
            emit=None,
        )

        if outcome.task.status != "completed":
            raise ValueError(
                f"subagent task {outcome.task.id} ended in"
                f" {outcome.task.status}: {outcome.final_answer}"
            )
        return {
            "subagent_task_id": str(outcome.task.id),
            "answer": outcome.final_answer,
            "score": (outcome.verdict.score if outcome.verdict else None),
        }

    # --- synthesis ---------------------------------------------------------

    async def _synthesize_answer(
        self, *, ctx: ToolContext, plan: Plan, results: list[StepResult],
    ) -> str:
        for r in reversed(results):
            if r.status == StepStatus.COMPLETED and r.kind == "reasoning":
                # Planner ended with a synthesis step — use it directly,
                # no extra model call.
                return str(r.output or "")
            if r.status == StepStatus.COMPLETED:
                break  # last completed was non-reasoning; do the extra call
        settings = get_settings()
        prompt = _build_synthesis_prompt(plan, results)
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_executor,
            messages=[{"role": "user", "content": prompt}],
            system=_SYNTHESIS_SYSTEM_PROMPT,
            prompt_version_id=self._prompt_version_id,
            task_id=ctx.task_id,
        )
        return result.text or "(no answer produced)"


# --- helpers ---------------------------------------------------------------


def _build_step_prompt(step: Step, plan: Plan, prior: list[StepResult]) -> str:
    lines = [
        "# User request",
        plan.query,
        "",
        "# Plan summary",
        plan.summary,
        "",
        "# Steps so far",
    ]
    if not prior:
        lines.append("(no prior steps)")
    else:
        for r in prior:
            lines.append(_format_prior_result(r, cap=_PRIOR_RESULT_TRUNCATE))
    lines.extend([
        "",
        "# Your current step",
        f"{step.id} [{step.kind}]: {step.description}",
        "",
        "Produce the output for this step now.",
    ])
    return "\n".join(lines)


def _build_synthesis_prompt(plan: Plan, results: list[StepResult]) -> str:
    lines = [
        "# User request",
        plan.query,
        "",
        "# Plan summary",
        plan.summary,
        "",
        "# Step results",
    ]
    for r in results:
        lines.append(_format_prior_result(r, cap=_SYNTHESIS_TRUNCATE))
    lines.extend([
        "",
        "Write the final user-facing answer in plain markdown.",
    ])
    return "\n".join(lines)


def _format_prior_result(r: StepResult, *, cap: int) -> str:
    if r.status != StepStatus.COMPLETED:
        return f"- {r.step_id} [{r.kind}]: ({r.status.value})"
    out = json.dumps(r.output, default=str)
    if len(out) > cap:
        out = out[:cap] + " …(truncated)"
    return f"- {r.step_id} [{r.kind}]: {out}"


def _format_failure_summary(results: list[StepResult]) -> str:
    completed = [r for r in results if r.status == StepStatus.COMPLETED]
    failed = next((r for r in results if r.status == StepStatus.FAILED), None)
    if failed is None:
        return "Execution failed before any step completed."
    return (
        f"I ran into a problem on step **{failed.step_id}** ({failed.kind}):"
        f" {failed.error}\n\n"
        f"Completed {len(completed)} step(s) before this. Let me know if"
        f" you'd like me to try a different approach."
    )


async def _maybe_emit(emit: EmitFn | None, event: str, data: str) -> None:
    if emit is None:
        return
    result = emit(event, data)
    if hasattr(result, "__await__"):
        await result


_agent: ExecutorAgent | None = None


def get_executor_agent() -> ExecutorAgent:
    global _agent
    if _agent is None:
        _agent = ExecutorAgent()
    return _agent


def reset_executor_agent() -> None:
    global _agent
    _agent = None
