"""Phase 0 baseline for the content-by-reference (blackboard) work.

Read-only report over `model_call_logs`. Answers two questions before any
of the blackboard machinery gets built:

  1. **How often does the output budget actually blow?** Calls that end on
     `stop_reason = 'max_tokens'`, grouped by agent + model. This is the
     failure the `write_doc` bug was an instance of — the model asked to
     re-emit content it was already handed, and ran out of room.

  2. **How many bytes are we paying the model to photocopy?** Every
     `tool_use` block in a response carries an `input` object the model
     generated token by token. Large ones are the blackboard's target:
     content that already existed somewhere and got retyped. Grouped by
     agent + tool, so it's obvious which tools to convert first.

Together these size the prize and give the before/after baseline that
Phase 5's tokens-saved metric compares against. Run it again after each
phase lands.

The third Phase 0 signal — silent prompt-side clipping — is not in this
table. It's the `executor.prior_result.truncated` warning emitted by
`agents/executor.py`, counted from the structured logs:

    grep executor.prior_result.truncated <logs> | jq -s \\
      'group_by(.site) | map({site: .[0].site, n: length,
                              dropped: (map(.dropped_bytes) | add)})'

Usage:
    python -m scripts.blackboard_baseline [--days 7] [--threshold 4000]

Reads `WOLFPAW_DATABASE_URL` from the env, same as the app. Writes
nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from wolfpaw.memory.db import acquire, close_pool


# Calls that hit the output ceiling. `attempt` is not collapsed — a retry
# that also stopped on max_tokens is a distinct instance of the problem,
# not a duplicate of it.
_Q_MAX_TOKENS = """
SELECT
    agent,
    model,
    COUNT(*)                                                   AS total_calls,
    COUNT(*) FILTER (WHERE stop_reason = 'max_tokens')         AS max_tokens_stops,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE stop_reason = 'max_tokens')
        / NULLIF(COUNT(*), 0), 2
    )                                                          AS pct_of_calls
FROM model_call_logs
WHERE created_at >= NOW() - make_interval(days => $1)
GROUP BY agent, model
ORDER BY max_tokens_stops DESC, total_calls DESC;
"""

# `#>> '{}'` renders a jsonb value as its text form, so `length()` gives
# the serialized byte count of the arguments the model emitted. Rows whose
# `response_content` isn't an array (NULL on a failed attempt) are skipped
# rather than erroring `jsonb_array_elements`.
_Q_TOOL_ARG_BYTES = """
WITH blocks AS (
    SELECT
        l.agent,
        COALESCE(b->>'name', '(unnamed)')       AS tool,
        length(b->'input' #>> '{}')             AS input_bytes
    FROM model_call_logs AS l,
         LATERAL jsonb_array_elements(l.response_content) AS b
    WHERE l.created_at >= NOW() - make_interval(days => $1)
      AND jsonb_typeof(l.response_content) = 'array'
      AND b->>'type' = 'tool_use'
)
SELECT
    agent,
    tool,
    COUNT(*)                                            AS tool_calls,
    COUNT(*) FILTER (WHERE input_bytes > $2)            AS over_threshold,
    MAX(input_bytes)                                    AS max_bytes,
    ROUND(AVG(input_bytes))::BIGINT                     AS avg_bytes,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY input_bytes)::BIGINT
                                                        AS p95_bytes,
    SUM(input_bytes)                                    AS total_bytes
FROM blocks
GROUP BY agent, tool
ORDER BY total_bytes DESC;
"""

# The individual worst offenders, with `trace_id` so each one can be
# pulled up in /monitor and eyeballed: is this content the model authored
# (legitimate, stays a model call) or content it copied (the target)?
_Q_WORST_OFFENDERS = """
WITH blocks AS (
    SELECT
        l.created_at,
        l.trace_id,
        l.task_id,
        l.agent,
        COALESCE(b->>'name', '(unnamed)')       AS tool,
        length(b->'input' #>> '{}')             AS input_bytes
    FROM model_call_logs AS l,
         LATERAL jsonb_array_elements(l.response_content) AS b
    WHERE l.created_at >= NOW() - make_interval(days => $1)
      AND jsonb_typeof(l.response_content) = 'array'
      AND b->>'type' = 'tool_use'
)
SELECT created_at, trace_id, task_id, agent, tool, input_bytes
FROM blocks
WHERE input_bytes > $2
ORDER BY input_bytes DESC
LIMIT $3;
"""


def _table(title: str, rows: list, columns: list[str]) -> str:
    """Fixed-width text table. No dependency, pipes cleanly into a file."""
    out = [f"\n{title}", "=" * len(title)]
    if not rows:
        out.append("(no rows)")
        return "\n".join(out)

    data = [[_fmt(r[c]) for c in columns] for r in rows]
    widths = [
        max(len(col), *(len(row[i]) for row in data))
        for i, col in enumerate(columns)
    ]
    out.append("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    out.append("  ".join("-" * w for w in widths))
    out.extend(
        "  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in data
    )
    return "\n".join(out)


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, int) and not isinstance(v, bool) and v >= 10_000:
        return f"{v:,}"
    return str(v)


async def _run(days: int, threshold: int, limit: int) -> int:
    try:
        async with acquire() as conn:
            max_tokens = await conn.fetch(_Q_MAX_TOKENS, days)
            tool_args = await conn.fetch(_Q_TOOL_ARG_BYTES, days, threshold)
            worst = await conn.fetch(_Q_WORST_OFFENDERS, days, threshold, limit)
    except Exception as e:  # noqa: BLE001 — a report should explain itself
        print(f"[baseline] query failed: {e}", file=sys.stderr)
        return 1
    finally:
        await close_pool()

    print(
        f"\nBlackboard Phase 0 baseline — last {days} day(s), "
        f"large-argument threshold {threshold:,} bytes"
    )

    print(_table(
        "1. Calls stopped by the output ceiling",
        max_tokens,
        ["agent", "model", "total_calls", "max_tokens_stops", "pct_of_calls"],
    ))

    print(_table(
        "2. Bytes the model emitted as tool arguments",
        tool_args,
        ["agent", "tool", "tool_calls", "over_threshold",
         "avg_bytes", "p95_bytes", "max_bytes", "total_bytes"],
    ))

    print(_table(
        f"3. Worst individual arguments (over {threshold:,} bytes)",
        worst,
        ["created_at", "trace_id", "agent", "tool", "input_bytes"],
    ))

    total = sum(r["total_bytes"] or 0 for r in tool_args)
    over = sum(r["over_threshold"] or 0 for r in tool_args)
    stops = sum(r["max_tokens_stops"] or 0 for r in max_tokens)
    print(
        f"\nSummary: {total:,} bytes emitted as tool arguments across "
        f"{sum(r['tool_calls'] for r in tool_args):,} tool calls; "
        f"{over:,} argument(s) over threshold; {stops:,} call(s) stopped "
        f"on max_tokens.\n"
        "Not every byte here is recoverable — content the model *authors* "
        "has no handle to reference and correctly stays a model call. "
        "Read the row-3 traces to split the two.\n"
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Baseline report for the blackboard work (read-only)."
    )
    ap.add_argument("--days", type=int, default=7,
                    help="lookback window in days (default 7)")
    ap.add_argument("--threshold", type=int, default=4_000,
                    help="byte size above which a tool argument is 'large' "
                         "(default 4000)")
    ap.add_argument("--limit", type=int, default=20,
                    help="rows in the worst-offenders table (default 20)")
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.days, args.threshold, args.limit)))


if __name__ == "__main__":
    main()
