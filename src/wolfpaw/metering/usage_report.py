"""`/usage` command — read-side aggregator over `token_usage` + `compute_usage`.

Pipeline:
    build_usage_report(user_id, scope) → UsageReport (dataclass)
    render_text(report)                 → str (default plaintext rendering)

Channels can either call `render_text` or render `UsageReport` themselves
into a channel-native shape (markdown tables for web, code blocks for
Telegram, etc).

Cost split is re-derived per-row via `LATERAL JOIN model_prices` on the
call's `created_at`. We don't trust a single current price snapshot — if
prices changed mid-period, each call is priced as it was at the time, per
the plan's "cost computation honesty" section.

Cached per `(user_id, scope)` for 30 seconds. The cache is process-local
which is correct for the single-process v1 deployment; if we ever scale
horizontally the cache moves to Redis (and we'll bump the TTL discussion
along with it).
"""

from __future__ import annotations

import time
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from wolfpaw.channels import InboundMessage
from wolfpaw.channels.commands import CommandResult, register
from wolfpaw.memory.db import acquire

Scope = Literal["default", "today", "month", "all"]

CACHE_TTL_SECONDS = 30


# --- dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class ModelRow:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    input_cost_cents: int
    output_cost_cents: int
    cache_cost_cents: int
    total_cost_cents: int


@dataclass(frozen=True)
class AgentRow:
    agent: str
    cost_cents: int


@dataclass(frozen=True)
class PeriodReport:
    label: str
    start: datetime
    end: datetime
    models: list[ModelRow] = field(default_factory=list)
    by_agent: list[AgentRow] = field(default_factory=list)
    compute_cost_cents: int = 0
    total_cost_cents: int = 0  # tokens + compute


@dataclass(frozen=True)
class UsageReport:
    user_id: UUID
    scope: Scope
    generated_at: datetime
    timezone: str
    periods: list[PeriodReport]
    include_by_agent: bool = False


# --- period math -------------------------------------------------------------


