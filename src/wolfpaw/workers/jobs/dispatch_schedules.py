"""Schedule dispatcher — the "when" half of scheduled tasks.

Registered as a per-minute arq cron (see arq_app.py). Each firing claims
the `schedules` rows whose `next_run_at` is due, spawns one Task per
schedule from its stored instruction, and lets the existing
plan -> execute -> notify pipeline do the rest. The DAO advances each
claimed row to its next slot atomically (FOR UPDATE SKIP LOCKED), so this
job is safe to run concurrently and across overlapping minutes.

Gating: no-ops unless ``WOLFPAW_SCHEDULES_ENABLED=true`` — same posture as
the Sleep Cycle, so the cron is safe to keep registered everywhere and the
operator opts into the recurring token spend.

At-most-once semantics: a row is advanced (committed) before its Task is
enqueued. A crash in the narrow window between commit and enqueue drops
that single firing rather than risking a double-fire. Acceptable for
reminders; a future "pending schedule_id with no task" sweep could close
the gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from wolfpaw.config import get_settings
from wolfpaw.memory import schedules as schedules_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


@dataclass(frozen=True)
class DispatchResult:
    """Per-run summary — exposed for tests + observability."""

    claimed: int
    spawned: int
    errors: int


def _render_content(schedule: schedules_dao.Schedule) -> str:
    """Build the Task content the agent sees for one scheduled run: the
    instruction, the resolved context, any cross-run notes, and a header
    making clear it's headless (no human to ask) plus how to remember
    things for the next run."""
    parts: list[str] = [schedule.instruction.strip(), ""]

    if schedule.context:
        parts.append("[Scheduling context]")
        for key, value in schedule.context.items():
            parts.append(f"- {key}: {value}")
        parts.append("")

    if schedule.state:
        parts.append(
            "[Notes you left on previous runs] " + json.dumps(schedule.state)
        )
        parts.append("")

    parts.append(
        f"You are running as scheduled job {schedule.id} — there is no human"
        " available to answer questions, so do not ask any; act on the"
        " instruction and stop. Reach out to the user only if the"
        " instruction says to (e.g. via send_telegram_message). If you need"
        " to remember something so a future run doesn't repeat itself (for"
        " example, that you already notified the user), call"
        f' update_schedule_state with schedule_id="{schedule.id}".'
    )
    return "\n".join(parts)


async def dispatch_schedules() -> DispatchResult:
    """Claim + run all currently-due schedules. Returns counters."""
    settings = get_settings()
    if not settings.schedules_enabled:
        log.info("workers.dispatch_schedules.disabled")
        return DispatchResult(0, 0, 0)

    now = datetime.now(timezone.utc)
    async with acquire() as conn:
        claimed = await schedules_dao.claim_and_advance_due(
            conn, now=now, limit=settings.schedules_dispatch_batch,
        )
    if not claimed:
        return DispatchResult(0, 0, 0)

    log.info("workers.dispatch_schedules.claimed", count=len(claimed))

    spawned = 0
    errors = 0
    for schedule in claimed:
        try:
            await _spawn_run(schedule)
            spawned += 1
        except Exception:  # noqa: BLE001 — one bad schedule can't stop the batch
            errors += 1
            log.warning(
                "workers.dispatch_schedules.spawn_failed",
                schedule_id=str(schedule.id), exc_info=True,
            )

    log.info(
        "workers.dispatch_schedules.complete",
        claimed=len(claimed), spawned=spawned, errors=errors,
    )
    return DispatchResult(claimed=len(claimed), spawned=spawned, errors=errors)


async def _spawn_run(schedule: schedules_dao.Schedule) -> None:
    """Create a Task from the schedule and enqueue it to run. Imports are
    local to keep the worker import graph lean (and avoid a cycle through
    the queue module)."""
    from wolfpaw.tasks.service import get_task_service
    from wolfpaw.workers.queue import enqueue_run_task

    task = await get_task_service().create(
        user_id=schedule.user_id,
        thread_id=None,
        content=_render_content(schedule),
        title=schedule.title or "Scheduled task",
        channel_for_completion=schedule.channel,
        complexity_hint="moderate",
        # Run scheduled work through the Quick agent's tool loop, not the
        # static planner→executor pipeline — the loop calls tools with
        # real outputs in context, so "compose X then send it" actually
        # sends (the pipeline can't thread a composed value into a tool
        # call). See TaskService._run_agentic.
        agentic=True,
    )
    # Trace the run back to its schedule. Best-effort — the run is already
    # created; a missing back-link only costs observability.
    try:
        async with acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET schedule_id = $2 WHERE id = $1",
                task.id, schedule.id,
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "workers.dispatch_schedules.link_failed",
            task_id=str(task.id), schedule_id=str(schedule.id), exc_info=True,
        )

    await enqueue_run_task(task.id)
    log.info(
        "workers.dispatch_schedules.spawned",
        schedule_id=str(schedule.id), task_id=str(task.id),
    )


# --- arq wrapper -----------------------------------------------------------


async def dispatch_schedules_job(_ctx: dict) -> dict:
    """arq-shaped entry point. Returns the counters so the cron history
    carries the per-run stats."""
    result = await dispatch_schedules()
    return {
        "claimed": result.claimed,
        "spawned": result.spawned,
        "errors": result.errors,
    }
