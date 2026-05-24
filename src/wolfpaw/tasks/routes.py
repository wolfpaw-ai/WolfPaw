"""JSON HTTP API for tasks. Companion to the slash commands in
`tasks.commands` — the React app uses these directly rather than parsing
slash-command text.

    GET   /tasks                        — list user's recent tasks
    GET   /tasks/{id}                   — task + its events
    POST  /tasks/{id}/cancel            — cancel (idempotent + user-scoped)

All endpoints are user-scoped: the route always passes `user_id` from
the session and the DAO methods reject cross-user reads/cancels.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory import task_events, tasks as tasks_dao
from wolfpaw.memory.db import acquire

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskResponse(BaseModel):
    id: str
    title: str
    description: str | None
    status: str
    current_plan_id: str | None
    spent_cents: int
    budget_cents: int | None
    blocking_reason: str | None
    parent_task_id: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    last_active_at: str | None

    @classmethod
    def from_dao(cls, t: tasks_dao.Task) -> "TaskResponse":
        return cls(
            id=str(t.id),
            title=t.title,
            description=t.description,
            status=t.status,
            current_plan_id=str(t.current_plan_id) if t.current_plan_id else None,
            spent_cents=t.spent_cents,
            budget_cents=t.budget_cents,
            blocking_reason=t.blocking_reason,
            parent_task_id=(
                str(t.parent_task_id) if t.parent_task_id else None
            ),
            created_at=t.created_at.isoformat() if t.created_at else "",
            started_at=t.started_at.isoformat() if t.started_at else None,
            completed_at=t.completed_at.isoformat() if t.completed_at else None,
            last_active_at=(
                t.last_active_at.isoformat() if t.last_active_at else None
            ),
        )


class TaskEventResponse(BaseModel):
    id: str
    event_type: str
    content: dict[str, Any]
    created_at: str

    @classmethod
    def from_dao(cls, e: task_events.TaskEvent) -> "TaskEventResponse":
        return cls(
            id=str(e.id),
            event_type=e.event_type,
            content=dict(e.content),
            created_at=e.created_at.isoformat() if e.created_at else "",
        )


class TaskDetailResponse(BaseModel):
    task: TaskResponse
    events: list[TaskEventResponse]


@router.get("")
async def list_tasks(
    user_id: UUID = Depends(require_user_id),
    limit: int = 50,
) -> dict[str, list[TaskResponse]]:
    limit = max(1, min(200, limit))
    async with acquire() as conn:
        rows = await tasks_dao.list_for_user(
            conn, user_id=user_id, limit=limit,
        )
    return {"tasks": [TaskResponse.from_dao(t) for t in rows]}


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: UUID,
    user_id: UUID = Depends(require_user_id),
) -> TaskDetailResponse:
    async with acquire() as conn:
        task = await tasks_dao.get_by_id(
            conn, user_id=user_id, task_id=task_id,
        )
        if task is None:
            raise HTTPException(404, "task not found")
        events = await task_events.fetch_for_task(conn, task_id=task_id)
    return TaskDetailResponse(
        task=TaskResponse.from_dao(task),
        events=[TaskEventResponse.from_dao(e) for e in events],
    )


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: UUID,
    user_id: UUID = Depends(require_user_id),
) -> TaskResponse:
    async with acquire() as conn:
        cancelled = await tasks_dao.cancel(
            conn, user_id=user_id, task_id=task_id,
        )
        if cancelled is not None:
            await task_events.append_event(
                conn, task_id=task_id, event_type="status.cancelled",
                content={"by": "user"},
            )
            # Lock in the cost the task already accrued before cancellation
            # so the listing reflects partial spend rather than 0.
            await tasks_dao.rollup_spent_cents(conn, task_id=task_id)
            cancelled = await tasks_dao.get_by_id(
                conn, user_id=user_id, task_id=task_id,
            )
    if cancelled is None:
        # Either doesn't exist (for this user) or is already terminal.
        async with acquire() as conn:
            existing = await tasks_dao.get_by_id(
                conn, user_id=user_id, task_id=task_id,
            )
        if existing is None:
            raise HTTPException(404, "task not found")
        # Already terminal — 409 so the client knows it's a no-op.
        raise HTTPException(409, f"task is already {existing.status}")
    return TaskResponse.from_dao(cancelled)
