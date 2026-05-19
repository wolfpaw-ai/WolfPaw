"""Token-usage writer — one row per model call.

Synchronous-write semantics: the row must persist before the caller returns
the answer to the user. Async-batch flushing can land later if Postgres
becomes the bottleneck; for v1 latency is fine.
"""

from __future__ import annotations

from uuid import UUID

from wolfpaw.memory.db import acquire
from wolfpaw.metering.types import TokenCounts
from wolfpaw.tracing import get_logger, get_trace_id

log = get_logger()


async def record_usage(
    *,
    user_id: UUID,
    agent: str,
    model: str,
    usage: TokenCounts,
    cost_cents: int,
    prompt_version_id: UUID | None = None,
    task_id: UUID | None = None,
    request_id: str | None = None,
) -> UUID:
    """Insert one `token_usage` row. Returns the inserted row id.

    `trace_id` is pulled from the request-scoped contextvar — no need to
    thread it through every call site.
    """
    trace_id = get_trace_id()
    async with acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO token_usage"
            " (user_id, task_id, trace_id, request_id, agent, model,"
            "  prompt_version_id, input_tokens, output_tokens,"
            "  cache_read_tokens, cache_write_tokens, cost_cents)"
            " VALUES ($1, $2, $3, $4, $5::agent_kind, $6, $7,"
            "         $8, $9, $10, $11, $12)"
            " RETURNING id",
            user_id,
            task_id,
            trace_id,
            request_id,
            agent,
            model,
            prompt_version_id,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            cost_cents,
        )
    log.info(
        "model.call.recorded",
        agent=agent,
        model=model,
        cost_cents=cost_cents,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        prompt_version_id=str(prompt_version_id) if prompt_version_id else None,
    )
    return row_id
