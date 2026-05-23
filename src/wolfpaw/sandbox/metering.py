"""Compute metering helper — writes `compute_usage` + `sandboxes` rows.

Called at sandbox teardown by SandboxManager (and could be called
periodically by a long-running sandbox if we want finer-grained metering
later). Cost is `compute_seconds * sandbox_compute_per_second_micros / 100`
in whole cents, rounded up (never undercharge — same posture as
`pricing.compute_cost_cents`).

If task_id is None (ad-hoc compute outside a task), we still write the
rows — migration 003 made task_id nullable. The /usage command already
sums `compute_usage` per user, so dev usage shows up there.
"""

from __future__ import annotations

from math import ceil
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


def compute_cost_cents(compute_seconds: float, per_second_micros: int) -> int:
    """micros = millionths of a cent. Round up to whole cents."""
    micros = compute_seconds * per_second_micros
    return ceil(micros / 1_000_000)


async def record_compute(
    *,
    user_id: UUID,
    task_id: UUID | None,
    sandbox_id: UUID | None,
    provider: str,
    compute_seconds: float,
) -> None:
    settings = get_settings()
    cost_cents = compute_cost_cents(
        compute_seconds, settings.sandbox_compute_per_second_micros
    )
    seconds_int = max(1, round(compute_seconds))  # integer schema column
    async with acquire() as conn:
        # Upsert a sandbox row so /usage can join through it if it wants to.
        if sandbox_id is not None:
            await conn.execute(
                "INSERT INTO sandboxes"
                " (id, task_id, provider, status, terminated_at,"
                "  compute_seconds, cost_cents)"
                " VALUES ($1, $2, $3, 'stopped', NOW(), $4, $5)"
                " ON CONFLICT (id) DO UPDATE SET"
                "  status = 'stopped',"
                "  terminated_at = NOW(),"
                "  compute_seconds = sandboxes.compute_seconds + EXCLUDED.compute_seconds,"
                "  cost_cents     = sandboxes.cost_cents     + EXCLUDED.cost_cents",
                sandbox_id, task_id, provider, seconds_int, cost_cents,
            )
        await conn.execute(
            "INSERT INTO compute_usage"
            " (user_id, task_id, sandbox_id, compute_seconds, cost_cents)"
            " VALUES ($1, $2, $3, $4, $5)",
            user_id, task_id, sandbox_id, seconds_int, cost_cents,
        )
    log.info(
        "sandbox.compute.recorded",
        user_id=str(user_id),
        sandbox_id=str(sandbox_id) if sandbox_id else None,
        provider=provider,
        compute_seconds=seconds_int,
        cost_cents=cost_cents,
    )
