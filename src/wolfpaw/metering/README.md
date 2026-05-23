# metering/

Per-call cost recording for every model call, the cap enforcer, the prompt-version registry, and the `/usage` slash command. Built before any agent so from the first model call we have full observability.

## Files

- **`types.py`** — shared dataclasses: `TokenCounts`, `ModelPrice`, `ModelCallResult`, `PromptVersionRow`.
- **`pricing.py`** — `compute_cost_cents(price, usage)` (pure, rounds up so we never undercharge) and `get_active_price(conn, model_id, at_time)` (respects `effective_from / effective_to` so historical rows price as they were at the call's `created_at`).
- **`prompt_versions.py`** — read/write the `prompt_versions` table. `get_active_prompt_version(agent)` returns the row to stamp on each `token_usage` write; `bump_prompt_version(...)` is the idempotent loader called when an agent's prompt changes.
- **`recorder.py`** — `record_usage(...)` inserts one `token_usage` row per model call, with `trace_id` pulled from the request-scoped contextvar so callers don't have to plumb it.
- **`enforcer.py`** — `Enforcer.check_can_spend(user_id)`. No-op stub in v1 (every user is on the `dev` tier); step 24 replaces this with a real period-summary lookup that raises `OverCap` when the user is at allowance with overage off.
- **`langsmith_client.py`** — context manager that wraps each model call in a LangSmith trace. Gated on `WOLFPAW_LANGSMITH_ENABLED`; a no-op when disabled.
- **`model_client.py`** — `ModelClient.call(...)` is the central wrapper every agent goes through. Sequence: enforcer check → LangSmith trace context → `anthropic.messages.create` → pricing lookup → recorder write → return `ModelCallResult`. The Anthropic client is injectable so tests pass a fake.
- **`usage_report.py`** — backs the `/usage` slash command. `build_usage_report(user_id, scope)` queries `token_usage × model_prices` via a LATERAL join (priced per-row at its own `created_at`), plus `compute_usage`, returns a `UsageReport` dataclass, and caches per `(user_id, scope)` for 30s. `render_text(report)` is the default formatter; channels can override. Registers `/usage`.

## How it fits together

Every model call: agent → `ModelClient.call(user_id, agent, model, messages, prompt_version_id=...)`. The wrapper enforces caps, runs through LangSmith for tracing, hits Anthropic, computes cost from the active price row, writes a `token_usage` row, returns the result. The `/usage` command reads those rows back as the user's "what did I spend?" answer.

## Extending

- **New agent's prompt:** before its first call, ensure a `prompt_versions` row exists (call `bump_prompt_version` once at boot for each agent). Pass the resulting `id` as `prompt_version_id` to `ModelClient.call`.
- **New model:** add a row to `model_prices` (use the `models_prices` seed or a follow-up migration). Pricing + recording adapt automatically.
- **Real enforcement** (step 24): swap `Enforcer.check_can_spend` for a real implementation that reads `usage_summaries` for the current period; existing tests cover the no-op path.
