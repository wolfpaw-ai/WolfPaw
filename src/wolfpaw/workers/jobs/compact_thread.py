"""Compact a thread's older messages into tiered summaries.

Two folds per invocation, both bounded by config knobs:

- **Level 1.** When a thread has at least
  ``compaction_trigger_threshold`` messages (default 40 = 20 verbatim +
  20 to summarize), batch the oldest ``compaction_window_size`` (default
  20) messages older than the recent window into a single L1 summary
  via Haiku. Repeat until the unsummarized-older tail drops below the
  window size.
- **Level 2.** When at least ``l2_fold_threshold`` (default 10)
  un-folded L1 summaries exist, fold the oldest 10 into a single L2
  summary-of-summaries. The folded L1 rows stay in the table but get
  their ``folded_into_summary_id`` stamped so :func:`conv.fetch_summaries`
  prefers the L2.

The summarizer is injectable via :func:`set_summarizer_for_test` so
unit tests can exercise the trigger + folding logic without a live
Anthropic key. The production summarizer routes through the existing
:class:`ModelClient` so the call shows up in ``token_usage`` /
LangSmith under ``agent='compactor'``.

Caller-side rules:

- ``compact_thread(thread_id)`` is idempotent. Concurrent invocations
  race only on the L1 / L2 insert; the trigger-threshold checks
  re-evaluate after every fold so the second invocation just no-ops
  when the first has already drained the backlog.
- Failures inside the summarizer log + return — they never raise back
  into the ``conv.append`` follow-up.
"""

from __future__ import annotations

from typing import Awaitable, Callable
from uuid import UUID

import asyncpg

from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


# A summarizer takes a list of message dicts (or summary dicts for L2)
# plus the owning user_id (for cost attribution on the production path)
# and returns the compressed Markdown summary text.
Summarizer = Callable[[list[dict], UUID], Awaitable[str]]


_summarizer_override: Summarizer | None = None


def set_summarizer_for_test(fn: Summarizer | None) -> None:
    """Inject a summarizer for tests. Pass ``None`` to restore the
    production Haiku-backed summarizer."""
    global _summarizer_override
    _summarizer_override = fn


async def compact_thread(thread_id: UUID) -> None:
    """One drain pass over the thread. Safe to call after every message
    append — does nothing when the thread is below threshold."""
    settings = get_settings()
    summarizer = _summarizer_override or _haiku_summarizer

    try:
        async with acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT user_id FROM threads WHERE id = $1", thread_id,
            )
            if user_id is None:
                return
            await _drain_l1(
                conn,
                thread_id=thread_id,
                user_id=user_id,
                recent_window=settings.recent_window_size,
                window_size=settings.compaction_window_size,
                trigger_threshold=settings.compaction_trigger_threshold,
                summarizer=summarizer,
            )
            await _drain_l2(
                conn,
                thread_id=thread_id,
                user_id=user_id,
                fold_threshold=settings.l2_fold_threshold,
                summarizer=summarizer,
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "workers.compact_thread.failed",
            thread_id=str(thread_id), exc_info=True,
        )


# --- level 1 ---------------------------------------------------------------


async def _drain_l1(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    user_id: UUID,
    recent_window: int,
    window_size: int,
    trigger_threshold: int,
    summarizer: Summarizer,
) -> None:
    """Repeatedly summarize the oldest ``window_size`` messages that
    aren't yet covered by an L1, until fewer than ``window_size``
    unsummarized-older messages remain."""
    while True:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE thread_id = $1",
            thread_id,
        )
        if total < trigger_threshold:
            return

        unsummarized = await _fetch_unsummarized_older(
            conn,
            thread_id=thread_id,
            recent_window=recent_window,
            limit=window_size,
        )
        if len(unsummarized) < window_size:
            return

        summary_md = await summarizer(
            [
                {
                    "role": m["role"],
                    "content": m["content"],
                    "created_at": m["created_at"].isoformat(),
                }
                for m in unsummarized
            ],
            user_id,
        )
        if not summary_md:
            log.warning(
                "workers.compact_thread.l1.empty_summary",
                thread_id=str(thread_id),
            )
            return

        first_id = unsummarized[0]["id"]
        last_id = unsummarized[-1]["id"]
        await conn.execute(
            """
            INSERT INTO thread_summaries
                (thread_id, level, summary_md,
                 range_start_message_id, range_end_message_id)
            VALUES ($1, 1, $2, $3, $4)
            """,
            thread_id, summary_md, first_id, last_id,
        )
        log.info(
            "workers.compact_thread.l1.created",
            thread_id=str(thread_id),
            messages_summarized=len(unsummarized),
        )


async def _fetch_unsummarized_older(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    recent_window: int,
    limit: int,
) -> list[asyncpg.Record]:
    """Return up to ``limit`` messages, oldest-first, that (a) sit
    outside the most recent ``recent_window`` and (b) aren't already
    covered by an existing L1 summary's [start, end] range."""
    return await conn.fetch(
        """
        WITH recent AS (
            SELECT id
              FROM messages
             WHERE thread_id = $1
             ORDER BY created_at DESC, id DESC
             LIMIT $2
        ),
        covered_end AS (
            -- The newest message that's already inside an L1 summary's range.
            SELECT MAX(m.created_at) AS ts
              FROM thread_summaries ts
              JOIN messages m ON m.id = ts.range_end_message_id
             WHERE ts.thread_id = $1 AND ts.level = 1
        )
        SELECT m.id, m.role::text AS role, m.content, m.created_at
          FROM messages m
         WHERE m.thread_id = $1
           AND m.id NOT IN (SELECT id FROM recent)
           AND (
                (SELECT ts FROM covered_end) IS NULL
             OR m.created_at > (SELECT ts FROM covered_end)
           )
         ORDER BY m.created_at ASC, m.id ASC
         LIMIT $3
        """,
        thread_id, recent_window, limit,
    )


