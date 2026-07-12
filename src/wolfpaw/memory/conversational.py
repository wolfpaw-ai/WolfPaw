"""Conversational memory — `threads` + `messages` access.

Three tiers, all per-thread in v1:

- **Verbatim recent window.** The most recent ``recent_window_size``
  messages of a thread are loaded into every agent prompt via
  :func:`fetch_recent` (default 20).
- **Tiered summaries** (:func:`fetch_summaries`). Older messages get
  compacted into rolling level-1 summaries; once enough L1s accumulate
  they fold into a level-2 summary-of-summaries. The
  :mod:`wolfpaw.workers.jobs.compact_thread` job does the folding;
  ``fetch_summaries`` returns the highest level that covers each
  range (every L2 + every L1 not yet folded into one).
- **Per-thread vector recall** (:func:`search_relevant`). Every message
  gets embedded on append into ``message_embeddings``; the Planner does
  a top-k cosine lookup over that index alongside the verbatim window
  to surface relevant older context past the recent-window cap.

Embedding-on-append + compaction-trigger run as deferred jobs via
:mod:`wolfpaw.workers.queue` so a Voyage hiccup or a Haiku
summarization call never blocks the chat response path. The queue
abstraction picks between arq (when ``WOLFPAW_WORKERS_ENABLED=true``,
durable across process restart) and an in-process
``asyncio.create_task`` fallback (the single-process dev default).

Persistence policy: we store visible user turns and final assistant
responses only. Intermediate tool-call / tool-result blocks live
in-process during the agent loop and are not persisted to the messages
table. That keeps the recent-window small and useful as context for the
next turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

from wolfpaw.tracing import get_logger

MessageRole = Literal["user", "assistant", "system", "tool"]
ChannelName = Literal["web", "telegram", "email", "slack"]

log = get_logger()


@dataclass(frozen=True)
class Message:
    id: UUID
    thread_id: UUID
    role: MessageRole
    content: str
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class ThreadSummary:
    """A row from `thread_summaries`. Level 1 = window of verbatim
    messages; level 2 = fold of N level-1 summaries."""

    id: UUID
    thread_id: UUID
    level: int
    summary_md: str
    range_start_message_id: UUID | None
    range_end_message_id: UUID | None
    created_at: datetime


# Post-append (embed + compaction trigger) is on by default in app code
# and off in unit tests that exercise the DAO directly. Tests opt in via
# `enable_post_append_for_test()` when they want the full flow.
_post_append_enabled = True


def disable_post_append_for_test() -> None:
    """Suppress the embed + compact follow-ups inside `append`. Use in
    tests that exercise the DAO without a configured embedder or model
    client."""
    global _post_append_enabled
    _post_append_enabled = False


def enable_post_append_for_test() -> None:
    global _post_append_enabled
    _post_append_enabled = True


async def get_or_create_thread(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    channel: ChannelName,
    thread_id: UUID | None = None,
) -> UUID:
    """Return the existing thread (validated to belong to `user_id`) or
    create a fresh one for this channel.

    New threads are stamped with `soul_version` (the SHA-256 prefix of the
    currently-loaded Soul) and `user_profile_version` (the user's profile
    row's version counter). Both are nullable in the schema, so Soul
    unavailable or profile missing → NULL stamp, which downstream code
    treats as "unknown persona snapshot".
    """
    if thread_id is not None:
        row = await conn.fetchrow(
            "SELECT id FROM threads WHERE id = $1 AND user_id = $2",
            thread_id, user_id,
        )
        if row is not None:
            return row["id"]
        # Stale or cross-user thread_id — silently fall through and create
        # a fresh thread rather than erroring. Avoids exposing whether the
        # id exists for some other user.

    # Best-effort persona version stamps. Defensive: missing soul file /
    # missing profile row don't block thread creation.
    soul_version: str | None = None
    try:
        from wolfpaw.persona.soul import get_soul

        soul_version = get_soul().version
    except Exception:  # noqa: BLE001
        pass
    profile_version = await conn.fetchval(
        "SELECT version FROM user_profiles WHERE user_id = $1", user_id,
    )

    new_id = await conn.fetchval(
        "INSERT INTO threads"
        " (user_id, channel, soul_version, user_profile_version)"
        " VALUES ($1, $2::channel, $3, $4) RETURNING id",
        user_id, channel, soul_version, profile_version,
    )
    return new_id


async def append(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    role: MessageRole,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Insert one row into `messages` and (best-effort) defer the
    post-append follow-ups onto the workers queue: embed the content
    for vector recall, then check whether the thread needs compaction.

    The follow-ups go through :mod:`wolfpaw.workers.queue`. With
    ``WOLFPAW_WORKERS_ENABLED=true`` they run on the arq worker (so a
    Voyage hiccup or a Haiku summarization call never blocks the chat
    response path AND the work survives an app restart). With workers
    off they fall back to ``asyncio.create_task`` inside the caller's
    event loop — same liveness contract, no Redis required.

    Both follow-ups are suppressed for tool / system rows (structural
    payloads, not natural language to embed) and for empty content.
    """
    message_id = await conn.fetchval(
        "INSERT INTO messages (thread_id, role, content, metadata)"
        " VALUES ($1, $2::message_role, $3, $4) RETURNING id",
        thread_id, role, content, metadata or {},
    )

    if not _post_append_enabled:
        return message_id
    if role not in ("user", "assistant"):
        return message_id
    if not content or not content.strip():
        return message_id

    try:
        from wolfpaw.workers.queue import (
            enqueue_compact_thread,
            enqueue_embed_message,
        )

        await enqueue_embed_message(thread_id, message_id, content)
        await enqueue_compact_thread(thread_id)
    except Exception:  # noqa: BLE001 — best-effort; never block the write
        log.warning(
            "conv.append.post_enqueue_failed",
            thread_id=str(thread_id), message_id=str(message_id),
            exc_info=True,
        )
    return message_id


async def get_most_recent_thread(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
) -> UUID | None:
    """Return the user's most-recent thread across *all* channels, or
    None if they have no thread yet.

    Channel-agnostic by design: a user has one continuous conversation
    that follows them between web, Telegram, Slack, etc. When any client
    opens chat without a stored `thread_id` (new device, fresh browser,
    an inbound Telegram/Slack message), it resolves to the user's single
    most-recent thread regardless of which channel last touched it, so a
    conversation started in one channel continues in the next. The
    originating channel of each individual message is preserved in
    `messages.metadata.channel` for provenance. `/reset` is the explicit
    escape hatch — it always mints a new thread, which then becomes "most
    recent" for the next message.
    """
    return await conn.fetchval(
        "SELECT id FROM threads"
        " WHERE user_id = $1"
        " ORDER BY created_at DESC LIMIT 1",
        user_id,
    )


async def fetch_recent(
    conn: asyncpg.Connection, *, thread_id: UUID, n: int = 20
) -> list[Message]:
    """Return the most recent `n` messages in chronological order
    (oldest first), capped at `n`."""
    rows = await conn.fetch(
        "SELECT id, thread_id, role::text AS role, content, metadata, created_at"
        "  FROM messages WHERE thread_id = $1"
        " ORDER BY created_at DESC, id DESC LIMIT $2",
        thread_id, n,
    )
    out = [_row_to_message(r) for r in rows]
    out.reverse()
    return out


async def fetch_page(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    before_created_at: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = 30,
    roles: tuple[str, ...] | None = None,
) -> list[Message]:
    """Return up to `limit` messages from a thread in chronological order
    (oldest first), ending just before the `(before_created_at, before_id)`
    keyset cursor. With no cursor, returns the newest `limit` messages.

    Built for backwards infinite-scroll in the web UI: pass the oldest
    currently-shown message's `(created_at, id)` as the cursor to fetch the
    page immediately before it. Uses keyset (not OFFSET) pagination on the
    `(created_at, id)` order, so it's stable under concurrent inserts and
    served by the `messages_thread_idx` index. `roles` optionally restricts
    the result to given message roles — the chat UI passes
    `("user", "assistant")` to skip tool/system rows.
    """
    conds = ["thread_id = $1"]
    params: list[Any] = [thread_id]
    if roles is not None:
        params.append(list(roles))
        conds.append(f"role = ANY(${len(params)}::message_role[])")
    if before_created_at is not None and before_id is not None:
        params.append(before_created_at)
        params.append(before_id)
        conds.append(f"(created_at, id) < (${len(params) - 1}, ${len(params)})")
    params.append(limit)
    rows = await conn.fetch(
        "SELECT id, thread_id, role::text AS role, content, metadata, created_at"
        f"  FROM messages WHERE {' AND '.join(conds)}"
        f" ORDER BY created_at DESC, id DESC LIMIT ${len(params)}",
        *params,
    )
    out = [_row_to_message(r) for r in rows]
    out.reverse()
    return out


async def fetch_summaries(
    conn: asyncpg.Connection, *, thread_id: UUID
) -> list[ThreadSummary]:
    """Return the active summaries for this thread, oldest range first.

    "Active" means every summary that hasn't been folded into a
    higher-level one — i.e. ``folded_into_summary_id IS NULL``. That
    surfaces: the single L3 digest (the top of the ladder, never folded),
    every L2 not yet folded into the L3, and every L1 not yet folded into
    an L2. A reader sees each older message-range covered by at most one
    summary — the highest level available for that range.

    Returns an empty list when no compaction has happened yet (new
    thread, or thread under the trigger threshold).
    """
    rows = await conn.fetch(
        """
        SELECT id, thread_id, level, summary_md,
               range_start_message_id, range_end_message_id, created_at
          FROM thread_summaries
         WHERE thread_id = $1
           AND folded_into_summary_id IS NULL
         ORDER BY level DESC, created_at ASC
        """,
        thread_id,
    )
    return [_row_to_summary(r) for r in rows]


async def search_relevant(
    conn: asyncpg.Connection,
    *,
    thread_id: UUID,
    query_embedding: list[float],
    k: int = 15,
    exclude_recent_n: int = 20,
    recency_weight: float = 0.15,
    half_life_days: float = 30.0,
    window: int = 2,
    max_distance: float = 2.0,
) -> list[Message]:
    """Recency-weighted top-``k`` semantic recall from this thread,
    returned as small chronological windows around each hit.

    ``max_distance`` is an optional relevance floor: hits whose raw cosine
    distance to the query exceeds it are dropped *before* ranking, so a
    query with only a few genuinely-relevant messages doesn't dredge up
    ``k`` loosely-related ones. Cosine distance runs 0 (identical) → 2
    (opposite); the default 2.0 admits everything (top-k, the Planner's
    behavior). Callers that need precision (e.g. `delete_memories`) pass a
    tighter value.

    Hits are ranked by a blend of semantic distance and recency::

        score = (embedding <=> query)
                - recency_weight * 0.5 ^ (age_days / half_life_days)

    Lower score surfaces first. The recency term is bounded by
    ``recency_weight`` (its max, for a brand-new message) and decays with
    a half-life of ``half_life_days`` — so a recent-and-relevant message
    outranks an ancient-and-equally-relevant one without letting recency
    override a strong semantic match. ``half_life_days <= 0`` disables the
    bonus (pure cosine).

    For each of the top-``k`` hits we also pull the ``window`` messages on
    either side (by thread order) so recalled context reads as a coherent
    snippet rather than an isolated line; neighbors are included even if
    they were never embedded. The most recent ``exclude_recent_n`` messages
    are excluded — both as hits and as neighbors — since the verbatim
    window already covers them. Results come back in chronological order.

    Messages without embeddings are skipped as *hits* (the
    ``message_embeddings`` join) but can still appear as *neighbors*.

    Returns an empty list if no embeddings exist for this thread yet.
    """
    rows = await conn.fetch(
        """
        WITH ordered AS (
            SELECT m.id, m.thread_id, m.role, m.content, m.metadata,
                   m.created_at,
                   ROW_NUMBER() OVER (ORDER BY m.created_at ASC, m.id ASC) AS rn
              FROM messages m
             WHERE m.thread_id = $1
        ),
        recent AS (
            SELECT id
              FROM messages
             WHERE thread_id = $1
             ORDER BY created_at DESC, id DESC
             LIMIT $4
        ),
        hits AS (
            SELECT o.rn,
                   (e.embedding <=> $2)
                     - COALESCE(
                         $5 * POWER(
                             0.5,
                             EXTRACT(EPOCH FROM (NOW() - o.created_at))
                                 / 86400.0 / NULLIF($6, 0)
                         ),
                         0
                       ) AS score
              FROM ordered o
              JOIN message_embeddings e ON e.message_id = o.id
             WHERE o.id NOT IN (SELECT id FROM recent)
               AND (e.embedding <=> $2) <= $8
             ORDER BY score ASC
             LIMIT $3
        ),
        window_rns AS (
            SELECT DISTINCT o.rn
              FROM ordered o
              JOIN hits h ON o.rn BETWEEN h.rn - $7 AND h.rn + $7
        )
        SELECT o.id, o.thread_id, o.role::text AS role, o.content,
               o.metadata, o.created_at
          FROM ordered o
          JOIN window_rns w ON o.rn = w.rn
         WHERE o.id NOT IN (SELECT id FROM recent)
         ORDER BY o.created_at ASC, o.id ASC
        """,
        thread_id, query_embedding, k, exclude_recent_n,
        recency_weight, half_life_days, window, max_distance,
    )
    return [_row_to_message(r) for r in rows]


async def search_user_messages(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    query_embedding: list[float],
    k: int = 25,
    recency_weight: float = 0.0,
    half_life_days: float = 30.0,
    window: int = 0,
    max_distance: float = 2.0,
    exclude_recent_thread_id: UUID | None = None,
    exclude_recent_n: int = 0,
) -> list[Message]:
    """Like :func:`search_relevant` but scoped to the *whole user* — every
    thread they own, not one.

    A user's memory is one continuous thing (threads are just channel-
    agnostic containers, and a user may have several from legacy per-channel
    splits or `/reset`). Recall therefore spans all of them: it shouldn't go
    blind to a topic just because it was last discussed in a different
    thread. Neighbor windows and the recency term stay *within* each
    message's own thread (``PARTITION BY thread_id``). Results come back
    grouped by thread, chronological within.

    ``exclude_recent_thread_id`` + ``exclude_recent_n`` drop the newest
    ``exclude_recent_n`` messages of *that one thread* — used by the Planner
    to avoid duplicating the current thread's verbatim window (which it
    already loaded via :func:`fetch_recent`). Other threads are never
    excluded. Leave unset (the tools' deliberate mode) to search everything.
    """
    rows = await conn.fetch(
        """
        WITH ordered AS (
            SELECT m.id, m.thread_id, m.role, m.content, m.metadata,
                   m.created_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.thread_id
                       ORDER BY m.created_at ASC, m.id ASC
                   ) AS rn
              FROM messages m
              JOIN threads t ON t.id = m.thread_id
             WHERE t.user_id = $1
        ),
        recent AS (
            SELECT id FROM messages
             WHERE thread_id = $8
             ORDER BY created_at DESC, id DESC
             LIMIT $9
        ),
        hits AS (
            SELECT o.thread_id, o.rn,
                   (e.embedding <=> $2)
                     - COALESCE(
                         $4 * POWER(
                             0.5,
                             EXTRACT(EPOCH FROM (NOW() - o.created_at))
                                 / 86400.0 / NULLIF($5, 0)
                         ),
                         0
                       ) AS score
              FROM ordered o
              JOIN message_embeddings e ON e.message_id = o.id
             WHERE (e.embedding <=> $2) <= $6
               AND o.id NOT IN (SELECT id FROM recent)
             ORDER BY score ASC
             LIMIT $3
        ),
        window_rns AS (
            SELECT DISTINCT o.thread_id, o.rn
              FROM ordered o
              JOIN hits h
                ON o.thread_id = h.thread_id
               AND o.rn BETWEEN h.rn - $7 AND h.rn + $7
        )
        SELECT o.id, o.thread_id, o.role::text AS role, o.content,
               o.metadata, o.created_at
          FROM ordered o
          JOIN window_rns w
            ON o.thread_id = w.thread_id AND o.rn = w.rn
         WHERE o.id NOT IN (SELECT id FROM recent)
         ORDER BY o.thread_id, o.created_at ASC, o.id ASC
        """,
        user_id, query_embedding, k,
        recency_weight, half_life_days, max_distance, window,
        exclude_recent_thread_id, max(0, exclude_recent_n),
    )
    return [_row_to_message(r) for r in rows]


# --- post-append follow-ups -------------------------------------------------
#
# `append` delegates these to wolfpaw.workers.queue, which runs them on
# arq when WOLFPAW_WORKERS_ENABLED=true and inline via asyncio.create_task
# otherwise. The two helpers below are the actual unit-of-work; the
# queue layer wraps them.


async def embed_and_store(message_id: UUID, content: str) -> None:
    """Compute the Voyage embedding for ``content`` and insert it into
    ``message_embeddings``. Idempotent: if the message already has an
    embedding row (the worker also re-embeds on backfill), we skip.

    Called by :mod:`wolfpaw.workers.queue` (and indirectly by
    :func:`append` through the queue layer) — not invoked directly from
    `append` so the queue layer can route execution to arq or to the
    inline fallback depending on ``WOLFPAW_WORKERS_ENABLED``."""
    from wolfpaw.embeddings import get_embedder
    from wolfpaw.memory.db import acquire
    from wolfpaw.metering.recorder import record_usage
    from wolfpaw.metering.types import TokenCounts

    embedder = get_embedder()
    result = await embedder.embed_one(content)
    if not result.vectors:
        return
    vector = result.vectors[0]
    async with acquire() as conn:
        await conn.execute(
            "INSERT INTO message_embeddings (message_id, embedding)"
            " VALUES ($1, $2)"
            " ON CONFLICT (message_id) DO NOTHING",
            message_id, vector,
        )
    # Best-effort cost attribution so embeddings show up in /usage. The
    # embedder needs a user_id to record against — we don't have one here
    # without an extra round-trip. Resolve via the message's thread→user.
    if result.input_tokens <= 0:
        return
    try:
        async with acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT t.user_id FROM messages m"
                " JOIN threads t ON t.id = m.thread_id"
                " WHERE m.id = $1",
                message_id,
            )
        if user_id is None:
            return
        await record_usage(
            user_id=user_id,
            agent="compactor",
            model=result.model,
            usage=TokenCounts(input_tokens=result.input_tokens),
            cost_cents=_voyage_cents(result.input_tokens),
        )
    except Exception:  # noqa: BLE001 — metering must not block the path
        log.warning("conv.embed.record_failed", exc_info=True)


