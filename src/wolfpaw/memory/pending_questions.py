"""DAO over `pending_questions` — the durable store for `ask_user` pauses.

The whole point of this table (vs the old in-process registry) is that the
ask and the answer can happen in different processes: `ask_user` runs in the
task's process (inline web request or arq worker) and polls `get`, while the
answer arrives via a completely separate request (web POST /answer, or the
next inbound Telegram message) that calls `mark_answered`. They only ever
meet at the row.

`mark_answered` is scoped to `user_id` so one user can't resolve another's
question, and is a no-op-safe compare-and-set on `status = 'pending'` so a
double-submit (two Telegram messages, a retry) can be told apart from a first
answer via the returned outcome.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

import asyncpg

from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()

# Postgres NOTIFY channel a resolved question fires on, so a waiting
# `ask_user` (possibly in another process) wakes immediately instead of
# polling. Payload is the question_id as text.
_NOTIFY_CHANNEL = "pending_answered"

# Even with NOTIFY, re-read the row this often as a backstop: a dropped
# connection or a notification that fired in the gap before we started
# listening must not hang the task until the full timeout.
_BACKSTOP_SECONDS = 15.0


@dataclass(frozen=True)
class PendingQuestion:
    id: UUID
    task_id: UUID
    user_id: UUID
    thread_id: UUID | None
    channel: str | None
    question: str
    options: list[str]
    urgency: str
    status: str
    answer: str | None
    created_at: datetime
    answered_at: datetime | None
    expires_at: datetime | None = None


class AnswerOutcome(str, Enum):
    """Result of `mark_answered`, so callers can respond precisely."""

    ANSWERED = "answered"      # was pending, now resolved
    UNKNOWN = "unknown"        # no such row for this (id, user)
    ALREADY = "already"        # row exists but already left 'pending'


def _row_to_question(row: asyncpg.Record) -> PendingQuestion:
    options_raw = row["options"]
    if isinstance(options_raw, str):
        options_raw = json.loads(options_raw)
    return PendingQuestion(
        id=row["id"],
        task_id=row["task_id"],
        user_id=row["user_id"],
        thread_id=row["thread_id"],
        channel=row["channel"],
        question=row["question"],
        options=list(options_raw or []),
        urgency=row["urgency"],
        status=row["status"],
        answer=row["answer"],
        created_at=row["created_at"],
        answered_at=row["answered_at"],
        expires_at=row.get("expires_at"),
    )


async def create(
    conn: asyncpg.Connection,
    *,
    task_id: UUID,
    user_id: UUID,
    question: str,
    thread_id: UUID | None = None,
    channel: str | None = None,
    options: list[str] | None = None,
    urgency: str = "normal",
    timeout_seconds: int = 300,
) -> PendingQuestion:
    """Insert a new pending question and return it.

    ``expires_at`` is stamped to now + ``timeout_seconds`` (the same window
    `ask_user` waits) so `get_open_for_user` stops matching this row once the
    wait is abandoned — a stranded 'pending' row must not swallow the user's
    later, unrelated messages."""
    row = await conn.fetchrow(
        """
        INSERT INTO pending_questions
            (task_id, user_id, thread_id, channel, question, options,
             urgency, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7,
                NOW() + ($8 * INTERVAL '1 second'))
        RETURNING *
        """,
        task_id, user_id, thread_id, channel, question,
        json.dumps(list(options) if options else []), urgency,
        max(1, int(timeout_seconds)),
    )
    return _row_to_question(row)


async def get(
    conn: asyncpg.Connection, *, question_id: UUID,
) -> PendingQuestion | None:
    """Fetch one question by id, or None."""
    row = await conn.fetchrow(
        "SELECT * FROM pending_questions WHERE id = $1", question_id,
    )
    return _row_to_question(row) if row is not None else None


async def get_open_for_user(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> PendingQuestion | None:
    """Return the user's oldest still-open question, or None. Used by the
    Telegram inbound path to decide whether the next message is an answer.

    "Open" means `pending` AND not past its `expires_at` — an abandoned wait
    (worker died before the timeout handler ran) leaves a stranded 'pending'
    row that must NOT capture unrelated later messages. NULL `expires_at`
    (legacy rows written before the column existed) never matches, which is
    the safe default: better to route the message as a fresh turn than to
    swallow it into a dead question."""
    row = await conn.fetchrow(
        """
        SELECT * FROM pending_questions
         WHERE user_id = $1 AND status = 'pending'
           AND expires_at > NOW()
         ORDER BY expires_at
         LIMIT 1
        """,
        user_id,
    )
    return _row_to_question(row) if row is not None else None


async def mark_answered(
    conn: asyncpg.Connection, *, question_id: UUID, user_id: UUID, answer: str,
) -> AnswerOutcome:
    """Compare-and-set the row to 'answered', scoped to `user_id`. Returns
    ANSWERED on success, ALREADY if it had already left 'pending', UNKNOWN if
    no such row belongs to this user."""
    updated = await conn.fetchval(
        """
        UPDATE pending_questions
           SET status = 'answered', answer = $3, answered_at = NOW()
         WHERE id = $1 AND user_id = $2 AND status = 'pending'
        RETURNING id
        """,
        question_id, user_id, answer,
    )
    if updated is not None:
        await _notify_resolved(conn, question_id)
        return AnswerOutcome.ANSWERED
    exists = await conn.fetchval(
        "SELECT 1 FROM pending_questions WHERE id = $1 AND user_id = $2",
        question_id, user_id,
    )
    return AnswerOutcome.ALREADY if exists else AnswerOutcome.UNKNOWN


async def mark_timeout(
    conn: asyncpg.Connection, *, question_id: UUID,
) -> None:
    """Flip a still-pending question to 'timeout'. No-op if already resolved."""
    await conn.execute(
        """
        UPDATE pending_questions
           SET status = 'timeout', answered_at = NOW()
         WHERE id = $1 AND status = 'pending'
        """,
        question_id,
    )


async def mark_cancelled(
    conn: asyncpg.Connection, *, question_id: UUID,
) -> None:
    """Flip a still-pending question to 'cancelled'. No-op if already resolved."""
    await conn.execute(
        """
        UPDATE pending_questions
           SET status = 'cancelled', answered_at = NOW()
         WHERE id = $1 AND status = 'pending'
        """,
        question_id,
    )
    # Wake any waiter so it stops blocking and observes the cancellation.
    await _notify_resolved(conn, question_id)


async def _notify_resolved(conn: asyncpg.Connection, question_id: UUID) -> None:
    """Fire the NOTIFY that wakes a waiting `ask_user`. Best-effort: a missed
    signal is caught by the waiter's backstop re-read."""
    try:
        await conn.execute(
            "SELECT pg_notify($1, $2)", _NOTIFY_CHANNEL, str(question_id),
        )
    except Exception:  # noqa: BLE001
        log.warning("pending_questions.notify_failed", exc_info=True)


