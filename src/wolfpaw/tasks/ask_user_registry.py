"""In-process registry for pending `ask_user` questions.

When the `ask_user` tool runs, it:
    1. Registers a `PendingQuestion` with a fresh question_id
    2. Awaits the question's `answer_future`
    3. Returns the resolved answer to the executor

The reply path (web channel POST /channels/web/answer, or a future
Telegram channel detecting next-message-as-answer) calls `submit_answer`
to resolve the future.

In-process means: if the runtime restarts mid-task, pending questions
are lost — the task transitions to `failed` on next start. This is
acceptable for v1 since execution is synchronous within one request.
When the arq worker lands, a long-lived worker process holds the
registry; reply endpoints stay in the API process and signal via a small
DB-backed table (deferred).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from wolfpaw.tracing import get_logger

log = get_logger()


class AlreadyAnswered(Exception):
    """submit_answer called on a question that was already resolved."""


class UnknownQuestion(Exception):
    """submit_answer called with a question_id the registry doesn't have."""


@dataclass
class PendingQuestion:
    id: UUID
    user_id: UUID
    task_id: UUID
    thread_id: UUID | None
    question: str
    options: list[str] | None
    urgency: str
    answer_future: asyncio.Future[str] = field(repr=False)


class AskUserRegistry:
    """Process-wide registry of pending questions."""

    def __init__(self) -> None:
        self._pending: dict[UUID, PendingQuestion] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        thread_id: UUID | None,
        question: str,
        options: list[str] | None = None,
        urgency: str = "normal",
    ) -> PendingQuestion:
        loop = asyncio.get_running_loop()
        pq = PendingQuestion(
            id=uuid4(),
            user_id=user_id,
            task_id=task_id,
            thread_id=thread_id,
            question=question,
            options=list(options) if options else None,
            urgency=urgency,
            answer_future=loop.create_future(),
        )
        async with self._lock:
            self._pending[pq.id] = pq
        log.info(
            "tasks.ask_user.registered",
            question_id=str(pq.id),
            task_id=str(task_id),
            user_id=str(user_id),
        )
        return pq

    async def submit_answer(
        self, *, question_id: UUID, user_id: UUID, answer: str,
    ) -> PendingQuestion:
        """Resolve the future for `question_id`. Scoped to `user_id` so
        a stray answer from another user can't satisfy a pending question."""
        async with self._lock:
            pq = self._pending.get(question_id)
            if pq is None:
                raise UnknownQuestion(str(question_id))
            if pq.user_id != user_id:
                raise UnknownQuestion(str(question_id))
            if pq.answer_future.done():
                raise AlreadyAnswered(str(question_id))
            pq.answer_future.set_result(answer)
            self._pending.pop(question_id, None)
        log.info(
            "tasks.ask_user.answered",
            question_id=str(question_id),
            task_id=str(pq.task_id),
        )
        return pq

    async def cancel(self, question_id: UUID, reason: str = "cancelled") -> None:
        """Reject the future so the awaiting `ask_user` raises. Used when
        a task gets cancelled while a question is pending."""
        async with self._lock:
            pq = self._pending.pop(question_id, None)
        if pq is None:
            return
        if not pq.answer_future.done():
            pq.answer_future.set_exception(RuntimeError(reason))

    def has_pending(self, question_id: UUID) -> bool:
        return question_id in self._pending


_registry: AskUserRegistry | None = None


def get_registry() -> AskUserRegistry:
    global _registry
    if _registry is None:
        _registry = AskUserRegistry()
    return _registry


def reset_registry() -> None:
    """Test/dev hook — drop the cached registry (does NOT resolve pending
    futures; tests should cancel explicitly if they need that)."""
    global _registry
    _registry = None