# --- level 2 ---------------------------------------------------------------


async def _drain_l2(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    user_id: UUID,
    fold_threshold: int,
    summarizer: Summarizer,
) -> None:
    """Fold the oldest ``fold_threshold`` un-folded L1s into one L2,
    then stamp ``folded_into_summary_id`` on each child. Repeats until
    fewer than ``fold_threshold`` un-folded L1s remain."""
    while True:
        l1s = await conn.fetch(
            """
            SELECT id, summary_md,
                   range_start_message_id, range_end_message_id, created_at
              FROM thread_summaries
             WHERE thread_id = $1 AND level = 1
               AND folded_into_summary_id IS NULL
             ORDER BY created_at ASC, id ASC
             LIMIT $2
            """,
            thread_id, fold_threshold,
        )
        if len(l1s) < fold_threshold:
            return

        summary_md = await summarizer(
            [
                {
                    "kind": "level_1_summary",
                    "summary_md": r["summary_md"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in l1s
            ],
            user_id,
        )
        if not summary_md:
            log.warning(
                "workers.compact_thread.l2.empty_summary",
                thread_id=str(thread_id),
            )
            return

        first_range_start = l1s[0]["range_start_message_id"]
        last_range_end = l1s[-1]["range_end_message_id"]
        l2_id = await conn.fetchval(
            """
            INSERT INTO thread_summaries
                (thread_id, level, summary_md,
                 range_start_message_id, range_end_message_id)
            VALUES ($1, 2, $2, $3, $4)
            RETURNING id
            """,
            thread_id, summary_md, first_range_start, last_range_end,
        )
        await conn.execute(
            "UPDATE thread_summaries SET folded_into_summary_id = $1"
            " WHERE id = ANY($2::uuid[])",
            l2_id, [r["id"] for r in l1s],
        )
        log.info(
            "workers.compact_thread.l2.created",
            thread_id=str(thread_id),
            l1_count=len(l1s),
        )


# --- production summarizer -------------------------------------------------


_L1_SYSTEM_PROMPT = """You are the **Compactor**. You distill an excerpt of a chat thread into a tight Markdown summary that preserves the parts a future agent will need to keep a coherent conversation: who asked what, what was answered, decisions made, deliverables produced, and any open threads. Lose pleasantries, repetition, and filler.

Write 3-8 short bullets. Be concrete. Lead each bullet with the most-load-bearing noun. Don't hedge."""


_L2_SYSTEM_PROMPT = """You are the **Compactor**, folding several mid-level summaries of the same chat thread into a single higher-level summary. Preserve the load-bearing facts: persistent context about who this user is and what they've been working on, plus a chronological skeleton of the major turns.

Write 4-10 short bullets. Be concrete. The reader is a future agent picking up the thread cold."""


async def _haiku_summarizer(items: list[dict], user_id: UUID) -> str:
    """Production summarizer — Haiku call routed through the existing
    metering wrapper so cost lands in ``token_usage`` attributed to the
    thread's owner.

    Tests inject a fake via :func:`set_summarizer_for_test` and never
    hit this path. We import lazily so a missing Anthropic key in unit
    tests doesn't blow up the module load."""
    from wolfpaw.metering.model_client import get_model_client

    if not items:
        return ""

    is_l2 = items[0].get("kind") == "level_1_summary"
    system_prompt = _L2_SYSTEM_PROMPT if is_l2 else _L1_SYSTEM_PROMPT
    user_payload = _format_items_for_prompt(items, is_l2=is_l2)

    settings = get_settings()
    client = get_model_client()
    result = await client.call(
        user_id=user_id,
        agent="compactor",
        model=settings.model_triage,  # Haiku
        messages=[{"role": "user", "content": user_payload}],
        system=system_prompt,
        max_tokens=600,
    )
    return result.text.strip()


def _format_items_for_prompt(items: list[dict], *, is_l2: bool) -> str:
    """Render the messages-or-summaries window into a plain-text block
    Haiku can read without confusion. We don't ship the structure as
    JSON because Haiku tends to mirror JSON back as JSON, and we want
    Markdown out."""
    if is_l2:
        lines = ["# Previous summaries (chronological)\n"]
        for it in items:
            lines.append("---")
            lines.append(it["summary_md"])
        lines.append("---")
        lines.append("\nFold the above into one higher-level summary.")
        return "\n".join(lines)

    lines = ["# Conversation excerpt (chronological)\n"]
    for it in items:
        role = it["role"]
        content = it["content"]
        lines.append(f"**{role}** ({it['created_at']}):")
        lines.append(content)
        lines.append("")
    lines.append("Summarize the above excerpt.")
    return "\n".join(lines)
