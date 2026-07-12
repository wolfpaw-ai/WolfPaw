"""Tasks subsystem — persistent, trackable units of work.

Step 15 ships the synchronous substrate:
    - Tasks DAO + state machine (in `memory/tasks.py`)
    - `TaskService` for create + run lifecycle (`tasks.service`)
    - `ask_user` durable pause/resume via `memory/pending_questions.py`
    - `/tasks`, `/task <id>`, `/cancel <id>` slash commands (`tasks.commands`)

The arq-driven async worker is a follow-up — sync execution covers the
correctness substrate for v1. When a task is created today, the Router
runs it inline; once arq lands, the same TaskService.run() will be
invoked by the worker instead.

The package init is intentionally empty: `tasks.service` imports back
into the agents package, so eager re-exports here would create an import
cycle. Consumers should use full module paths:

    from wolfpaw.memory import pending_questions
    from wolfpaw.tasks.service import get_task_service
"""
