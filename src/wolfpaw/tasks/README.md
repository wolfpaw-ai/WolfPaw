# tasks/

Persistent, trackable units of long-running work. Step 15 ships the
synchronous substrate (Task row + state machine + ask_user); step 23
splits the service into `create()` + `run(task_id)` so the arq worker
can pick up the run while the Router returns immediately. Subagents
(step 16) keep using the synchronous `create_and_run` because the
parent's plan waits on the child.

## Files

- **`__init__.py`** — intentionally empty. Eager re-exports from `service` would create an import cycle (agents → toolbox → `ask_user` tool → tasks init → service → agents). Consumers use full module paths.
- **`service.py`** — `TaskService` exposes three entry points: (1) `create(...)` inserts the row in `pending` + stamps a `status.pending` event carrying the run inputs (content, thread_id, complexity_hint) so a worker process can recover them later; (2) `run(task_id)` reloads those inputs and drives the Planner → Executor → Post-Evaluator chain through to a terminal status; (3) `create_and_run(...)` does both inline (subagent path + dev fallback when workers are disabled). `TaskOutcome` dataclass carries the final task, plan, execution, verdict, and answer. Singleton `get_task_service()`.
- **`ask_user_registry.py`** — `AskUserRegistry`: process-local dict of `PendingQuestion`s keyed on `question_id`. `register(...)` creates an asyncio.Future the `ask_user` tool awaits; `submit_answer(...)` resolves it. Cross-user `submit_answer` attempts surface as `UnknownQuestion` rather than leaking the question's existence. `cancel(...)` raises in the awaiter — used when a task is cancelled mid-question.
- **`commands.py`** — registers `/tasks`, `/task <id>`, `/cancel <id>` with the channel dispatcher at module import. All three are user-scoped: you can't list, inspect, or cancel another user's tasks.
- **`routes.py`** — JSON HTTP API consumed by the React app (step 19): `GET /tasks`, `GET /tasks/{id}` (with events), `POST /tasks/{id}/cancel`. Same user-scoping as the slash commands. 404 on cross-user reads, 409 on cancel of an already-terminal task.

## Flow — task verdict end to end

The task-vs-plan decision now lives on the **Planner**, not Triage.
Triage only emits `quick` or `plan`; if `plan`, the Planner runs and
emits `plan.is_task: bool`. The Router reads that flag to decide
whether to wrap execution in a Task lifecycle.

When `plan.is_task=true`:
* `WOLFPAW_WORKERS_ENABLED=true` → Router calls `TaskService.create()`,
  enqueues `run_task` onto arq, returns "Started Task <id>" immediately;
  the worker process invokes `TaskService.run(task_id)` later (which
  re-runs the Planner inside the task).
* workers off → Router calls `TaskService.create_and_run(precomputed_plan=plan, ...)`
  with the plan it already built, skipping the wasted re-plan.

Subagents always use `create_and_run` without a precomputed plan
(they plan fresh inside their own task). Both paths converge on the
same internal pipeline below.

```
Triage → "plan" → Planner emits plan with is_task=true
  → TaskService.create_and_run(precomputed_plan=plan, ...)   ← workers off
    (or)
  → TaskService.create(...) + enqueue_run_task                ← workers on
  → arq worker → TaskService.run(task_id)
      1. tasks_dao.create()            → row in `pending`, event `status.pending`
                                          (the event's content JSONB carries
                                           content + thread_id + complexity_hint
                                           so `run` can recover them)
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
      7. tasks_dao.rollup_spent_cents() → sum of token_usage + compute_usage rows
                                          for this task → tasks.spent_cents
  ← TaskOutcome(task, plan, execution, verdict, final_answer)
```

`spent_cents` rollup runs on every terminal transition (including planner-failed / executor-crashed via `_fail`, plus the `/cancel` route + slash command) so `/tasks` and `/task <id>` show authoritative final cost. The roll-up sums *direct* spend only — a subagent's own `spent_cents` reflects its own model + compute rows; a future recursive query could surface the full subtree cost on the root.

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
- Step 23 moves long-running Tasks onto the arq worker, but the `AskUserRegistry` still lives in-process. The worker process holds the registry; the API process's `/answer` endpoint signals via a small DB-backed handoff. **That handoff is still deferred** — today, an `ask_user` mid-task only resolves cleanly when the task is running on the same process that owns the registry. Workers-mode `ask_user` is a known gap; fixing it needs a Postgres LISTEN/NOTIFY (or a Redis pub-sub) signal from `/answer` to the worker.

## How it fits with the rest

- **`memory/tasks.py`** owns the row + state transitions.
- **`memory/task_events.py`** is the append-only history every transition writes to.
- **`agents/router.py`** calls `TaskService.create_and_run` on the `task` verdict; ctx.task_id flows from there into the Executor (which uses it for sandbox keying) and into tool ctx (which `ask_user` requires).
- **`channels/web.py`** owns the `/answer` endpoint that resolves pending questions.
- **`metering`** — every model call inside a task already records `task_id` in `token_usage` (the wrapper has had that parameter since step 4); sandbox compute likewise flows through `compute_usage`.

## Extending

- **Proactive task-completion push** (v2 step 37): when `TaskService.run` reaches a terminal state, look up `tasks.channel_for_completion` and call the appropriate channel's `Channel.send(user_id, content)` with the final answer. Today the worker quietly finishes the row; users have to poll `/tasks`.
- **`ask_user` across workers** (deferred — see registry caveats above): plumb `/answer` → worker via LISTEN/NOTIFY or Redis pub-sub so workers-mode tasks can pause for user input cleanly.
- **Recurring tasks**: `tasks.schedule_pattern` is already a column; an arq cron-style scheduler in `workers/jobs/` enqueues `run_task` on the cadence.
- **Recursive subtree cost**: `spent_cents` currently reflects direct spend only. A recursive CTE over `parent_task_id` would surface the full subtree cost on the root once budget enforcement needs it.
