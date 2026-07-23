"""Retention + partition maintenance for `model_call_logs`.

Two jobs in one pass, both idempotent:

- **Provision ahead.** Ensure the partitions for this month and next exist.
  A write with no partition to land in *errors*, so provisioning has to run
  before the month rolls over, not on demand at insert time.
- **Drop expired.** Remove whole partitions whose entire range is older than
  ``trace_retention_days``. Dropping the table returns the disk immediately;
  a ``DELETE`` on a table this wide would leave dead tuples behind and hand
  autovacuum a large, pointless job.

Because retention is enforced at partition granularity, the effective cutoff
rounds up: with a 14-day setting, rows survive until the end of the last month
that *starts* inside the window. That's the deliberate trade for O(1) deletes —
tighten it by shortening the partition interval, not by switching to DELETE.

Metering rows in `token_usage` are untouched. They're small, they're
billing-relevant, and they keep a much longer history than the payloads do —
that split is the whole point of the two-table design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()

_PARTITION_PREFIX = "model_call_logs_"


@dataclass(frozen=True, slots=True)
class PruneResult:
    provisioned: list[str]
    dropped: list[str]


def _month_start(at: datetime) -> datetime:
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def prune_traces(now: datetime | None = None) -> PruneResult:
    """Provision upcoming partitions, drop expired ones. Safe to run often."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.trace_retention_days)
    # A partition is expendable only when its whole range predates the cutoff,
    # i.e. the month *after* it started has also ended before the cutoff.
    # Comparing against the cutoff's own month start gives exactly that.
    drop_before = _month_start(cutoff)

    provisioned: list[str] = []
    dropped: list[str] = []

    async with acquire() as conn:
        for offset_months in (0, 1):
            at = now if offset_months == 0 else _month_start(now) + timedelta(days=32)
            name = await conn.fetchval("SELECT ensure_model_call_log_partition($1)", at)
            provisioned.append(name)

        rows = await conn.fetch(
            """
            SELECT c.relname AS name
              FROM pg_class c
              JOIN pg_inherits i ON i.inhrelid = c.oid
              JOIN pg_class p ON p.oid = i.inhparent
             WHERE p.relname = 'model_call_logs'
            """
        )
        for row in rows:
            name = row["name"]
            start = _partition_start(name)
            if start is None or start >= drop_before:
                continue
            # Partition names are derived from pg_class, not user input, but
            # they still go through format(%I) — identifiers can't be bound as
            # parameters and a DROP is not the place to relax that.
            await conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            dropped.append(name)

    log.info(
        "workers.prune_traces.complete",
        retention_days=settings.trace_retention_days,
        provisioned=provisioned,
        dropped=dropped,
    )
    return PruneResult(provisioned=provisioned, dropped=dropped)


def _partition_start(name: str) -> datetime | None:
    """Parse `model_call_logs_YYYY_MM` back into its range start. Returns None
    for anything that doesn't match, so a hand-created partition is left alone
    rather than dropped on a bad guess."""
    if not name.startswith(_PARTITION_PREFIX):
        return None
    suffix = name[len(_PARTITION_PREFIX) :]
    try:
        year, month = suffix.split("_")
        return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def prune_traces_job(_ctx: dict) -> dict:
    """arq entrypoint."""
    result = await prune_traces()
    return {"provisioned": result.provisioned, "dropped": result.dropped}
