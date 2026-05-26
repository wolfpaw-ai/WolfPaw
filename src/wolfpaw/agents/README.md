# agents/

Agent implementations + the Router that composes them. Step 10 shipped the Quick Agent; step 11 added the Triage Agent and the Router; step 12 added the Planning Agent; step 13 added the Executor; step 14 added the Post-Evaluator; step 16 added the `subagent` step kind to the Executor (parent tasks spawn child tasks for parallel investigations). v2 step 24 added the Plan Pre-Evaluator (sanity-check between Planner and Executor with a one-retry loop). The full plan-path now reads: Planner → Pre-Evaluator → (retry once if rejected) → Executor (functional / reasoning / evaluation / subagent) → Post-Evaluator → score back to procedural memory.

## Files

- **`__init__.py`** — re-exports `QuickAgent`, `TriageAgent`, `TriageVerdict`, `PlannerAgent`, `PlanContext`, `ExecutorAgent`, `PostEvaluatorAgent`, `Router`, and the `get_*` / `reset_*` helpers. Add new agents here as they come online.
- **`quick.py`** — `QuickAgent`: Haiku 4.5 + the 7 non-sandbox tools. The actual "do work" agent for one-shot answers. Singleton accessor `get_quick_agent()`; test hook `reset_quick_agent()`.
- **`triage.py`** — `TriageAgent`: Haiku 4.5 with a *forced* `classify` tool_use that returns `TriageVerdict(route, complexity, reasoning)`. Read-only — never writes to `messages`. Singleton accessor `get_triage_agent()`.
- **`planner.py`** — `PlannerAgent`: Sonnet 4.6 (Opus 4.7 for ambitious-complexity verdicts) with a *forced* `generate_plan` tool_use. Embeds the query via Voyage, retrieves similar past plans + matching seeded skills, inlines them into the system prompt, then asks Sonnet for a structured `Plan` (`schemas.Plan` with a list of `Step`s + `is_task` flag). Persists every generated plan into procedural memory (success/score=None until the Executor and Post-Evaluator run). Singleton accessor `get_planner_agent()`.
- **`executor.py`** — `ExecutorAgent`: runs a `Plan`. Walks steps in execution order, batches contiguous parallel-group steps via `asyncio.gather`. Four step kinds: functional dispatches through the tool registry; reasoning + evaluation make Sonnet calls with the plan + prior step results in context; **subagent** spawns a child Task via `TaskService.create_and_run` (step 16 — depth capped at 3, parent_task_id propagated, budget passed through). On step failure: marks remaining steps `SKIPPED`, returns an error summary as the final answer. Tears down the task's sandbox in `finally`. Persists `final_answer` + `success` + `error` to procedural memory (Post-Evaluator follows up with `score`). Synthesis is skipped when the last completed step is reasoning (the planner already produced the final text). Singleton accessor `get_executor_agent()`.
- **`post_evaluator.py`** — `PostEvaluatorAgent`: Haiku 4.5 with a *forced* `record_score` tool_use returning `PostEvalVerdict(score, summary, what_went_well, what_went_wrong, improvements)` on a 0-100 scale. Score is clamped server-side. Runs synchronously in the Router after the Executor; failures are swallowed so scoring never blocks the user response. Singleton accessor `get_post_evaluator_agent()`.
- **`plan_pre_evaluator.py`** — `PlanPreEvaluatorAgent` (v2 step 24): Haiku with a *forced* `evaluate_plan` tool_use returning three booleans (`achieves_objective`, `simplifiable`, `better_than_past_plans`) + a prose `diagnosis`. A plan is approved iff `achieves_objective AND NOT simplifiable AND better_than_past_plans`. The `plan_with_pre_evaluation(...)` helper in the same module runs Planner → Pre-Evaluator → (one retry with `revision_diagnosis` on rejection) and is what the Router + TaskService now call instead of `planner.plan(...)` directly. Approves by default on any internal failure — the cost of an unreviewed plan is worse output, the cost of a misbehaving evaluator blocking the chain is no output at all. Singleton accessor `get_pre_evaluator_agent()`.
- **`skill_distiller.py`** — `SkillDistillerAgent` (v2 step 25): Sonnet with a *forced* `emit_skill` tool_use producing `name`, `description`, `ingredients`, `generalized_steps`. The `maybe_distill_skill(...)` helper is the actual public entry point — called by the Router + TaskService after Post-Evaluator scoring. Five gates before persistence: score ≥ `WOLFPAW_SKILL_EMIT_MIN_SCORE` (default 90), multi-step + tool-using reusability heuristic, persisted plan row exists, no near-duplicate skill found via cosine search above `WOLFPAW_SKILL_DEDUP_SIMILARITY_THRESHOLD` (default 0.85), distiller produces a valid payload. Emits a `skill_emitted` SSE event on success. Failures are silent — skill emission is purely about the agent compounding quality and must never block the user's answer. Singleton accessor `get_skill_distiller_agent()`.
- **`router.py`** — `Router`: orchestrates Triage → downstream dispatch for every channel. Calls `TriageAgent.classify`, emits a `triage` event, then dispatches to Quick (one-shot) or Planner+Executor+Post-Evaluator (plan path) or `TaskService.create_and_run` (task path, ships in step 15 — wraps the same agents in a persistent Task row so `ask_user` works and ctx.task_id flows everywhere). The plan/task path emits a `plan` event with a step summary, propagates the executor's `step.start` / `step.end` / `step.error` events, then emits a `score` event with the verdict before returning the final answer; the task path also emits a `task` event with the new task id.

