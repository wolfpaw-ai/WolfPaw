# Wolfpaw — Implementation Plan (v1)

Living document. Updated as decisions are made.

## Guiding principle

Wolfpaw is a **general agent that can do lots of things** — not a domain-specific tool like `dmitris-fabulous_document-agent`. The architecture should bias toward generality: pluggable tools, model tiering, retrieval-driven memory. We may borrow patterns from the existing dmitris-fabulous agents, but don't carry over assumptions baked in for documents/recipes/etc.

## Decisions locked in

1. **Datastore:** Postgres + `pgvector` for v1. Oracle AI Database evaluated later for specific use cases.
2. **Hosting:** Own EC2 instance, possibly in a separate AWS account.
3. **Repo:** Standalone app, separate from `dmitris-fabulous`. Lives in `wolfpaw/` inside this workspace for now; will move to its own repo once formally begun.
4. **v1 scope:** Triage → (Quick | Plan → Execute → Post-Evaluator), with conversational + procedural memory and a static toolbox. Tool Creator, Plan Pre-Evaluator, Skills, and Sleep Cycle deferred to v2.
5. **Framework:** FastAPI (async, native SSE, Pydantic v2 schemas).
6. **Web search:** Tavily (purpose-built for agents — returns clean, ranked, LLM-friendly results; Brave is cheaper at high volume but raw, not worth the post-processing tax for v1).
7. **Auth:** `X-API-Key` header for v1 — enough to experiment safely. JWT/Cognito deferred to whenever Wolfpaw opens up to users.
8. **Soul file:** Drafted — see [soul.md](soul.md). Hand-edit as the persona evolves.
9. **Embeddings:** Voyage AI `voyage-3` (1024 dims). Behind a small interface so we can swap.

## Stack

- **Language/framework:** Python 3.12 + FastAPI. Pydantic v2 for schemas.
- **Database:** Postgres + `pgvector`. `asyncpg` driver, raw SQL via small migration files (no ORM — keeps the memory layer thin and translatable to Oracle later).
- **Models (Anthropic SDK direct):**
  - Triage → Haiku 4.5
  - Quick Agent → Haiku 4.5 (with tools)
  - Planner → Sonnet 4.6 default; escalate to Opus 4.7 when Triage flags "ambitious"
  - Executor reasoning steps → Sonnet 4.6
  - Post-Evaluator → Haiku 4.5
- **Embeddings:** Voyage `voyage-3`.
- **Process layout on EC2:** systemd unit running `uvicorn`, Caddy reverse proxy with TLS. v1 Postgres location TBD (RDS vs co-located).

## Folder layout (in `wolfpaw/` for now)

```
wolfpaw/
  pyproject.toml
  README.md
  spec.md                    # existing
  implementation_plan.md     # this file
  soul.md                    # persona
  alake_memory_manager.py    # reference (course material)
  alake_toolbox.py           # reference (course material)
  migrations/
    001_init.sql
  src/wolfpaw/
    config.py                # env, model IDs
    api.py                   # FastAPI: POST /chat (SSE)
    auth.py                  # X-API-Key middleware
    schemas.py               # Plan, Step, TriageResult, etc.
    soul.py                  # loads soul.md
    tracing.py               # trace_id, structured JSON logger
    agents/
      triage.py              # classify: quick | complex, ambitious?
      quick.py               # 1-2 tool calls, return
      planner.py             # generate plan, consult procedural memory
      executor.py            # run the plan (functional vs reasoning steps)
      post_evaluator.py      # score, store outcome
    memory/
      db.py                  # asyncpg pool
      conversational.py      # chat per thread
      procedural.py          # past plans + scores + vector search
    toolbox/
      registry.py            # static decorator-based registry
      tools/
        web_search.py        # Tavily
        http_get.py
        calculator.py
        read_doc.py
        write_doc.py
        sql_query.py
        create_table.py
  tests/                     # pytest, hits a real Postgres
```

## Tools (v1)

| Tool | Purpose | Backed by |
|------|---------|-----------|
| `web_search` | General web search returning ranked, LLM-friendly results | Tavily |
| `http_get` | Fetch a specific URL (HTML → text) | `httpx` + `readability-lxml` |
| `calculator` | Safe arithmetic / unit conversions | Python `eval` over a restricted AST |
| `read_doc` | Read a file from a sandboxed workspace dir | local FS (`/var/wolfpaw/workspace/`) |
| `write_doc` | Write a file to the sandboxed workspace | local FS |
| `sql_query` | Read-only query against Wolfpaw's own Postgres (and any user-created tables) | `asyncpg` with `SET TRANSACTION READ ONLY` |
| `create_table` | Create a new table in a user-data schema (`user_data.*`) for storing structured info | `asyncpg`, DDL whitelisted |

`create_table` and `write_doc` give Wolfpaw a way to durably store things it learns/produces — important for the "general agent" goal.

## Postgres schema (v1)

- `threads(id, created_at, soul_version)`
- `messages(id, thread_id, role, content, metadata jsonb, created_at)` — conversational memory
- `plans(id, thread_id, query, query_embedding vector(1024), steps jsonb, final_answer, success bool, score int, error text, trace_id, created_at)` — procedural memory; IVFFlat index on `query_embedding`
- `tools(name, description, signature jsonb, embedding vector(1024))` — for retrieval-style tool selection (read-only in v1)
- `user_data` schema — sandboxed namespace where `create_table` / `sql_query` / `write_doc`-via-DB operate, kept separate from system tables

