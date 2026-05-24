"""Unit tests for `tasks.ask_user_registry`. The registry is process-local
so all tests here run without a DB."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from wolfpaw.tasks.ask_user_registry import (
    AlreadyAnswered,
    AskUserRegistry,
    UnknownQuestion,
)


async def test_register_returns_pending_question_with_id():
    r = AskUserRegistry()
    pq = await r.register(
        user_id=uuid4(), task_id=uuid4(), thread_id=None,
        question="approve overwrite?",
    )
    assert pq.id is not None
    assert pq.question == "approve overwrite?"
    assert r.has_pending(pq.id)
    assert not pq.answer_future.done()


async def test_submit_answer_resolves_future():
    r = AskUserRegistry()
    uid = uuid4()
    pq = await r.register(
        user_id=uid, task_id=uuid4(), thread_id=None,
        question="?",
    )

    async def awaiter():
        return await pq.answer_future

    awaiter_task = asyncio.create_task(awaiter())
    await asyncio.sleep(0)  # let the awaiter start
    await r.submit_answer(question_id=pq.id, user_id=uid, answer="yes")
    answer = await awaiter_task
    assert answer == "yes"
    assert not r.has_pending(pq.id)  # popped after answer


async def test_submit_answer_rejects_unknown_id():
    r = AskUserRegistry()
    with pytest.raises(UnknownQuestion):
        await r.submit_answer(
            question_id=uuid4(), user_id=uuid4(), answer="x",
        )


async def test_submit_answer_rejects_cross_user_attempt():
    """User B can't answer User A's pending question."""
    r = AskUserRegistry()
    a = uuid4()
    b = uuid4()
    pq = await r.register(
        user_id=a, task_id=uuid4(), thread_id=None, question="?",
    )
    with pytest.raises(UnknownQuestion):
        await r.submit_answer(question_id=pq.id, user_id=b, answer="x")
    # A's future is still pending.
    assert not pq.answer_future.done()


async def test_double_submit_rejected_as_already_answered():
    r = AskUserRegistry()
    uid = uuid4()
    pq = await r.register(
        user_id=uid, task_id=uuid4(), thread_id=None, question="?",
    )
    await r.submit_answer(question_id=pq.id, user_id=uid, answer="first")
    # Second attempt finds the entry gone — surfaces as UnknownQuestion.
    with pytest.raises(UnknownQuestion):
        await r.submit_answer(question_id=pq.id, user_id=uid, answer="second")


async def test_cancel_raises_exception_in_awaiter():
    r = AskUserRegistry()
    pq = await r.register(
        user_id=uuid4(), task_id=uuid4(), thread_id=None, question="?",
    )

    async def awaiter():
        return await pq.answer_future

    awaiter_task = asyncio.create_task(awaiter())
    await asyncio.sleep(0)
    await r.cancel(pq.id, reason="task cancelled")
    with pytest.raises(RuntimeError, match="task cancelled"):
        await awaiter_task
