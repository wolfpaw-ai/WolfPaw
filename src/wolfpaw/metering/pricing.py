"""Per-call cost computation.

`compute_cost_cents` is pure — it takes a price row and a token tally and
returns cents. The DB-bound `get_active_price` fetches the right row for a
given model + time (defaulting to NOW), respecting `effective_from /
effective_to`. Historical rows in `token_usage` are priced against whichever
row was in effect at the time of the call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import ceil

import asyncpg

from wolfpaw.metering.types import ModelPrice, TokenCounts

_MTOK = 1_000_000


def compute_cost_cents(price: ModelPrice, usage: TokenCounts) -> int:
    """Return total cost in whole cents (rounded up — never undercharge)."""
    total = 0.0
    total += usage.input_tokens * price.input_per_mtok_cents / _MTOK
    total += usage.output_tokens * price.output_per_mtok_cents / _MTOK
    total += usage.cache_read_tokens * price.cache_read_per_mtok_cents / _MTOK
    total += usage.cache_write_tokens * price.cache_write_per_mtok_cents / _MTOK
    return ceil(total)


async def get_active_price(
    conn: asyncpg.Connection,
    model_id: str,
    at_time: datetime | None = None,
) -> ModelPrice | None:
    at_time = at_time or datetime.now(timezone.utc)
    row = await conn.fetchrow(
        "SELECT model_id, input_per_mtok_cents, output_per_mtok_cents,"
        "       cache_read_per_mtok_cents, cache_write_per_mtok_cents,"
        "       effective_from"
        "  FROM model_prices"
        " WHERE model_id = $1"
        "   AND effective_from <= $2"
        "   AND (effective_to IS NULL OR effective_to > $2)"
        " ORDER BY effective_from DESC LIMIT 1",
        model_id,
        at_time,
    )
    if row is None:
        return None
    return ModelPrice(**dict(row))
