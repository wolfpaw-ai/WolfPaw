"""Compact a thread's older messages into tiered summaries.

Three folds per invocation, all bounded by config knobs:

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
- **Level 3.** When at least ``l3_fold_threshold`` (default 10)
  un-folded L2 summaries exist, fold them — together with the thread's
  current L3 — into a **single** L3 digest that is rewritten in place
  (never accumulated). This bounds the summary layer to O(1) in the
  prompt no matter how long the conversation runs: the digest is a
  fixed-budget rolling memory of the distant past, lossy by design.
  Folded L2 rows get their ``folded_into_summary_id`` stamped so they
  drop out of :func:`conv.fetch_summaries` in favor of the L3.

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
            await _drain_l3(
                conn,
                thread_id=thread_id,
                user_id=user_id,
                fold_threshold=settings.l3_fold_threshold,
                char_cap=settings.l3_char_cap,
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


# --- level 3 (single rewritten-in-place digest) ----------------------------


async def _drain_l3(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    user_id: UUID,
    fold_threshold: int,
    char_cap: int,
    summarizer: Summarizer,
) -> None:
    """Fold the oldest ``fold_threshold`` un-folded L2s into the thread's
    **single** L3 digest, rewriting it in place so the summary layer never
    grows unbounded. Unlike L1/L2, this never inserts more than one row
    per thread: the existing L3 (if any) is fed back to the summarizer
    alongside the new L2s and the result overwrites it. Repeats until
    fewer than ``fold_threshold`` un-folded L2s remain.

    The digest is lossy by design — as new material folds in, the model
    is asked to keep it within budget, so the oldest detail compresses
    away. ``char_cap`` is a hard truncation backstop in case the model
    overruns its token limit; the digest lands in every prompt, so its
    size must be bounded regardless of model behavior."""
    while True:
        l2s = await conn.fetch(
            """
            SELECT id, summary_md,
                   range_start_message_id, range_end_message_id, created_at
              FROM thread_summaries
             WHERE thread_id = $1 AND level = 2
               AND folded_into_summary_id IS NULL
             ORDER BY created_at ASC, id ASC
             LIMIT $2
            """,
            thread_id, fold_threshold,
        )
        if len(l2s) < fold_threshold:
            return

        # At most one L3 per thread (the invariant this function upholds).
        l3 = await conn.fetchrow(
            "SELECT id, summary_md, range_start_message_id"
            "  FROM thread_summaries WHERE thread_id = $1 AND level = 3",
            thread_id,
        )

        items: list[dict] = []
        if l3 is not None:
            items.append(
                {"kind": "level_3_digest", "summary_md": l3["summary_md"]}
            )
        items.extend(
            {
                "kind": "level_2_summary",
                "summary_md": r["summary_md"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in l2s
        )

        digest_md = await summarizer(items, user_id)
        if not digest_md:
            log.warning(
                "workers.compact_thread.l3.empty_summary",
                thread_id=str(thread_id),
            )
            return
        if len(digest_md) > char_cap:
            digest_md = digest_md[:char_cap].rstrip() + "\n\n…[digest truncated]"

        # New range spans from the digest's existing start (or the oldest
        # L2 in this batch, on first creation) through the newest L2.
        new_range_start = (
            l3["range_start_message_id"] if l3 is not None
            else l2s[0]["range_start_message_id"]
        )
        new_range_end = l2s[-1]["range_end_message_id"]

        if l3 is not None:
            l3_id = l3["id"]
            await conn.execute(
                "UPDATE thread_summaries"
                "   SET summary_md = $1,"
                "       range_start_message_id = $2,"
                "       range_end_message_id = $3,"
                "       created_at = NOW()"
                " WHERE id = $4",
                digest_md, new_range_start, new_range_end, l3_id,
            )
        else:
            l3_id = await conn.fetchval(
                """
                INSERT INTO thread_summaries
                    (thread_id, level, summary_md,
                     range_start_message_id, range_end_message_id)
                VALUES ($1, 3, $2, $3, $4)
                RETURNING id
                """,
                thread_id, digest_md, new_range_start, new_range_end,
            )

        await conn.execute(
            "UPDATE thread_summaries SET folded_into_summary_id = $1"
            " WHERE id = ANY($2::uuid[])",
            l3_id, [r["id"] for r in l2s],
        )
        log.info(
            "workers.compact_thread.l3.updated",
            thread_id=str(thread_id),
            l2_count=len(l2s),
            rewritten=l3 is not None,
        )


# --- arq job wrapper -------------------------------------------------------


async def compact_thread_job(_ctx: dict, thread_id_str: str) -> None:
    """arq-shaped wrapper around :func:`compact_thread`. arq passes the
    job context dict as the first positional argument and serializes
    UUIDs as strings on the wire."""
    await compact_thread(UUID(thread_id_str))


async def embed_message_job(
    _ctx: dict, _thread_id_str: str, message_id_str: str, content: str,
) -> None:
    """arq-shaped wrapper around
    :func:`wolfpaw.memory.conversational.embed_and_store`. The
    ``thread_id`` is included on the wire for log + dashboard
    correlation but isn't used by the embedder itself."""
    from wolfpaw.memory.conversational import embed_and_store

    await embed_and_store(UUID(message_id_str), content)


