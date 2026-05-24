# agents/

Agent implementations + the Router that composes them. Step 10 shipped the Quick Agent; step 11 added the Triage Agent and the Router; step 12 added the Planning Agent; step 13 added the Executor. Post-Evaluator lands in step 14.

## Files

- **`__init__.py`** — re-exports `QuickAgent`, `TriageAgent`, `TriageVerdict`, `PlannerAgent`, `PlanContext`, `ExecutorAgent`, `Router`, and the `get_*` / `reset_*` helpers. Add new agents here as they come online.
- **`quick.py`** — `QuickAgent`: Haiku 4.5 + the 7 non-sandbox tools. The actual "do work" agent for one-shot answers. Singleton accessor `get_quick_agent()`; test hook `reset_quick_agent()`.
- **`triage.py`** — `TriageAgent`: Haiku 4.5 with a *forced* `classify` tool_use that returns `TriageVerdict(route, complexity, reasoning)`. Read-only — never writes to `messages`. Singleton accessor `get_triage_agent()`.
- **`planner.py`** — `PlannerAgent`: Sonnet 4.6 (Opus 4.7 for ambitious-complexity verdicts) with a *forced* `generate_plan` tool_use. Embeds the query via Voyage, retrieves similar past plans + matching seeded skills, inlines them into the system prompt, then asks Sonnet for a structured `Plan` (`schemas.Plan` with a list of `Step`s + `is_task` flag). Persists every generated plan into procedural memory (success/score=None until step 14's Post-Evaluator). Singleton accessor `get_planner_agent()`.
- **`executor.py`** — `ExecutorAgent`: runs a `Plan`. Walks steps in execution order, batches contiguous parallel-group steps via `asyncio.gather`. Functional steps dispatch through the tool registry; reasoning + evaluation steps make Sonnet calls with the plan + prior step results in context. On step failure: marks remaining steps `SKIPPED`, returns an error summary as the final answer. Tears down the task's sandbox in `finally`. Persists `final_answer` + `success` + `error` to procedural memory (Post-Evaluator follows up with `score`). Synthesis is skipped when the last completed step is reasoning (the planner already produced the final text). Singleton accessor `get_executor_agent()`.
- **`router.py`** — `Router`: orchestrates Triage → downstream dispatch for every channel. Calls `TriageAgent.classify`, emits a `triage` event, then dispatches to Quick or Planner+Executor or Task (currently falls through to Quick with a preamble until step 15). The plan path emits a `plan` event with a step summary and propagates the executor's `step.start` / `step.end` / `step.error` events as they fire.

## Flow

```
channel /chat → Router.handle(content) →
    1. TriageAgent.classify(content) → TriageVerdict
    2. emit("triage", verdict.route + reasoning)
    3. switch on verdict.route:
        - quick → QuickAgent.handle(content) → final text
        - plan  → PlannerAgent.plan(content, complexity_hint=verdict.complexity)
                  → emit("plan", summary)
                  → ExecutorAgent.execute(plan)
                      → emit("step.start" / "step.end" / "step.error") per step
                      → returns ExecutionPlan(final_answer, success, results)
                  → persist user + final_answer to messages
                  → return final_answer
        - task  → (step 15) TaskService.create(...); today: Quick + preamble
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
- **Prompt versions** (`metering/prompt_versions.py`) are seeded lazily on each agent's first call so the `<agent>:v1` row exists by the time the first `token_usage` write needs its id. DB unavailability degrades to `prompt_version_id=None` rather than crashing.

## Extending

- **New agent:** add a class in its own module (`planner.py`, `executor.py`, `post_evaluator.py`, …), set `AGENT_KIND` to the appropriate `agent_kind` enum value from `001_init.sql`, define its `ALLOWED_TOOL_NAMES` if it has tool access, and follow the loop pattern from `quick.py` (call ModelClient, append assistant block, branch on `stop_reason`, loop on `tool_use`). Add it to `Router` so the channels pick it up.
- **Different model:** point the agent's `model` parameter at the appropriate `model_*` setting from `config.py` (Haiku for triage / quick / post-evaluator, Sonnet for executor / planner, Opus for ambitious planning).
- **New triage route:** extend the `Route` literal in `triage.py`, add the enum value to the `classify` tool's `input_schema`, update the system prompt with a definition + examples, and add a dispatch branch in `Router.handle`.
- **Soul + User File** (step 17): the inline `_SYSTEM_PROMPT` constants in `quick.py` / `triage.py` are v1 placeholders. When step 17 lands, replace them with a builder that loads `soul.md` and the per-user `user_profiles` row, stamps `threads.soul_version` + `threads.user_profile_version`, and bumps the prompt version when either changes.
- **Token-level streaming:** agents currently return the full final text in one shot and the channel emits it as a single `delta`. To stream tokens, ModelClient needs a streaming variant; the channel already supports SSE deltas — just emit multiple `delta` events as they arrive.
