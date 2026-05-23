# agents/

Agent implementations. Step 10 ships the Quick Agent; Triage / Planner / Executor / Post-Evaluator land in steps 11–14.

## Files

- **`__init__.py`** — re-exports the public agent classes. Add new agents here as they come online.
- **`quick.py`** — `QuickAgent`: Haiku 4.5 + the 7 non-sandbox tools. Used for one-shot answers that don't need a plan. Until Triage (step 11) lands, the web channel pipes every non-slash message straight through this agent. Singleton accessor `get_quick_agent()`; test hook `reset_quick_agent()`.

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

The `emit(event, data)` callback is optional; channels pass one in to surface progress (`event="tool"` with a compact `name(input)` summary) as the loop runs. The web channel uses this to push SSE `tool` events to the client before the final `delta`.

Allow-list: `calculator`, `http_get`, `web_search`, `sql_query`, `create_table`, `read_doc`, `write_doc`. Sandbox + artifact tools are off-limits to the Quick Agent — those belong to the Executor (step 13). The loop rejects any `tool_use` for a name not in `ALLOWED_TOOL_NAMES` with an `is_error` tool_result rather than calling the tool.

## How it fits with the rest of the system

- **ModelClient** (`metering/`) handles every model call — pricing, token-usage recording, LangSmith trace. The agent never calls `anthropic.messages.create` directly.
- **Tool registry** (`toolbox/`) is the source of tools; the agent resolves names via `Registry.get(name)`. Tool inputs are validated by the tool itself.
- **Conversational memory** (`memory/conversational.py`) owns thread + message persistence. The agent calls `conv.append` for the user message + the final assistant message — intermediate tool calls/results live in-process to keep the verbatim window small.
- **Prompt versions** (`metering/prompt_versions.py`) are seeded lazily on first call so the `quick:v1` row exists by the time the first `token_usage` write needs its id. DB unavailability degrades to `prompt_version_id=None` rather than crashing the agent.

## Extending

- **New agent:** add a class in its own module (`triage.py`, `planner.py`, `executor.py`, …), set `AGENT_KIND` to the appropriate `agent_kind` enum value from `001_init.sql`, define its `ALLOWED_TOOL_NAMES`, and reuse the model loop pattern from `quick.py` (call ModelClient, append assistant block, branch on `stop_reason`, loop on `tool_use`).
- **Different model:** point the agent's `model` parameter at the appropriate `model_*` setting from `config.py` (Haiku for triage / quick / post-evaluator, Sonnet for executor / planner, Opus for ambitious planning).
- **Soul + User File** (step 17): the inline `_SYSTEM_PROMPT` constant in `quick.py` is a v1 placeholder. When step 17 lands, replace it with a builder that loads `soul.md` and the per-user `user_profiles` row, stamps `threads.soul_version` + `threads.user_profile_version`, and bumps the prompt version when either changes.
- **Token-level streaming:** the agent currently returns the full final text in one shot and the channel emits it as a single `delta`. To stream tokens, ModelClient needs a streaming variant; the channel already supports SSE deltas — just emit multiple `delta` events as they arrive.