## Flow

```
channel /chat → Router.handle(content) →
    1. TriageAgent.classify(content) → TriageVerdict
    2. emit("triage", verdict.route + reasoning)
    3. switch on verdict.route:
        - quick → QuickAgent.handle(content) → final text
        - plan  → plan_with_pre_evaluation(planner, pre_evaluator, ...)
                      → PlannerAgent.plan(content, complexity_hint)
                      → PlanPreEvaluatorAgent.evaluate(content, plan, past_plans)
                      → emit("pre_eval", verdict)
                      → if approved: ship plan
                        else: PlannerAgent.plan(..., revision_diagnosis=verdict.diagnosis)
                              emit("pre_eval", "retry") — second draft ships unchecked
                  → emit("plan", summary)
                  → ExecutorAgent.execute(plan)
                      → emit("step.start" / "step.end" / "step.error") per step
                      → returns ExecutionPlan(final_answer, success, results)
                  → PostEvaluatorAgent.evaluate(plan, execution)
                      → emit("score", verdict)
                      → maybe_distill_skill(plan, execution, verdict)  # step 25
                          → if score ≥ 90, multi-step+tools, no near-duplicate:
                              SkillDistillerAgent.distill(...) → Sonnet
                              skills.store_emitted(...)
                              emit("skill_emitted", name)
                      → procedural.update_outcome(plan_id, score=...)
                      → task_events.append_event("plan_scored", verdict)
                  → persist user + final_answer to messages
                  → return final_answer
        - task  → TaskService.create_and_run(...)
                  → emit("task", task_id)
                  → planner + executor + post-eval all run inside a Task row
                    so ctx.task_id flows through (ask_user requires it,
                    sandbox keys on it, token_usage records it)
                  → returns the executor's final_answer
```

The Router is the only thing channels ever call. Adding a new channel (Telegram in step 18) means wiring it through `Router.handle(...)` exactly the same way the web channel does.

## How the Quick Agent works

```
handle(ctx, thread_id, content, emit=None) →
    1. Lazy-seed `quick:v1` into prompt_versions (idempotent, DB-tolerant)
    2. Load recent thread history via memory.conversational.fetch_recent
    3. Append user turn to messages list + persist via conv.append
    4. Tool loop, capped at MAX_ITERATIONS (10):
        - ModelClient.call(agent="quick", model=Haiku, tools=allowed_tools, ...)
        - If stop_reason != "tool_use" → break
        - Run each tool_use block (rejecting any not on the allow-list)
        - Append tool_result blocks as a user turn, loop
    5. Persist final assistant text via conv.append
    6. Return the text
```

