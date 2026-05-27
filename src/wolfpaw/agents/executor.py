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
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory import procedural, tasks as tasks_dao
from wolfpaw.memory.db import acquire
from wolfpaw.metering.model_client import ModelClient, get_model_client
from wolfpaw.metering.prompt_versions import bump_prompt_version
from wolfpaw.persona.builder import build_for_agent
from wolfpaw.sandbox import get_manager as get_sandbox_manager
from wolfpaw.schemas import (
    ExecutionPlan,
    Plan,
    ReplanContext,
    Step,
    StepResult,
    StepStatus,
)
from wolfpaw.toolbox.registry import (
    Registry,
    Tool,
    ToolContext,
    ToolError,
    get_registry,
)
from wolfpaw.tracing import get_logger
from wolfpaw.workspace.files import WorkspaceCollision

if TYPE_CHECKING:
    # Lazy reference: the Executor calls Planner.plan() on replan, but
    # Planner doesn't import Executor — no real cycle, just keeping the
    # import out of module-load order so test fakes can stub the Planner
    # via the `planner=` constructor argument.
    from wolfpaw.agents.planner import PlannerAgent

# Affirmative answers to the "overwrite?" prompt. Anything not in here is
# treated as a decline (which is the safer default — the executor will
# surface the collision rather than clobber data).
_OVERWRITE_YES = frozenset({"y", "yes", "overwrite", "ok", "confirm"})

# Max ancestor depth for `subagent` steps (step 16). The Executor checks
# `tasks_dao.get_depth` before spawning a child; spawning is rejected when
# the parent's depth is already at MAX_SUBAGENT_DEPTH (child would be 1
# deeper). Keeps recursive plans bounded.
MAX_SUBAGENT_DEPTH = 3

# Cap concurrent subagent spawns scoped to the root task. Subagents at any
# depth under the same root contend for one semaphore so a wide-fanout plan
# can't overwhelm the model provider or sandbox capacity. Per-root keeps
# unrelated user tasks independent.
MAX_CONCURRENT_SUBAGENTS_PER_ROOT = 5

# Step-level recovery (1): if a functional step's tool raises ToolError,
# call a cheap model to repair the inputs once and re-run. _MAX_TOOL_ATTEMPTS
# counts the initial call + every retry, so 2 = "one repair attempt."
_MAX_TOOL_ATTEMPTS = 2

# Mid-plan replan (3): if a step still fails after step-level retry, ask
# the Planner for a continuation. Capped at 1 per execution so a hostile
# query can't pin the system in a replan loop.
_MAX_REPLANS = 1

# Process-local registry of root-task → semaphore. Refcounted so an entry
# is dropped once its last in-flight subagent releases — keeps the dict
# small for long-running processes with many root tasks over time.
_subagent_semaphores: dict[UUID, asyncio.Semaphore] = {}
_subagent_semaphore_refs: dict[UUID, int] = {}
_subagent_registry_lock = asyncio.Lock()


@asynccontextmanager
async def _acquire_subagent_slot(root_task_id: UUID) -> AsyncIterator[None]:
    """Acquire one slot from the per-root subagent semaphore. Blocks when
    `MAX_CONCURRENT_SUBAGENTS_PER_ROOT` siblings are already running."""
    async with _subagent_registry_lock:
        sem = _subagent_semaphores.get(root_task_id)
        if sem is None:
            sem = asyncio.Semaphore(MAX_CONCURRENT_SUBAGENTS_PER_ROOT)
            _subagent_semaphores[root_task_id] = sem
        _subagent_semaphore_refs[root_task_id] = (
            _subagent_semaphore_refs.get(root_task_id, 0) + 1
        )
    try:
        async with sem:
            yield
    finally:
        async with _subagent_registry_lock:
            remaining = _subagent_semaphore_refs[root_task_id] - 1
            if remaining <= 0:
                _subagent_semaphores.pop(root_task_id, None)
                _subagent_semaphore_refs.pop(root_task_id, None)
            else:
                _subagent_semaphore_refs[root_task_id] = remaining

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]

