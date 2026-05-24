# tasks/

Persistent, trackable units of long-running work. Step 15 ships the
synchronous substrate (Task row + state machine + ask_user); the
arq-driven async worker is a follow-up.

## Files

- **`__init__.py`** — intentionally empty. Eager re-exports from `service` would create an import cycle (agents → toolbox → `ask_user` tool → tasks init → service → agents). Consumers use full module paths.
- **`service.py`** — `TaskService.create_and_run(...)`: orchestrates the full Planner → Executor → Post-Evaluator chain inside a Task row, emitting state-transition events at each step. `TaskOutcome` dataclass carries the final task, plan, execution, verdict, and answer. Singleton `get_task_service()`.
- **`ask_user_registry.py`** — `AskUserRegistry`: process-local dict of `PendingQuestion`s keyed on `question_id`. `register(...)` creates an asyncio.Future the `ask_user` tool awaits; `submit_answer(...)` resolves it. Cross-user `submit_answer` attempts surface as `UnknownQuestion` rather than leaking the question's existence. `cancel(...)` raises in the awaiter — used when a task is cancelled mid-question.
- **`commands.py`** — registers `/tasks`, `/task <id>`, `/cancel <id>` with the channel dispatcher at module import. All three are user-scoped: you can't list, inspect, or cancel another user's tasks.

## Flow — task verdict end to end

```
Router gets "task" verdict
  → TaskService.create_and_run(user_id, content, ...)
      1. tasks_dao.create()            → row in `pending`, event `status.pending`
      2. tasks_dao.mark_started()      → row in `running`,  event `status.running`
      3. PlannerAgent.plan()           → Plan with steps + plan.id
         tasks_dao.attach_plan()       → row.current_plan_id = plan.id
      4. ExecutorAgent.execute()       → ExecutionPlan
           - functional/reasoning/eval steps dispatch (ctx.task_id flows through)
           - if a step calls ask_user → tool registers a question, marks task
             awaiting_user, awaits answer_future
      5. PostEvaluatorAgent.evaluate() → score persisted via procedural.update_outcome
                                          + task_event `plan_scored`
      6. terminal:
           execution.success           → tasks_dao.mark_completed() + event
           else                        → tasks_dao.mark_failed()    + event
  ← TaskOutcome(task, plan, execution, verdict, final_answer)
```

## ask_user pause/resume

1. Executor invokes the `ask_user` tool with a question.
2. Tool calls `AskUserRegistry.register(user_id, task_id, question, ...)` — gets back a `PendingQuestion` with an `answer_future`.
3. Tool transitions the task: `tasks_dao.mark_awaiting_user(...)` + `task_events.append_event("user_question", ...)`.
4. Tool awaits `pq.answer_future` with a timeout (default 5 min).
5. **Reply path**: user POSTs to `/channels/web/answer` with `{question_id, answer}`. The endpoint calls `registry.submit_answer(...)` which resolves the future.
6. Tool wakes up: transitions task back to `running`, writes `user_answer` event, returns `{question_id, answer}` to the executor.
7. Timeout path: `mark_blocked` + `ToolError`. The task is left in `blocked` for human inspection.

In-process registry caveats:
- If the runtime restarts while a question is pending, the future is lost — the task is effectively orphaned. Acceptable for v1 (sync execution within one request).
- When the arq worker lands, the long-lived worker process holds the registry; the API process's `/answer` endpoint signals via a small DB-backed handoff (deferred).

## How it fits with the rest

- **`memory/tasks.py`** owns the row + state transitions.
- **`memory/task_events.py`** is the append-only history every transition writes to.
- **`agents/router.py`** calls `TaskService.create_and_run` on the `task` verdict; ctx.task_id flows from there into the Executor (which uses it for sandbox keying) and into tool ctx (which `ask_user` requires).
- **`channels/web.py`** owns the `/answer` endpoint that resolves pending questions.
- **`metering`** — every model call inside a task already records `task_id` in `token_usage` (the wrapper has had that parameter since step 4); sandbox compute likewise flows through `compute_usage`.

## Extending

- **Real async via arq** (deferred): add `workers/arq_app.py` that picks runnable tasks (status='pending' or 'awaiting_user' resumes) and calls `TaskService.run(task_id)`. The TaskService can be split into `create()` + `run()` so the Router can create the row + return immediately while the worker picks up the run.
- **Recurring tasks**: `tasks.schedule_pattern` is already a column; a cron-style scheduler in `workers/` enqueues runs.
- **WorkspaceCollision → ask_user**: catch `WorkspaceCollision` in the executor, call ask_user with "Overwrite?", on yes pass `overwrite=True` and retry the step. Small follow-up.
- **Subagent tasks** (step 16): `tasks.parent_task_id` is already a column. Router or executor spawns sub-tasks; their costs roll up to the parent via the partial-write `update_outcome` pattern.
