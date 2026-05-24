"""Tasks subsystem — persistent, trackable units of work.

Step 15 ships the synchronous substrate:
    - Tasks DAO + state machine (in `memory/tasks.py`)
    - `TaskService` for create + run lifecycle (`tasks.service`)
    - `ask_user` pause/resume registry (`tasks.ask_user_registry`)
    - `/tasks`, `/task <id>`, `/cancel <id>` slash commands (`tasks.commands`)

The arq-driven async worker is a follow-up — sync execution covers the
correctness substrate for v1. When a task is created today, the Router
runs it inline; once arq lands, the same TaskService.run() will be
invoked by the worker instead.

The package init is intentionally empty: `tasks.service` imports back
into the agents package, and a few agent-side modules (e.g. the toolbox
loading the `ask_user` tool) import from `tasks.ask_user_registry`.
Eager re-exports here would create an import cycle. Consumers should
use full module paths:

    from wolfpaw.tasks.ask_user_registry import get_registry
    from wolfpaw.tasks.service import get_task_service
"""
