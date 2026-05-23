# agents/

Agent implementations + the Router that composes them. Step 10 shipped the Quick Agent; step 11 added the Triage Agent and the Router. Planner / Executor / Post-Evaluator land in steps 12–14.

## Files

- **`__init__.py`** — re-exports `QuickAgent`, `TriageAgent`, `TriageVerdict`, `Router`, `get_router`, `reset_router`. Add new agents here as they come online.
- **`quick.py`** — `QuickAgent`: Haiku 4.5 + the 7 non-sandbox tools. The actual "do work" agent for one-shot answers. Singleton accessor `get_quick_agent()`; test hook `reset_quick_agent()`.
- **`triage.py`** — `TriageAgent`: Haiku 4.5 with a *forced* `classify` tool_use that returns `TriageVerdict(route, complexity, reasoning)`. Read-only — never writes to `messages`. Singleton accessor `get_triage_agent()`.
- **`router.py`** — `Router`: orchestrates Triage → downstream dispatch for every channel. Calls `TriageAgent.classify`, emits a `triage` event with the verdict, then dispatches to Quick (today) or Plan / Task (later steps). When destinations don't exist yet, the router falls through to Quick with an honest preamble.

## Flow

```
channel /chat → Router.handle(content) →
    1. TriageAgent.classify(content) → TriageVerdict
    2. emit("triage", verdict.route + reasoning)
    3. switch on verdict.route:
        - quick → QuickAgent.handle(content) → final text
        - plan  → (step 12) PlannerAgent.plan(...);  today: Quick + preamble
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
- **Conversational memory** (`memory/conversational.py`) owns thread + message persistence. Quick calls `conv.append` for the user message + the final assistant message; Triage doesn't persist anything.
- **Prompt versions** (`metering/prompt_versions.py`) are seeded lazily on each agent's first call so the `<agent>:v1` row exists by the time the first `token_usage` write needs its id. DB unavailability degrades to `prompt_version_id=None` rather than crashing.

## Extending

- **New agent:** add a class in its own module (`planner.py`, `executor.py`, `post_evaluator.py`, …), set `AGENT_KIND` to the appropriate `agent_kind` enum value from `001_init.sql`, define its `ALLOWED_TOOL_NAMES` if it has tool access, and follow the loop pattern from `quick.py` (call ModelClient, append assistant block, branch on `stop_reason`, loop on `tool_use`). Add it to `Router` so the channels pick it up.
- **Different model:** point the agent's `model` parameter at the appropriate `model_*` setting from `config.py` (Haiku for triage / quick / post-evaluator, Sonnet for executor / planner, Opus for ambitious planning).
- **New triage route:** extend the `Route` literal in `triage.py`, add the enum value to the `classify` tool's `input_schema`, update the system prompt with a definition + examples, and add a dispatch branch in `Router.handle`.
- **Soul + User File** (step 17): the inline `_SYSTEM_PROMPT` constants in `quick.py` / `triage.py` are v1 placeholders. When step 17 lands, replace them with a builder that loads `soul.md` and the per-user `user_profiles` row, stamps `threads.soul_version` + `threads.user_profile_version`, and bumps the prompt version when either changes.
- **Token-level streaming:** agents currently return the full final text in one shot and the channel emits it as a single `delta`. To stream tokens, ModelClient needs a streaming variant; the channel already supports SSE deltas — just emit multiple `delta` events as they arrive.