Allow-list: `calculator`, `http_get`, `web_search`, `sql_query`, `create_table`, `read_doc`, `write_doc`. Sandbox + artifact tools are off-limits to the Quick Agent — those belong to the Executor (step 13). The loop rejects any `tool_use` for a name not in `ALLOWED_TOOL_NAMES` with an `is_error` tool_result rather than calling the tool.

## How the Planner Agent works

```
plan(ctx, thread_id, content, complexity_hint) → (Plan, PlanContext) →
    1. Lazy-seed `planner:v1` into prompt_versions
    2. Embed the query via embeddings.get_embedder() (Voyage in prod, Stub in dev/tests)
        - record_usage row: agent="planner", model="voyage-3", input_tokens=N
    3. Retrieve context (all best-effort; empty list on failure):
        - conv.fetch_recent(thread_id, n=20)
        - procedural.search_similar(user_id, query_embedding, k=3)
        - skills.search_by_task(user_id, query_embedding, k=3)
    4. Build messages = recent history + new user turn
       Build system prompt = base + "## Past plans" + "## Relevant seeded skills"
    5. ModelClient.call with model = Sonnet (or Opus for ambitious),
       tools=[generate_plan_tool],
       tool_choice={"type":"tool","name":"generate_plan"}
    6. Parse forced tool_use → schemas.Plan(steps, summary, is_task, ...)
    7. procedural.store(...) → plan.id set
    8. Return (Plan, PlanContext) — Router renders the preview today
```

Model tier rules (today): `complexity in ("simple", "moderate")` → Sonnet; `"ambitious"` → Opus. Every dev user is on `tier='dev'` which allows Opus. Step 24 (billing) wires real per-tier gating.

The Planner does NOT persist anything to `messages` — the Router persists the user message + the Executor's final answer once the plan completes.

## How the Executor Agent works

```
execute(ctx, plan, emit=None) → ExecutionPlan →
    1. Lazy-seed `executor:v1` into prompt_versions
    2. Reject plans with > 50 steps (cap) — return error summary, persist failure
    3. Walk plan.steps in order:
        - Sequential step (parallel_group=None): await one
        - Contiguous parallel-group steps: asyncio.gather them
        - For each step:
            - functional → tool = registry.get(step.tool); await tool.run(ctx, **step.inputs)
            - reasoning  → ModelClient.call(Sonnet, system + prompt with prior results)
            - evaluation → same as reasoning (v1; structured verdicts are v2)
            - emit step.start / step.end / step.error
        - On any step failure: mark remaining SKIPPED, break the walk
    4. Synthesis:
        - If the last completed step was reasoning → use its text directly
        - Else → one extra Sonnet synthesis call over all step results
    5. finally: SandboxManager.close_for_task(ctx.user_id, ctx.task_id)
    6. If plan.id set → procedural.update_outcome(final_answer, success, error)
    7. Return ExecutionPlan(plan, results, final_answer, success, error)
```

Failure semantics: a single failed step fails the whole plan (no retry in v1). The Router renders the executor's `final_answer` regardless of `success` — on failure it's the markdown summary "I ran into a problem on step X…". The Post-Evaluator (step 14) scores the outcome via `procedural.update_outcome(score=...)`.

Sandbox lifecycle: always closed in `finally`. SandboxManager pops by `(user_id, task_id)` key, so closing a sandbox that was never spun up is a safe no-op. Functional steps that hit sandbox tools (`run_python`, `create_pdf`, etc.) implicitly create the sandbox on first call; this method tears it down on exit so the next plan starts fresh.

### subagent steps (step 16)

A `subagent` step delegates to a child Task that runs its own full Planner→Executor→Post-Eval pipeline.