## Request flow (v1)

```
POST /chat {thread_id, message}        [auth: X-API-Key]
  → mint trace_id, log "request.start"
  → load conversational memory (last N msgs)
  → Triage (Haiku)
      → Quick path: Quick Agent loop → stream answer → log message
      → Complex path:
          → Planner: pulls top-K similar past plans from procedural memory
          → Executor: runs steps; SSE-streams "step.start/step.end" events
          → Post-Evaluator: scores, writes plan row
          → Stream final answer
  → Append assistant message
  → log "request.end" with totals
```

## Step typing inside the Executor

From the diagram:
- **Functional** (no model): SQL query, web search, tool call, calculation
- **Reasoning** (model): evaluating prior step results, formatting, assembling complex outputs
- **Evaluation** (model): "was the task completed? are required parts present?"

Planner emits step type alongside each step so the Executor knows whether to call a model.

## Observability / monitoring

End-to-end traceability is a v1 requirement, not a v2 nice-to-have.

**Logging shape**
- All logs are **structured JSON to stdout** (one event per line). systemd → CloudWatch Logs via the CloudWatch agent.
- Every log line includes:
  - `trace_id` — minted at `/chat` entry, threaded through every agent + tool call
  - `thread_id` — conversation
  - `agent` — `triage` | `quick` | `planner` | `executor` | `post_evaluator`
  - `step_id` — within executor
  - `event` — `request.start`, `agent.start`, `agent.end`, `tool.call`, `tool.result`, `step.start`, `step.end`, `model.call`, `request.end`, `error`
  - `model` (when applicable), `input_tokens`, `output_tokens`, `latency_ms`
  - `extra` (free-form jsonb)

**Querying**
- CloudWatch Logs Insights queries by `trace_id` reconstruct a full request flow (triage decision → plan → each step → eval → response).
- Saved Insights queries committed in `infra/dashboards/` so they're reproducible.

**Metrics & dashboards**
- CloudWatch **metric filters** convert JSON fields into metrics:
  - `wolfpaw.requests` (count, dimensioned by triage path: quick vs complex)
  - `wolfpaw.latency_ms` (p50/p95/p99 by agent)
  - `wolfpaw.tokens` (input/output, by model)
  - `wolfpaw.plan_score` (post-evaluator score histogram)
  - `wolfpaw.errors` (count by agent + error class)
- **CloudWatch Dashboard** in `infra/dashboards/wolfpaw.json` — single pane: request rate, latency by agent, token spend, plan-success rate, recent errors. Zero extra infra.
- **Online dashboard:** the CloudWatch dashboard supports public/shared URLs. If we want richer viz later, we can layer Grafana on top of CloudWatch as a data source — same underlying logs.

**Why structured logs, not OpenTelemetry, for v1**
OTel + X-Ray would give true distributed traces, but it's setup complexity we don't need yet — Wolfpaw is a single process. Structured logs with `trace_id` give us the same query power for ~10% of the integration cost. v2 can move to OTel if we outgrow Logs Insights.

## Deferred to v2

- Tool Creator agent (auto-creates new tools / databases beyond `create_table`)
- Plan Pre-Evaluator (sanity-check plan before execution)
- Skills store (stored procedures of how to get things done)
- Sleep Cycle cron (memory organization, summarization, plan-quality re-scoring)
- Entity / Summary / Knowledge-Base memory types from the Alake module
- JWT / Cognito auth for multi-user
- OpenTelemetry tracing if Logs Insights becomes limiting

## Open questions

- **Postgres location.** RDS (managed, separate cost) vs co-located on the EC2 instance (cheap, simpler, fine for solo dev v1). Lean co-located for now.
- **Anthropic vs Bedrock for model calls.** Direct Anthropic API is simpler; Bedrock would unify with the document-agent stack but adds a config layer. Lean direct Anthropic for v1.
- **Workspace dir for `read_doc`/`write_doc`.** Single shared dir, or per-thread sandbox? Per-thread is safer but limits cross-conversation continuity.

## Build order

1. `pyproject.toml`, FastAPI skeleton, `config.py`, `auth.py` (X-API-Key), `tracing.py`, health endpoint.
2. Postgres migration `001_init.sql`, `memory/db.py` pool.
3. Conversational memory read/write + a no-op `/chat` that just echoes and logs (verifies trace_id flow).
4. Static toolbox registry + the 7 tools above.
5. Quick Agent (Haiku + tools), wire to `/chat` for trivial queries.
6. Triage agent, route between Quick and Plan paths.
7. Planner (procedural memory retrieval, plan generation).
8. Executor (functional vs reasoning vs evaluation step handling, SSE events).
9. Post-Evaluator (scoring + plan persistence).
10. Soul file integration into all agent system prompts.
11. CloudWatch metric filters + dashboard JSON in `infra/dashboards/`.
12. systemd + Caddy deploy notes; minimal Terraform for EC2 + security group + IAM role for CloudWatch.
