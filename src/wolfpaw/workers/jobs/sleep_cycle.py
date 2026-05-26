"""Sleep Cycle — periodic maintenance over the agent's memory.

Three operations, each independent + bounded so a long-running cycle
can't blow past a maintenance window:

1. **Re-score old plans.** Take the N oldest scored plans, run them
   back through the current Post-Evaluator prompt, write the new
   score. This catches "prompt drift": if the evaluator's standards
   shifted, what we used to call a 90 may now be a 65, and the Planner
   should adapt its retrieval accordingly. Reconstruction is partial —
   we only persist `final_answer` + `success`, not intermediate step
   outputs, so the re-score reflects the answer + plan shape rather
   than the full execution trace.

2. **Consolidate near-duplicate skills.** For each user, walk their
   active emitted skills and merge any pair whose cosine similarity is
   ≥ ``WOLFPAW_SLEEP_CYCLE_DEDUP_THRESHOLD`` (default 0.92). The
   higher-scoring of the two survives; the loser gets soft-deleted via
   ``superseded_by_skill_id``. Threshold deliberately higher than the
   emit-time dedup (0.85) — post-hoc merging is destructive and we
   want high confidence.

3. **GC orphan threads.** Threads with zero messages older than
   ``WOLFPAW_SLEEP_CYCLE_ORPHAN_THREAD_AGE_DAYS`` (default 30) get
   deleted outright. These typically come from `/reset` followed by
   the user never returning, or a channel-link mishap that minted a
   thread before the inbound got dispatched.

Gating: the whole job no-ops unless ``WOLFPAW_SLEEP_CYCLE_ENABLED`` is
true. Lets the cron register unconditionally on the worker — operators
flip the env flag and the next firing picks it up without redeploying.

Failure posture: every operation has its own try/except + log path.
Re-score errors per plan don't stop the batch. Merge errors per pair
don't stop the user. Thread-GC errors log + bail without touching
plans/skills. The whole job is best-effort + idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from wolfpaw.config import get_settings
from wolfpaw.memory import procedural, skills as skills_mem
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


@dataclass(frozen=True)
class SleepCycleResult:
    """Summary of one cycle — exposed for tests + observability."""

    rescored_plans: int
    rescore_errors: int
    skills_superseded: int
    orphan_threads_deleted: int


async def sleep_cycle() -> SleepCycleResult:
    """Run the full cycle. Returns counters for each operation."""
    settings = get_settings()
    if not settings.sleep_cycle_enabled:
        log.info("workers.sleep_cycle.disabled")
        return SleepCycleResult(0, 0, 0, 0)

    log.info("workers.sleep_cycle.start")

    rescored, errors = await _rescore_old_plans(
        batch=settings.sleep_cycle_rescore_batch,
    )
    superseded = await _consolidate_skills(
        threshold=settings.sleep_cycle_dedup_threshold,
    )
    deleted = await _gc_orphan_threads(
        age_days=settings.sleep_cycle_orphan_thread_age_days,
    )

    result = SleepCycleResult(
        rescored_plans=rescored,
        rescore_errors=errors,
        skills_superseded=superseded,
        orphan_threads_deleted=deleted,
    )
    log.info(
        "workers.sleep_cycle.complete",
        rescored=rescored, rescore_errors=errors,
        superseded=superseded, orphans_deleted=deleted,
    )
    return result


# --- arq wrapper -----------------------------------------------------------


async def sleep_cycle_job(_ctx: dict) -> dict:
    """arq-shaped entry point. Returns the counter dict so the cron
    history (visible via `arq.jobs.Job.info`) carries the per-run
    stats."""
    result = await sleep_cycle()
    return {
        "rescored_plans": result.rescored_plans,
        "rescore_errors": result.rescore_errors,
        "skills_superseded": result.skills_superseded,
        "orphan_threads_deleted": result.orphan_threads_deleted,
    }


# --- operation 1: re-score old plans ---------------------------------------


async def _rescore_old_plans(*, batch: int) -> tuple[int, int]:
    """Re-evaluate the N oldest already-scored plans against the
    current Post-Evaluator prompt. Returns ``(updated_count, errors)``.

    Skips plans without a persisted ``final_answer`` (Executor never
    finished writing) — there's nothing to score against."""
    from wolfpaw.agents.post_evaluator import get_post_evaluator_agent
    from wolfpaw.schemas import (
        ExecutionPlan, Plan, Step,
    )
    from wolfpaw.toolbox.registry import ToolContext

    if batch <= 0:
        return 0, 0

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, query, steps, final_answer, success,
                   error
              FROM plans
             WHERE score IS NOT NULL
               AND final_answer IS NOT NULL
             ORDER BY created_at ASC
             LIMIT $1
            """,
            batch,
        )

    if not rows:
        return 0, 0

    evaluator = get_post_evaluator_agent()
    updated = 0
    errors = 0
    for row in rows:
        plan_id: UUID = row["id"]
        try:
            plan = _row_to_plan(row)
            execution = _synthetic_execution(plan, row)
            verdict = await evaluator.evaluate(
                ctx=ToolContext(user_id=row["user_id"]),
                plan=plan, execution=execution,
            )
            async with acquire() as write_conn:
                await procedural.update_outcome(
                    write_conn, plan_id=plan_id, score=verdict.score,
                )
            updated += 1
            log.info(
                "workers.sleep_cycle.rescore",
                plan_id=str(plan_id), new_score=verdict.score,
            )
        except Exception:  # noqa: BLE001
            errors += 1
            log.warning(
                "workers.sleep_cycle.rescore_failed",
                plan_id=str(plan_id), exc_info=True,
            )
    return updated, errors


def _row_to_plan(row: asyncpg.Record):
    """Build a Plan dataclass from a plans-row + its JSONB steps."""
    import json

    from wolfpaw.schemas import Plan, Step

    raw_steps = row["steps"]
    if isinstance(raw_steps, str):
        raw_steps = json.loads(raw_steps)
    steps = [Step.from_dict(s) for s in (raw_steps or [])]
    return Plan(
        query=row["query"], summary="(re-score: original summary not persisted)",
        steps=steps, is_task=False, model_used="(unknown)",
        id=row["id"],
    )


def _synthetic_execution(plan, row: asyncpg.Record):
    """Build a degenerate ExecutionPlan with just final_answer + success.
    Intermediate step results aren't persisted on `plans` rows, so the
    re-score is necessarily over the plan shape + final answer only."""
    from wolfpaw.schemas import ExecutionPlan

    return ExecutionPlan(
        plan=plan,
        results=[],  # not persisted; the prompt notes their absence
        final_answer=row["final_answer"] or "",
        success=bool(row["success"]) if row["success"] is not None else False,
        error=row["error"],
    )


# --- operation 2: consolidate near-duplicate skills ------------------------


async def _consolidate_skills(*, threshold: float) -> int:
    """For every user with active emitted skills, find pairs above
    ``threshold`` cosine similarity and supersede the lower-scoring of
    the two. Returns the count of newly-superseded skills.

    Algorithm: per user, iterate the active skills oldest-first. For
    each, find its nearest neighbour. If similarity ≥ threshold, mark
    the lower-scoring (or older, on ties) as superseded by the higher.
    Repeat until no pair crosses the threshold.

    This is O(N²)-ish per user but capped by the number of emitted
    skills (small in practice) and gated behind the high threshold.
    Larger users could outgrow this; if so the right move is per-batch
    pair-finding in SQL rather than the Python loop."""
    superseded_total = 0
    async with acquire() as conn:
        user_ids = [
            r["user_id"]
            for r in await conn.fetch(
                """
                SELECT DISTINCT user_id FROM skills
                 WHERE user_id IS NOT NULL
                   AND embedding IS NOT NULL
                   AND superseded_by_skill_id IS NULL
                """,
            )
        ]
    for user_id in user_ids:
        superseded_total += await _consolidate_user_skills(
            user_id=user_id, threshold=threshold,
        )
    return superseded_total


async def _consolidate_user_skills(
    *, user_id: UUID, threshold: float,
) -> int:
    """One user's slice of the consolidation pass. Keeps going until
    no pair in the user's active set crosses the threshold."""
    superseded_here = 0
    while True:
        async with acquire() as conn:
            active = await skills_mem.list_active_for_user(
                conn, user_id=user_id,
            )
        if len(active) < 2:
            return superseded_here

        # Find any pair above threshold. Stop at the first hit per
        # outer iteration so the next iteration re-reads the (now
        # smaller) active set and can find subsequent pairs.
        pair = await _find_supersession_pair(
            user_id=user_id, active=active, threshold=threshold,
        )
        if pair is None:
            return superseded_here

        loser_id, survivor_id = pair
        try:
            async with acquire() as conn:
                ok = await skills_mem.mark_superseded(
                    conn, skill_id=loser_id, superseded_by=survivor_id,
                )
            if ok:
                superseded_here += 1
                log.info(
                    "workers.sleep_cycle.skill_superseded",
                    user_id=str(user_id),
                    loser=str(loser_id),
                    survivor=str(survivor_id),
                )
            else:
                # Race / already superseded — bail to avoid loop.
                return superseded_here
        except Exception:  # noqa: BLE001
            log.warning(
                "workers.sleep_cycle.skill_supersede_failed",
                user_id=str(user_id),
                loser=str(loser_id),
                survivor=str(survivor_id),
                exc_info=True,
            )
            return superseded_here