_AGENT_ROLE = """You are the **Executor**. You work through one step of a multi-step plan at a time.

For each step you'll see the user's original request, the full plan, results from prior steps, and your current step's description. Produce concrete output for the current step — brief, accurate, focused on this step's task.

If your step is a synthesis step (typically the last reasoning step), write the final user-facing answer in plain markdown. Otherwise produce intermediate output that subsequent steps may use as input."""

_SYNTHESIS_AGENT_ROLE = """You are the **Executor** synthesizing the final user-facing answer from a plan execution. Write in plain markdown, be direct, lead with the answer. Cite step results where the user benefits from seeing them; skip the implementation details otherwise."""

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
        planner: "PlannerAgent | None" = None,
    ) -> None:
        self._model_client = model_client
        self._registry = registry
        self._planner = planner
        self._prompt_version_id: UUID | None = None
        self._prompt_seeded = False

    @property
    def model_client(self) -> ModelClient:
        return self._model_client or get_model_client()

    @property
    def registry(self) -> Registry:
        return self._registry or get_registry()

    @property
    def planner(self) -> "PlannerAgent":
        if self._planner is not None:
            return self._planner
        from wolfpaw.agents.planner import get_planner_agent

        return get_planner_agent()

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
                        "synthesis_agent_role": _SYNTHESIS_AGENT_ROLE,
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
        # Replan budget lives in the function scope — explicit local
        # variable, not stateful on the agent. Capped at _MAX_REPLANS
        # so a query that keeps tripping the same failure can't pin the
        # system in a planner loop.
        replan_budget = _MAX_REPLANS
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
                    failed_result = next(
                        r for r in batch_results
                        if r.status == StepStatus.FAILED
                    )
                    # Try to recover by asking the Planner for a
                    # continuation. If that returns a plan, splice its
                    # steps in at i (which already points past the
                    # failed batch) and continue. Otherwise mark the
                    # remainder skipped and stop, matching the prior
                    # v1 behavior.
                    if replan_budget > 0:
                        continuation = await self._replan_after_failure(
                            ctx=ctx, original_plan=plan,
                            completed=results, failed_result=failed_result,
                            emit=emit,
                        )
                        if continuation is not None:
                            replan_budget -= 1
                            steps = steps[:i] + list(continuation.steps)
                            continue
                    for skipped in steps[i:]:
                        results.append(
                            StepResult(
                                step_id=skipped.id,
                                kind=skipped.kind,
                                status=StepStatus.SKIPPED,
                            )
                        )
                    break

            # Success allows for replan recovery: a FAILED step buried
            # mid-stream is fine if the continuation finished the work.
            # The rule is "nothing was abandoned (no SKIPPED) and the
            # final step ran to completion." A failure that was never
            # recovered ends with SKIPPED entries, which fails this
            # check. An execution with no results (e.g. step cap hit
            # earlier) also fails.
            has_results = bool(results)
            no_skipped = not any(
                r.status == StepStatus.SKIPPED for r in results
            )
            last_completed = (
                has_results and results[-1].status == StepStatus.COMPLETED
            )
            success = has_results and no_skipped and last_completed
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
                output: Any = await self._run_functional(ctx, step, emit=emit)
            elif step.kind == "reasoning":
                output = await self._run_reasoning(ctx, step, plan, snapshot)
            elif step.kind == "evaluation":
                output = await self._run_evaluation(ctx, step, plan, snapshot)
            elif step.kind == "subagent":
                output = await self._run_subagent(ctx, step)
            elif step.kind == "tool_creator":
                output = await self._run_tool_creator(ctx, step, plan)
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

    async def _run_functional(
        self, ctx: ToolContext, step: Step, *, emit: EmitFn | None,
    ) -> Any:
        if not step.tool:
            raise ValueError(f"functional step {step.id!r} has no `tool`")
        tool = await self._resolve_tool(ctx, step.tool)
        return await self._call_tool_with_recovery(
            ctx=ctx, step=step, tool=tool,
            inputs=step.inputs, emit=emit, attempt=1,
        )

    async def _call_tool_with_recovery(
        self,
        *,
        ctx: ToolContext,
        step: Step,
        tool: Tool,
        inputs: dict[str, Any],
        emit: EmitFn | None,
        attempt: int,
    ) -> Any:
        """Run the tool. On ToolError, ask a model to repair the inputs
        once and recurse. The `attempt` counter bounds the recursion via
        ``_MAX_TOOL_ATTEMPTS`` — no hidden state, no flag."""
        try:
            return await tool.run(ctx, **inputs)
        except WorkspaceCollision as collision:
            return await self._handle_workspace_collision(
                ctx=ctx, step=step, tool=tool, collision=collision,
            )
        except ToolError as e:
            if attempt >= _MAX_TOOL_ATTEMPTS:
                raise
            corrected = await self._propose_corrected_inputs(
                ctx=ctx, step=step, tool=tool,
                original_inputs=inputs, error=e,
            )
            if corrected is None:
                raise
            await _maybe_emit(
                emit, "step.recover",
                f"{step.id} retrying with corrected inputs",
            )
            return await self._call_tool_with_recovery(
                ctx=ctx, step=step, tool=tool, inputs=corrected,
                emit=emit, attempt=attempt + 1,
            )

    async def _propose_corrected_inputs(
        self,
        *,
        ctx: ToolContext,
        step: Step,
        tool: Tool,
        original_inputs: dict[str, Any],
        error: ToolError,
    ) -> dict[str, Any] | None:
        """One Haiku call with a forced ``propose_inputs`` tool_use that
        returns a corrected inputs dict (or None if the model declines).
        Stateless — no conversational context, no thread linkage."""
        settings = get_settings()
        system_prompt = (
            "You repair tool inputs after a recoverable failure. You"
            " will be given: a step description, the tool's name +"
            " input_schema, the inputs that just failed, and the"
            " error message. Propose corrected inputs that address"
            " the error. Do not change the tool. Do not change"
            " unrelated fields. If the error names the actual valid"
            " columns / files / options, use that hint directly."
            " If the inputs can't be fixed without more information,"
            " set ``give_up: true`` and explain briefly."
        )
        user_payload = {
            "step_description": step.description,
            "tool": tool.name,
            "tool_input_schema": tool.input_schema,
            "failed_inputs": original_inputs,
            "error_message": str(error),
        }
        propose_tool = {
            "name": "propose_inputs",
            "description": (
                "Return corrected inputs for the failed tool call, or"
                " set give_up=true if you can't fix them."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "corrected_inputs": {
                        "type": "object",
                        "description": (
                            "A new inputs dict to pass to the same tool."
                            " Required when give_up is false."
                        ),
                    },
                    "give_up": {"type": "boolean", "default": False},
                    "reasoning": {"type": "string"},
                },
                "required": ["reasoning"],
            },
        }
        try:
            result = await self.model_client.call(
                user_id=ctx.user_id,
                agent="executor_recover",
                model=settings.model_triage,
                messages=[{
                    "role": "user",
                    "content": json.dumps(user_payload, default=str),
                }],
                system=system_prompt,
                prompt_version_id=self._prompt_version_id,
                task_id=ctx.task_id,
                tools=[propose_tool],
                tool_choice={"type": "tool", "name": "propose_inputs"},
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "agents.executor.recover.call_failed",
                step_id=step.id, exc_info=True,
            )
            return None

        for block in getattr(result.raw, "content", []) or []:
            if (
                _block_type(block) == "tool_use"
                and _block_name(block) == "propose_inputs"
            ):
                payload = _block_input(block) or {}
                if payload.get("give_up"):
                    log.info(
                        "agents.executor.recover.give_up",
                        step_id=step.id,
                        reasoning=payload.get("reasoning"),
                    )
                    return None
                corrected = payload.get("corrected_inputs")
                if not isinstance(corrected, dict) or not corrected:
                    return None
                log.info(
                    "agents.executor.recover.proposed",
                    step_id=step.id,
                    reasoning=payload.get("reasoning"),
                )
                return corrected

        return None

    async def _replan_after_failure(
        self,
        *,
        ctx: ToolContext,
        original_plan: Plan,
        completed: list[StepResult],
        failed_result: StepResult,
        emit: EmitFn | None,
    ) -> Plan | None:
        """Build a ReplanContext from the failure and ask the Planner for
        a continuation. Returns the new plan or None on planner error.

        Uses ``thread_id=None`` so the Planner doesn't reload chat
        history — the replan block already carries focused context
        (original query + completed steps + failure). Past-plan and
        skill retrieval still fires because the embedding still runs."""
        failed_step = next(
            (s for s in original_plan.steps if s.id == failed_result.step_id),
            None,
        )
        if failed_step is None:
            log.warning(
                "agents.executor.replan.missing_failed_step",
                step_id=failed_result.step_id,
            )
            return None

        replan_ctx = ReplanContext(
            original_query=original_plan.query,
            completed_results=list(completed),
            failed_step=failed_step,
            failed_step_error=failed_result.error or "(no error message)",
        )
        try:
            new_plan, _ = await self.planner.plan(
                ctx=ctx,
                thread_id=None,
                content=original_plan.query,
                replan_from=replan_ctx,
            )
        except Exception:  # noqa: BLE001 — replan is best-effort
            log.warning(
                "agents.executor.replan.failed",
                failed_step_id=failed_step.id, exc_info=True,
            )
            return None

        await _maybe_emit(
            emit, "plan.replan",
            f"continuation: {len(new_plan.steps)} step(s) after"
            f" {failed_step.id} failed",
        )
        log.info(
            "agents.executor.replan.success",
            failed_step_id=failed_step.id,
            continuation_step_count=len(new_plan.steps),
        )
        return new_plan

    async def _resolve_tool(self, ctx: ToolContext, name: str) -> Any:
        """Resolve a tool name to a Tool instance, falling through to
        the per-user approved user-tools when no builtin matches.

        Builtins take precedence: if a user-tool happens to share a
        name with a builtin, the builtin wins (the Planner is told
        not to propose duplicates, but defense-in-depth here matters)."""
        try:
            return self.registry.get(name)
        except KeyError:
            pass
        # Fall through to user-tools.
        from wolfpaw.memory import tools as tools_dao
        from wolfpaw.toolbox.tools._dynamic_user_tool import DynamicUserTool

        async with acquire() as conn:
            row = await tools_dao.find_active_by_name(
                conn, user_id=ctx.user_id, name=name,
            )
        if row is None:
            raise ValueError(f"unknown tool: {name!r}")
        return DynamicUserTool(row)

    async def _handle_workspace_collision(
        self, *, ctx: ToolContext, step: Step, tool: Any,
        collision: WorkspaceCollision,
    ) -> Any:
        """Ask the user whether to overwrite, then retry the tool with
        `overwrite=True` if they say yes.

        Requires `ctx.task_id` — the `ask_user` tool only works inside a
        task lifecycle. Outside a task, the collision propagates as a
        normal step failure (same v1 behavior as before this hook).
        """
        if ctx.task_id is None:
            raise collision

        existing = collision.existing
        ask_tool = self.registry.get("ask_user")
        prompt = (
            f"File {existing.filename!r} already exists at v{existing.version}."
            " Overwrite it? (yes / no)"
        )
        answer_payload = await ask_tool.run(
            ctx,
            question=prompt,
            options=["yes", "no"],
            urgency="normal",
        )
        answer = str(answer_payload.get("answer", "")).strip().lower()
        if answer not in _OVERWRITE_YES:
            raise collision
        retry_inputs = dict(step.inputs)
        retry_inputs["overwrite"] = True
        return await tool.run(ctx, **retry_inputs)

    async def _run_reasoning(
        self, ctx: ToolContext, step: Step, plan: Plan, prior: list[StepResult],
    ) -> str:
        settings = get_settings()
        prompt = _build_step_prompt(step, plan, prior)
        system = await build_for_agent(
            user_id=ctx.user_id, agent_role=_AGENT_ROLE,
        )
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_executor,
            messages=[{"role": "user", "content": prompt}],
            system=system,
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

    async def _run_tool_creator(
        self, ctx: ToolContext, step: Step, plan: Plan,
    ) -> dict[str, Any]:
        """Invoke the Tool Creator: draft a spec, dedup, ask the user,
        persist. Returns the outcome dict so subsequent steps can read
        the new tool's name out of ``prior_results``.

        Requires ``ctx.task_id`` because ``ask_user`` lives inside the
        task lifecycle. The Planner only emits ``tool_creator`` steps
        inside the task path; we re-validate here defensively.
        """
        if ctx.task_id is None:
            raise ValueError(
                "tool_creator steps require a parent task — the Router"
                " should have created one for this plan"
            )
        inputs = step.inputs or {}
        intent = inputs.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError(
                f"tool_creator step {step.id!r} missing required `inputs.intent`"
            )
        required_inputs = inputs.get("required_inputs")
        if required_inputs is not None and not isinstance(required_inputs, list):
            required_inputs = None
        # Lazy import: tool_creator imports from agents.* which would
        # otherwise complete a cycle at module load.
        from wolfpaw.agents.tool_creator import get_tool_creator_agent

        agent = get_tool_creator_agent()
        outcome = await agent.create_tool(
            ctx=ctx,
            intent=intent,
            required_inputs=required_inputs,
            plan_id=plan.id,
        )
        return outcome.to_jsonb()

    async def _run_subagent(
        self, ctx: ToolContext, step: Step,
    ) -> dict[str, Any]:
        """Spawn a child Task that runs its own Planner→Executor→Post-Eval
        on `step.inputs.query`. Captures the child's final_answer as the
        step's output so subsequent reasoning steps can synthesize across
        multiple subagent outputs.

        Depth check: `MAX_SUBAGENT_DEPTH` ancestors. A subagent at the
        max depth itself can't spawn further subagents.

        Failure policy (step 27): `step.inputs.on_failure` selects how
        the executor handles a child task that didn't complete:
          - "fail" (default): propagate as a step failure
          - "drop": return a stub output marked ``dropped=True`` so
                    synthesis can carry on with the surviving branches
          - "retry": re-spawn once with the failure diagnosis appended
                     to the query; retry-failure propagates as "fail"

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
        on_failure = _normalize_failure_policy(inputs.get("on_failure"))

        async with acquire() as conn:
            depth = await tasks_dao.get_depth(conn, task_id=ctx.task_id)
            root_task_id = await tasks_dao.get_root(conn, task_id=ctx.task_id)
        if depth >= MAX_SUBAGENT_DEPTH:
            raise ValueError(
                f"subagent depth cap reached (parent depth={depth},"
                f" max={MAX_SUBAGENT_DEPTH}) — flatten the plan"
            )

        spawn_kwargs = dict(
            ctx=ctx, step=step, title=title, budget=budget,
            complexity=child_complexity, root_task_id=root_task_id,
        )
        try:
            return await self._spawn_subagent_task(query=query, **spawn_kwargs)
        except Exception as first_failure:  # noqa: BLE001
            log.warning(
                "agents.executor.subagent_failed",
                step_id=step.id,
                on_failure=on_failure,
                error=str(first_failure),
            )
            if on_failure == "drop":
                return _drop_stub(first_failure)
            if on_failure == "retry":
                augmented = _augment_query_with_failure(query, first_failure)
                try:
                    return await self._spawn_subagent_task(
                        query=augmented, **spawn_kwargs,
                    )
                except Exception as retry_failure:  # noqa: BLE001
                    log.warning(
                        "agents.executor.subagent_retry_failed",
                        step_id=step.id,
                        first_error=str(first_failure),
                        retry_error=str(retry_failure),
                    )
                    raise retry_failure from first_failure
            # fail (default) — re-raise so the outer `_run_step` marks
            # the step FAILED.
            raise

    async def _spawn_subagent_task(
        self,
        *,
        ctx: ToolContext,
        step: Step,
        query: str,
        title: str,
        budget: int | None,
        complexity: str,
        root_task_id: UUID,
    ) -> dict[str, Any]:
        """Single subagent spawn: acquire the per-root semaphore slot,
        run the child task end-to-end, validate the outcome. Used by
        both the initial attempt and (under ``on_failure="retry"``) the
        single retry attempt — depth + input validation already happened
        in the caller."""
        # Lazy import: `tasks.service` imports back into the agents
        # package, so a top-level import here would create a cycle.
        from wolfpaw.tasks.service import get_task_service

        service = get_task_service()
        # Per-root semaphore: bound fanout so a wide plan can't saturate
        # the model provider or sandbox pool. Siblings in a parallel_group
        # contend for the same slots as cousins under the same root.
        async with _acquire_subagent_slot(root_task_id):
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
                complexity_hint=complexity,
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
        system = await build_for_agent(
            user_id=ctx.user_id, agent_role=_SYNTHESIS_AGENT_ROLE,
        )
        result = await self.model_client.call(
            user_id=ctx.user_id,
            agent=self.AGENT_KIND,
            model=settings.model_executor,
            messages=[{"role": "user", "content": prompt}],
            system=system,
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


# --- subagent failure policy (step 27) -------------------------------------


_VALID_FAILURE_POLICIES = frozenset({"fail", "drop", "retry"})


def _normalize_failure_policy(raw: Any) -> str:
    """Coerce ``step.inputs.on_failure`` to one of the three valid
    policies. Missing → "fail" (preserves pre-step-27 behaviour).
    Anything else (typo, model hallucinated a policy) → also "fail"
    with a warning, so we never silently drop subagent failures."""
    if raw is None:
        return "fail"
    if isinstance(raw, str) and raw in _VALID_FAILURE_POLICIES:
        return raw
    log.warning(
        "agents.executor.unknown_failure_policy",
        on_failure=str(raw),
    )
    return "fail"


def _drop_stub(exc: Exception) -> dict[str, Any]:
    """Output payload returned when ``on_failure="drop"`` swallows a
    subagent failure. Marked ``dropped=True`` so the parent's
    synthesis step can distinguish "branch X dropped because Y" from
    a real subagent answer."""
    return {
        "subagent_task_id": None,
        "answer": None,
        "score": None,
        "dropped": True,
        "error": str(exc),
    }


def _augment_query_with_failure(original: str, exc: Exception) -> str:
    """Build the query the retry attempt receives. The previous failure
    goes at the bottom so a Planner that ignores the preamble still
    sees the original instruction; one that reads it gets a concrete
    "try something different" cue."""
    return (
        f"{original}\n\n"
        f"## Previous attempt failed\n"
        f"A prior attempt at this task failed with: {exc}\n"
        f"Try a different approach — don't repeat the failing strategy."
    )


# --- anthropic block helpers ----------------------------------------------
#
# Same shape as triage.py's helpers; duplicated here to avoid a
# top-level import dependency between agents.


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_name(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("name")
    return getattr(block, "name", None)


def _block_input(block: Any) -> dict | None:
    if isinstance(block, dict):
        return block.get("input")
    return getattr(block, "input", None)


_agent: ExecutorAgent | None = None


def get_executor_agent() -> ExecutorAgent:
    global _agent
    if _agent is None:
        _agent = ExecutorAgent()
    return _agent


def reset_executor_agent() -> None:
    global _agent
    _agent = None
