"""arq job: drive a queued Task end-to-end on a worker process.

Paired with :func:`wolfpaw.workers.queue.enqueue_run_task`. The Router
enqueues this job for top-level tasks (workers enabled); the worker
pulls it off Redis and calls :meth:`TaskService.run` against the
existing task row.

Subagent tasks bypass the queue — the parent agent waits for the
child's output, so the subagent path uses the synchronous
``create_and_run`` directly.
"""

from __future__ import annotations

from uuid import UUID

from wolfpaw.tasks.service import get_task_service


async def run_task_job(_ctx: dict, task_id_str: str) -> None:
    await get_task_service().run(UUID(task_id_str))
