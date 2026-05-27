# metering/

Per-call cost recording for every model call, the cap enforcer, the prompt-version registry, and the `/usage` slash command. Built before any agent so from the first model call we have full observability. Top-level placement is in the [root README](../../../README.md).

## Files

- **`types.py`** — shared dataclasses: `TokenCounts`, `ModelPrice`, `ModelCallResult`, `PromptVersionRow`.
- **`pricing.py`** — `compute_cost_cents(price, usage)` (pure, rounds up so we never undercharge) and `get_active_price(conn, model_id, at_time)` (respects `effective_from / effective_to` so historical rows price as they were at the call's `created_at`).
- **`prompt_versions.py`** — read/write the `prompt_versions` table. `get_active_prompt_version(agent)` returns the row to stamp on each `token_usage` write; `bump_prompt_version(...)` is the idempotent loader called when an agent's prompt changes.
- **`recorder.py`** — `record_usage(...)` inserts one `token_usage` row per model call, with `trace_id` pulled from the request-scoped contextvar so callers don't have to plumb it.
- **`enforcer.py`** — `Enforcer.check_can_spend(user_id)`. No-op stub in the OSS app (every user is on the `dev` tier); operators wire in a real period-summary lookup that raises `OverCap` when the user is at allowance with overage off.
- **`langsmith_client.py`** — context manager that wraps each model call in a LangSmith trace. Gated on `WOLFPAW_LANGSMITH_ENABLED`; a no-op when disabled.
- **`model_client.py`** — `ModelClient.call(...)` is the central wrapper every agent goes through. The Anthropic client is injectable so tests pass a fake.
- **`usage_report.py`** — backs the `/usage` slash command. `build_usage_report(user_id, scope)` queries `token_usage × model_prices` via a LATERAL join (priced per-row at its own `created_at`), plus `compute_usage`, returns a `UsageReport` dataclass, and caches per `(user_id, scope)` for 30s.
- **`routes.py`** — JSON `GET /usage?scope=default|today|month|all` consumed by the React app.

## Flow — single model call

```mermaid
flowchart TD
    Agent[("Any agent.<br/>e.g. agents/planner.py")]
    Agent -->|"ModelClient.call(...)"| Enf[Enforcer.check_can_spend]
    Enf -->|"under cap"| Trace[LangSmith trace context<br/>(no-op if disabled)]
    Enf -->|"over cap"| OverCap["raise OverCap →<br/>agent surfaces to user"]
    Trace --> Anthropic[("anthropic.messages.create")]
    Anthropic --> PriceLook[("pricing.get_active_price<br/>at call's created_at")]
    PriceLook --> Recorder[("recorder.record_usage<br/>→ token_usage row<br/>with trace_id + prompt_version_id<br/>+ task_id if in a Task")]
    Recorder --> CostNotif{cost_notifier?}
    CostNotif -->|"step thresholds"| Notify[notify user via channel]
    Recorder --> Result[Return ModelCallResult to agent]

    click PriceLook "pricing.py"
    click Recorder "recorder.py"
    click Anthropic "model_client.py"
```

`/usage` reads back from `token_usage × model_prices` (LATERAL join so historical calls are priced as they were at `created_at`) plus `compute_usage` for sandbox-seconds. Caching is per-scope, 30 seconds.

## How it fits together

Every model call: agent → `ModelClient.call(user_id, agent, model, messages, prompt_version_id=...)`. The wrapper enforces caps, runs through LangSmith for tracing, hits Anthropic, computes cost from the active price row, writes a `token_usage` row, returns the result. Same path for embeddings — see [`embeddings/`](../embeddings/README.md) — except those use `record_usage` directly with `model=<embedder.name>` (no LangSmith trace because embeddings aren't model conversations).

Sandbox compute metering lives in [`sandbox/metering.py`](../sandbox/README.md) and writes to `compute_usage` on sandbox teardown — separate table from `token_usage`, same scoping by `task_id`. `/usage` sums both.

## Extending

- **New agent's prompt** — before its first call, ensure a `prompt_versions` row exists (call `bump_prompt_version` once at boot for each agent). Pass the resulting `id` as `prompt_version_id` to `ModelClient.call`.
- **New model** — add a row to `model_prices` (use a follow-up migration). Pricing + recording adapt automatically because `get_active_price` looks up by `model_id` + `created_at`.
- **Real enforcement** — swap `Enforcer.check_can_spend` for a real implementation that reads `usage_summaries` for the current period; existing tests cover the no-op path.
- **New cost dimension** (e.g. paid OAuth API calls billed through to the user) — add a sibling table (`<provider>_usage`), record from the relevant tool in [`toolbox/`](../toolbox/README.md) or [`integrations/`](../integrations/README.md), and surface in `usage_report.build_usage_report`.