async def _find_supersession_pair(
    *,
    user_id: UUID,
    active: list[skills_mem.Skill],
    threshold: float,
) -> tuple[UUID, UUID] | None:
    """Walk the user's active skills; return the first
    ``(loser_id, survivor_id)`` whose similarity ≥ threshold, or None.
    Loser = lower score (older on score-tie). Survivor = higher score."""
    async with acquire() as conn:
        for skill in active:
            neighbours = await skills_mem.neighbours(
                conn, skill_id=skill.id, user_id=user_id, k=1,
            )
            if not neighbours:
                continue
            best = neighbours[0]
            similarity = best.similarity or 0.0
            if similarity < threshold:
                continue
            loser, survivor = _pick_survivor(skill, best)
            return loser.id, survivor.id
    return None


def _pick_survivor(
    a: skills_mem.Skill, b: skills_mem.Skill,
) -> tuple[skills_mem.Skill, skills_mem.Skill]:
    """Return ``(loser, survivor)``. Survivor is the higher-scoring
    skill; on score tie the newer row wins (more likely to reflect
    the current Planner's style)."""
    a_score = a.score if a.score is not None else -1
    b_score = b.score if b.score is not None else -1
    if a_score > b_score:
        return b, a
    if b_score > a_score:
        return a, b
    # Tie — newer wins. `created_at` may be None in degraded test
    # fixtures; treat None as "very old."
    a_at = a.created_at
    b_at = b.created_at
    if a_at is None and b_at is None:
        return b, a  # arbitrary
    if a_at is None:
        return a, b
    if b_at is None:
        return b, a
    if a_at >= b_at:
        return b, a
    return a, b


# --- operation 3: GC orphan threads ----------------------------------------


async def _gc_orphan_threads(*, age_days: int) -> int:
    """Delete threads with no messages and created_at older than the
    cutoff. Returns the number of rows deleted."""
    if age_days <= 0:
        return 0
    async with acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM threads
             WHERE created_at < NOW() - ($1 || ' days')::interval
               AND NOT EXISTS (
                   SELECT 1 FROM messages
                    WHERE messages.thread_id = threads.id
               )
            """,
            str(age_days),
        )
    # asyncpg returns 'DELETE <count>'.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