async def wait_for_answer(
    *, question_id: UUID, timeout: float,
) -> PendingQuestion | None:
    """Block until `question_id` leaves 'pending' (answered/cancelled/timeout)
    or `timeout` seconds elapse. Event-driven via Postgres LISTEN/NOTIFY on a
    dedicated connection, so a resolution in any process wakes us at once; a
    periodic backstop re-read guards against a missed notification.

    Returns the resolved PendingQuestion, or None on timeout / if it vanished.
    Does NOT itself mark timeout — the caller owns that transition.
    """
    loop = asyncio.get_running_loop()
    async with acquire() as conn:
        woke = asyncio.Event()

        def _on_notify(_conn, _pid, _channel, payload) -> None:
            # asyncpg dispatches listener callbacks on the event loop thread,
            # so setting the Event directly is safe.
            if payload == str(question_id):
                woke.set()

        await conn.add_listener(_NOTIFY_CHANNEL, _on_notify)
        try:
            deadline = time.monotonic() + timeout
            while True:
                # Check first: covers a NOTIFY that fired before add_listener
                # and the initial already-resolved case.
                current = await get(conn, question_id=question_id)
                if current is None or current.status != "pending":
                    return current
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                woke.clear()
                try:
                    await asyncio.wait_for(
                        woke.wait(), timeout=min(_BACKSTOP_SECONDS, remaining),
                    )
                except asyncio.TimeoutError:
                    pass  # backstop tick — loop re-reads the row
        finally:
            try:
                await conn.remove_listener(_NOTIFY_CHANNEL, _on_notify)
            except Exception:  # noqa: BLE001 — connection may be going away
                pass