_VOYAGE_MICROCENTS_PER_MTOK = 6  # matches the seeded model_prices row


def _voyage_cents(input_tokens: int) -> int:
    from math import ceil

    return max(0, ceil((input_tokens * _VOYAGE_MICROCENTS_PER_MTOK) / 1_000_000))


# --- prompt-context block formatters --------------------------------------


def format_summaries_block(summaries: list[ThreadSummary]) -> str:
    """Render summaries as a Markdown block for inclusion in an agent's
    system prompt. Returns the empty string when no summaries exist so
    the caller can ``+ block`` unconditionally."""
    if not summaries:
        return ""
    lines = [
        "## Earlier in this thread (compacted summaries, oldest first)",
        "",
    ]
    for s in summaries:
        label = "Older summary" if s.level == 2 else "Earlier window"
        lines.append(f"### {label}")
        lines.append(s.summary_md.strip())
        lines.append("")
    return "\n".join(lines).rstrip()


def format_vector_recall_block(messages: list[Message]) -> str:
    """Render semantically-relevant older messages as a Markdown block.
    Returns the empty string when the list is empty."""
    if not messages:
        return ""
    lines = [
        "## Possibly-relevant older messages from this thread",
        "(retrieved by semantic similarity to the current request)",
        "",
    ]
    for m in messages:
        snippet = m.content.strip()
        if len(snippet) > 400:
            snippet = snippet[:397].rstrip() + "..."
        lines.append(f"- **{m.role}** ({m.created_at.isoformat()}): {snippet}")
    return "\n".join(lines)


# --- helpers ---------------------------------------------------------------


def _row_to_message(r: asyncpg.Record) -> Message:
    return Message(
        id=r["id"],
        thread_id=r["thread_id"],
        role=r["role"],
        content=r["content"],
        metadata=dict(r["metadata"] or {}),
        created_at=r["created_at"],
    )


def _row_to_summary(r: asyncpg.Record) -> ThreadSummary:
    return ThreadSummary(
        id=r["id"],
        thread_id=r["thread_id"],
        level=int(r["level"]),
        summary_md=r["summary_md"],
        range_start_message_id=r["range_start_message_id"],
        range_end_message_id=r["range_end_message_id"],
        created_at=r["created_at"],
    )