```
_run_subagent(ctx, step) →
    1. Require ctx.task_id (subagents only make sense inside a Task)
    2. Check depth: tasks_dao.get_depth(ctx.task_id) < MAX_SUBAGENT_DEPTH (3)
    3. Resolve root: root_task_id = tasks_dao.get_root(ctx.task_id)
    4. Lazy-import TaskService (agents↔tasks circular)
    5. async with _acquire_subagent_slot(root_task_id):  # cap=5 per root
           service.create_and_run(
             content=step.inputs["query"],
             title=step.inputs.get("title") or step.description[:80],
             budget_cents=step.inputs.get("budget_cents"),
             parent_task_id=ctx.task_id,
             thread_id=None,           # subagent gets fresh context
             emit=None,                # don't interleave child events into parent SSE
           )
    6. Reject if child status != "completed" (child failure → parent step failure)
    7. Return {subagent_task_id, answer, score}
```

Concurrency: multiple `subagent` steps in the same `parallel_group` execute via `asyncio.gather` just like functional/reasoning steps. A trailing reasoning step in the parent plan synthesizes the subagent outputs (each appears in prior_results as `{subagent_task_id, answer, score}`).

Per-root concurrency cap (`MAX_CONCURRENT_SUBAGENTS_PER_ROOT = 5`): all descendants of the same root task share one `asyncio.Semaphore`, so a wide fanout plan can't saturate the model provider or sandbox pool. Cousin subagents in unrelated branches under the same root contend for the same slots; different users' root tasks are independent. The semaphore registry is refcounted — the entry drops out of the dict once its last in-flight subagent releases.

### WorkspaceCollision → ask_user (step 13+15 deferred follow-up)

`write_doc` raises `WorkspaceCollision` when a file with the same name already exists and `overwrite=False`. Without the executor hook below, this would surface as an opaque step failure ("workspace file already exists at v2").

The Executor catches `WorkspaceCollision` in `_run_functional` and:
1. If `ctx.task_id` is set (we're inside a Task), calls the `ask_user` tool with "Overwrite report.md (v1)? (yes / no)" and `options=["yes", "no"]`.
2. On a yes-ish answer (`yes`, `y`, `overwrite`, `ok`, `confirm`), retries the same tool with `overwrite=True` and the original inputs — the prior row stays in history, the new row is `version + 1`.
3. On any other answer (or no Task context), the collision propagates as the original step failure so the user sees an actionable error.

This is the only collision handler today; future destructive actions (mass delete, large purchase, send-message) should follow the same shape: catch the domain exception in the executor and route it through `ask_user`.

## How the Post-Evaluator works

```
evaluate(ctx, plan, execution) → PostEvalVerdict →
    1. Lazy-seed `post_evaluator:v1` into prompt_versions
    2. Build prompt: user request + plan summary + step list + step outcomes + final answer + success flag
    3. ModelClient.call(Haiku) with tools=[record_score],
       tool_choice={"type":"tool","name":"record_score"}
    4. Extract forced tool_use → PostEvalVerdict, clamp score to [0, 100]
    5. Return verdict (Router persists separately)
```

Persistence (done by the Router, not the agent):
```
procedural.update_outcome(plan_id, score=verdict.score)
task_events.append_event(
    event_type="plan_scored",
    content={plan_id, score, summary, what_went_well, what_went_wrong, improvements},
)
```

Failure posture: the Post-Evaluator runs in a guarded `try` block in the Router. If `evaluate` raises (Anthropic outage, malformed response, etc.), the user still receives the Executor's `final_answer` and a `WARN` log is emitted. Scoring is purely for the recipe-box (procedural memory + observability) — it must never block the user response.

Fallback verdict: if the model somehow returns without calling the forced tool, the agent defaults to `score=50` for successful executions and `score=0` for failed ones, with a `summary` noting that the score was defaulted. The Planner's future `min_score` filters will treat these neutrally rather than as endorsements.

Scoring scale (from the system prompt):
- 100 = served the request completely, cleanly, efficiently.
- 70–99 = served well; small issues.
- 40–69 = partially served; missing pieces or notable inefficiencies.
- 1–39 = poorly served; major gaps.
- 0 = total failure.

Skills auto-emission is **v2** — when a plan scores 90+ and looks reusable, future versions will emit a new row into `skills` keyed on the originating plan. The schema (`skills.source_plan_id`) is ready; the agent logic is not.

## How the Triage Agent works

```
classify(ctx, thread_id, content) →
    1. Lazy-seed `triage:v1` into prompt_versions
    2. Load recent thread history (fetch_recent — summaries land step 12.5)
    3. Build messages = past + new user turn
    4. ModelClient.call with tools=[classify_tool],
       tool_choice={"type":"tool","name":"classify"}
    5. Extract the forced tool_use input → TriageVerdict
    6. Return verdict (NO writes to `messages`)
```

The forced `tool_choice` makes the model emit a single JSON object matching the `classify` tool's input_schema — no prose to parse, no freeform output to wrestle with. If the model somehow returns without a tool_use block (shouldn't happen given the forced choice), the agent logs a warning and defaults to `route="quick"`.