async def embed_workspace_file_job(
    _ctx: dict, file_id_str: str, text: str,
) -> None:
    """arq-shaped wrapper around
    :func:`wolfpaw.workspace.files.embed_and_store_doc`. Runs after a
    workspace file is written so `search_docs` can find it."""
    from wolfpaw.workspace.files import embed_and_store_doc

    await embed_and_store_doc(UUID(file_id_str), text)


# --- production summarizer -------------------------------------------------


_L1_SYSTEM_PROMPT = """You are the **Compactor**. You distill an excerpt of a chat thread into a tight Markdown summary that preserves the parts a future agent will need to keep a coherent conversation: who asked what, what was answered, decisions made, deliverables produced, and any open threads. Lose pleasantries, repetition, and filler.

Write 3-8 short bullets. Be concrete. Lead each bullet with the most-load-bearing noun. Don't hedge."""


_L2_SYSTEM_PROMPT = """You are the **Compactor**, folding several mid-level summaries of the same chat thread into a single higher-level summary. Preserve the load-bearing facts: persistent context about who this user is and what they've been working on, plus a chronological skeleton of the major turns.

Write 4-10 short bullets. Be concrete. The reader is a future agent picking up the thread cold."""


_L3_SYSTEM_PROMPT = """You are the **Compactor** maintaining the long-term memory digest of a very long-running chat thread. You are given the CURRENT digest (if one exists) followed by newer mid-level summaries that now need to be absorbed into it. Rewrite the whole thing into a SINGLE consolidated digest.

This digest is fixed-budget: it lands in every future prompt, so it must not grow. Keep the most durable, load-bearing facts — who this user is, standing preferences and context, long-running projects, major decisions and outcomes — and let older, less-important specifics compress away or drop entirely. Recent material deserves more detail than distant material. It is expected and acceptable that the digest becomes vaguer about the distant past over time.

Write tight Markdown, at most ~12 short bullets, grouped sensibly (e.g. "About the user", "Ongoing work", "History"). Be concrete. The reader is a future agent picking up the thread cold. Return ONLY the rewritten digest."""


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

    # Route by the kind of the items being folded: L2 summaries or an L3
    # digest → L3 rewrite; L1 summaries → L2 fold; raw messages → L1.
    kinds = {it.get("kind") for it in items}
    if "level_2_summary" in kinds or "level_3_digest" in kinds:
        mode = "l3"
    elif "level_1_summary" in kinds:
        mode = "l2"
    else:
        mode = "l1"

    settings = get_settings()
    system_prompt = {
        "l1": _L1_SYSTEM_PROMPT,
        "l2": _L2_SYSTEM_PROMPT,
        "l3": _L3_SYSTEM_PROMPT,
    }[mode]
    max_tokens = settings.l3_max_tokens if mode == "l3" else 600
    user_payload = _format_items_for_prompt(items, mode=mode)

    client = get_model_client()
    result = await client.call(
        user_id=user_id,
        agent="compactor",
        model=settings.model_triage,  # Haiku
        messages=[{"role": "user", "content": user_payload}],
        system=system_prompt,
        max_tokens=max_tokens,
    )
    return result.text.strip()


def _format_items_for_prompt(items: list[dict], *, mode: str) -> str:
    """Render the messages-or-summaries window into a plain-text block
    Haiku can read without confusion. We don't ship the structure as
    JSON because Haiku tends to mirror JSON back as JSON, and we want
    Markdown out."""
    if mode == "l3":
        # First item may be the current digest; the rest are new L2s to
        # absorb. Label them so the model knows what to preserve vs fold.
        lines: list[str] = []
        rest = items
        if items and items[0].get("kind") == "level_3_digest":
            lines.append("# Current long-term digest\n")
            lines.append(items[0]["summary_md"])
            lines.append("")
            rest = items[1:]
        else:
            lines.append("# Current long-term digest\n")
            lines.append("(none yet — this is the first digest)")
            lines.append("")
        lines.append("# New summaries to absorb (chronological)\n")
        for it in rest:
            lines.append("---")
            lines.append(it["summary_md"])
        lines.append("---")
        lines.append(
            "\nRewrite the current digest and the new summaries into one"
            " consolidated fixed-budget digest."
        )
        return "\n".join(lines)

    if mode == "l2":
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