def _today_window(now_utc: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local_now = now_utc.astimezone(tz)
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _calendar_month_window(
    now_utc: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    local_now = now_utc.astimezone(tz)
    start_local = local_now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    _, last_day = monthrange(local_now.year, local_now.month)
    end_local = start_local.replace(day=last_day) + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _format_period_label(prefix: str, start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """e.g. 'Current period (May 1 – May 31, 2026)' / 'Today (May 23)'."""
    s = start.astimezone(tz)
    # end is exclusive — show the last *included* day.
    last = (end - timedelta(seconds=1)).astimezone(tz)
    if prefix == "Today":
        return f"Today ({s.strftime('%b')} {s.day})"
    if s.year == last.year and s.month == last.month and s.day == last.day:
        return f"{prefix} ({s.strftime('%b')} {s.day}, {s.year})"
    return f"{prefix} ({s.strftime('%b')} {s.day} – {last.strftime('%b')} {last.day}, {last.year})"


# --- DB ----------------------------------------------------------------------


async def _fetch_timezone(conn: asyncpg.Connection, user_id: UUID) -> str:
    row = await conn.fetchval(
        "SELECT timezone FROM user_profiles WHERE user_id = $1", user_id
    )
    return row or "UTC"


async def _fetch_subscription_period(
    conn: asyncpg.Connection, user_id: UUID
) -> tuple[datetime | None, datetime | None]:
    row = await conn.fetchrow(
        "SELECT current_period_start, current_period_end"
        "  FROM subscriptions WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return None, None
    return row["current_period_start"], row["current_period_end"]


_MODEL_BREAKDOWN_SQL = """
WITH priced AS (
    SELECT
        tu.model,
        tu.input_tokens,
        tu.output_tokens,
        tu.cache_read_tokens,
        tu.cache_write_tokens,
        tu.input_tokens::numeric
            * COALESCE(mp.input_per_mtok_cents, 0) / 1000000        AS input_cost,
        tu.output_tokens::numeric
            * COALESCE(mp.output_per_mtok_cents, 0) / 1000000       AS output_cost,
        tu.cache_read_tokens::numeric
            * COALESCE(mp.cache_read_per_mtok_cents, 0) / 1000000   AS cache_read_cost,
        tu.cache_write_tokens::numeric
            * COALESCE(mp.cache_write_per_mtok_cents, 0) / 1000000  AS cache_write_cost
    FROM token_usage tu
    LEFT JOIN LATERAL (
        SELECT input_per_mtok_cents, output_per_mtok_cents,
               cache_read_per_mtok_cents, cache_write_per_mtok_cents
          FROM model_prices
         WHERE model_id = tu.model
           AND effective_from <= tu.created_at
           AND (effective_to IS NULL OR effective_to > tu.created_at)
         ORDER BY effective_from DESC LIMIT 1
    ) mp ON TRUE
    WHERE tu.user_id = $1
      AND tu.created_at >= $2
      AND tu.created_at <  $3
)
SELECT
    model,
    SUM(input_tokens)::bigint                                   AS input_tokens,
    SUM(output_tokens)::bigint                                  AS output_tokens,
    SUM(cache_read_tokens)::bigint                              AS cache_read_tokens,
    SUM(cache_write_tokens)::bigint                             AS cache_write_tokens,
    CEIL(SUM(input_cost))::bigint                               AS input_cost_cents,
    CEIL(SUM(output_cost))::bigint                              AS output_cost_cents,
    CEIL(SUM(cache_read_cost) + SUM(cache_write_cost))::bigint  AS cache_cost_cents,
    CEIL(SUM(input_cost) + SUM(output_cost)
         + SUM(cache_read_cost) + SUM(cache_write_cost))::bigint AS total_cost_cents
  FROM priced
 GROUP BY model
 ORDER BY total_cost_cents DESC, model
"""

_BY_AGENT_SQL = """
SELECT agent::text AS agent, COALESCE(SUM(cost_cents), 0)::bigint AS cost_cents
  FROM token_usage
 WHERE user_id = $1
   AND created_at >= $2
   AND created_at <  $3
 GROUP BY agent
 ORDER BY cost_cents DESC, agent
"""

_COMPUTE_SQL = """
SELECT COALESCE(SUM(cost_cents), 0)::bigint AS compute_cost_cents
  FROM compute_usage
 WHERE user_id = $1
   AND created_at >= $2
   AND created_at <  $3
"""


async def _build_period(
    conn: asyncpg.Connection,
    user_id: UUID,
    label: str,
    start: datetime,
    end: datetime,
    include_by_agent: bool,
) -> PeriodReport:
    model_rows = await conn.fetch(_MODEL_BREAKDOWN_SQL, user_id, start, end)
    models = [
        ModelRow(
            model=r["model"],
            input_tokens=int(r["input_tokens"] or 0),
            output_tokens=int(r["output_tokens"] or 0),
            cache_read_tokens=int(r["cache_read_tokens"] or 0),
            cache_write_tokens=int(r["cache_write_tokens"] or 0),
            input_cost_cents=int(r["input_cost_cents"] or 0),
            output_cost_cents=int(r["output_cost_cents"] or 0),
            cache_cost_cents=int(r["cache_cost_cents"] or 0),
            total_cost_cents=int(r["total_cost_cents"] or 0),
        )
        for r in model_rows
    ]
    compute_cents = int(
        await conn.fetchval(_COMPUTE_SQL, user_id, start, end) or 0
    )
    by_agent: list[AgentRow] = []
    if include_by_agent:
        rows = await conn.fetch(_BY_AGENT_SQL, user_id, start, end)
        by_agent = [
            AgentRow(agent=r["agent"], cost_cents=int(r["cost_cents"]))
            for r in rows
        ]
    token_total = sum(m.total_cost_cents for m in models)
    return PeriodReport(
        label=label,
        start=start,
        end=end,
        models=models,
        by_agent=by_agent,
        compute_cost_cents=compute_cents,
        total_cost_cents=token_total + compute_cents,
    )


# --- cache -------------------------------------------------------------------


_cache: dict[tuple[UUID, Scope], tuple[float, UsageReport]] = {}


def clear_cache() -> None:
    _cache.clear()


def _cache_get(user_id: UUID, scope: Scope) -> UsageReport | None:
    entry = _cache.get((user_id, scope))
    if entry is None:
        return None
    written_at, report = entry
    if time.monotonic() - written_at > CACHE_TTL_SECONDS:
        _cache.pop((user_id, scope), None)
        return None
    return report


def _cache_put(user_id: UUID, scope: Scope, report: UsageReport) -> None:
    _cache[(user_id, scope)] = (time.monotonic(), report)


# --- public builder ----------------------------------------------------------


_VALID_SCOPES: tuple[Scope, ...] = ("default", "today", "month", "all")


def parse_scope(args: str) -> Scope:
    arg = args.strip().lower()
    if not arg:
        return "default"
    if arg in _VALID_SCOPES:
        return arg  # type: ignore[return-value]
    raise ValueError(
        f"Unknown /usage scope: {arg!r}. Try one of: today, month, all."
    )


async def build_usage_report(
    user_id: UUID, scope: Scope = "default"
) -> UsageReport:
    cached = _cache_get(user_id, scope)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)
    async with acquire() as conn:
        tz_name = await _fetch_timezone(conn, user_id)
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
        sub_start, sub_end = await _fetch_subscription_period(conn, user_id)

        periods: list[PeriodReport] = []
        include_by_agent = scope == "month"

        if scope in ("default", "month"):
            if sub_start and sub_end:
                p_start, p_end = sub_start, sub_end
                prefix = "Current period"
            else:
                p_start, p_end = _calendar_month_window(now, tz)
                prefix = "Current period"
            label = _format_period_label(prefix, p_start, p_end, tz)
            periods.append(
                await _build_period(
                    conn, user_id, label, p_start, p_end, include_by_agent
                )
            )

        if scope in ("default", "today"):
            t_start, t_end = _today_window(now, tz)
            label = _format_period_label("Today", t_start, t_end, tz)
            periods.append(
                await _build_period(conn, user_id, label, t_start, t_end, False)
            )

        if scope == "all":
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            far_future = datetime(9999, 1, 1, tzinfo=timezone.utc)
            label = "Lifetime (all time)"
            periods.append(
                await _build_period(
                    conn, user_id, label, epoch, far_future, False
                )
            )

    report = UsageReport(
        user_id=user_id,
        scope=scope,
        generated_at=now,
        timezone=tz_name,
        periods=periods,
        include_by_agent=include_by_agent,
    )
    _cache_put(user_id, scope, report)
    return report


# --- text rendering (default) ------------------------------------------------


def _fmt_dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _render_period(period: PeriodReport) -> list[str]:
    lines = [period.label]
    if not period.models and period.compute_cost_cents == 0:
        lines.append("  (no usage yet)")
        return lines
    for m in period.models:
        tokens_part = f"{_fmt_int(m.input_tokens)} in"
        if m.output_tokens:
            tokens_part += f" / {_fmt_int(m.output_tokens)} out"
        if m.output_tokens:
            cost_part = (
                f"{_fmt_dollars(m.input_cost_cents)}"
                f" + {_fmt_dollars(m.output_cost_cents)}"
                f" = {_fmt_dollars(m.total_cost_cents)}"
            )
        else:
            cost_part = _fmt_dollars(m.total_cost_cents)
        lines.append(f"  {m.model:<18} {tokens_part:<32} {cost_part}")
    if period.compute_cost_cents:
        lines.append(
            f"  {'sandbox compute':<18} {'':<32} "
            f"{_fmt_dollars(period.compute_cost_cents)}"
        )
    lines.append(
        f"  {'':<18} {'Total:':>32} {_fmt_dollars(period.total_cost_cents)}"
    )
    if period.by_agent:
        lines.append("  By agent:")
        for a in period.by_agent:
            lines.append(f"    {a.agent:<16} {_fmt_dollars(a.cost_cents)}")
    return lines


def render_text(report: UsageReport) -> str:
    blocks: list[list[str]] = [_render_period(p) for p in report.periods]
    sections = ["\n".join(b) for b in blocks]
    return "\n\n".join(sections)


# --- slash command -----------------------------------------------------------


@register("usage", "Show token spend + sandbox compute (today | month | all).")
async def _usage_command(message: InboundMessage, args: str) -> CommandResult:
    try:
        scope = parse_scope(args)
    except ValueError as e:
        return CommandResult(text=str(e))
    report = await build_usage_report(message.user_id, scope)
    return CommandResult(text=render_text(report))