## How it fits with the rest of the system

- **ModelClient** (`metering/`) handles every model call — pricing, token-usage recording, LangSmith trace. Agents never call `anthropic.messages.create` directly.
- **Tool registry** (`toolbox/`) is the source of tools; the Quick Agent resolves names via `Registry.get(name)`. Tool inputs are validated by the tool itself.
- **Conversational memory** (`memory/conversational.py`) owns thread + message persistence. Quick calls `conv.append` for the user message + the final assistant message; Triage / Planner / Executor don't persist anything (the Router handles user + final-answer appends for the plan path).
- **Embeddings** (`embeddings/`) — Planner uses `get_embedder()` to embed queries; provider is selectable via `WOLFPAW_EMBEDDING_BACKEND=voyage|stub`.
- **Procedural + skills memory** (`memory/procedural.py`, `memory/skills.py`) — Planner reads via `search_similar` and `search_by_task` and writes generated plans via `procedural.store`. Executor writes outcome (`final_answer` / `success` / `error`) via `procedural.update_outcome` (partial — no `score`). Post-Evaluator (step 14) closes the loop with `update_outcome(score=...)`.
- **Sandbox** (`sandbox/`) — Executor calls `get_sandbox_manager().close_for_task(user_id, task_id)` in `finally`. Functional steps that use sandbox tools implicitly create the sandbox on first call.
- **Prompt versions** (`metering/prompt_versions.py`) are seeded lazily on each agent's first call so the `<agent>:v1` row exists by the time the first `token_usage` write needs its id. DB unavailability degrades to `prompt_version_id=None` rather than crashing. Only the `agent_role` content is hashed — Soul + User File blocks vary per-user and aren't part of the version identity (step 17).
- **Persona** (`persona/`) wraps every system prompt with Soul + the user's profile via `build_for_agent(user_id, agent_role)`. Defensive — agents still get their bare role prompt if Soul or DB lookups fail. New threads are stamped with the active `soul_version` + `user_profile_version`.

## Extending

- **New agent:** add a class in its own module (`planner.py`, `executor.py`, `post_evaluator.py`, …), set `AGENT_KIND` to the appropriate `agent_kind` enum value from `001_init.sql`, define its `ALLOWED_TOOL_NAMES` if it has tool access, and follow the loop pattern from `quick.py` (call ModelClient, append assistant block, branch on `stop_reason`, loop on `tool_use`). Add it to `Router` so the channels pick it up.
- **Different model:** point the agent's `model` parameter at the appropriate `model_*` setting from `config.py` (Haiku for triage / quick / post-evaluator, Sonnet for executor / planner, Opus for ambitious planning).
- **New triage route:** extend the `Route` literal in `triage.py`, add the enum value to the `classify` tool's `input_schema`, update the system prompt with a definition + examples, and add a dispatch branch in `Router.handle`.
- **New `_AGENT_ROLE`** for a new agent: keep it focused on the agent's specific job. The Soul + User File context is wrapped in by `persona.build_for_agent` — don't re-state persona details in the role prompt.
- **Token-level streaming:** agents currently return the full final text in one shot and the channel emits it as a single `delta`. To stream tokens, ModelClient needs a streaming variant; the channel already supports SSE deltas — just emit multiple `delta` events as they arrive.
