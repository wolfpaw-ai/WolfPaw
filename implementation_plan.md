# Wolfpaw — Implementation Plan (v1)

Living document. Updated as decisions are made.

## Guiding principles

- **Agentic worker, not just an assistant.** Wolfpaw's bar is "gets real work done while you sleep" — not "answers questions in chat." That implies code execution, long-running tasks, real deliverables, and sub-agent delegation as core capabilities, not nice-to-haves.
- **General agent.** Wolfpaw is broadly useful, not a specialist. Architecture biases toward generality: pluggable tools, model tiering, retrieval-driven memory.
- **Plug-and-play for end users.** The hosted product asks for nothing beyond an email and a credit card. No API keys, no bot tokens, no install.
- **One codebase, two distributions.** Hosted SaaS and self-host OSS ship from the same repo. Differences are config, not separate forks.
- **Tread lightly.** Conservative defaults, transparent about what's happening, never surprises the user with destructive actions or surprise bills.
- **Multi-tenant from day one.** Every row keys on `user_id`. Self-host = single seeded user; hosted = many users. Same code path.

## Decisions locked in

1. **Datastore:** Postgres + `pgvector`. Oracle AI Database evaluated later for specific use cases.
2. **Hosting:** Wolfpaw Cloud runs on its own EC2 instance(s), possibly in a separate AWS account. RDS Postgres separate from the API instance for hosted; co-located acceptable for self-host.
3. **Repo:** Standalone, separate from `dmitris-fabulous`. Living in `wolfpaw/` here for now; will move to its own repo before public release.
4. **License:** TBD. Will be open-source-friendly; specific license deferred.
5. **Distribution:** Hosted SaaS (`wolfpaw.ai`) + self-host OSS, single codebase. Hosted-first build order; self-host packaging in v1.5.
6. **Framework:** Python 3.12 + FastAPI. Pydantic v2.
7. **Channels:**
   - **v1:** Web chat, Telegram (single shared `@WolfpawBot` for hosted; OSS users provision their own bot via BotFather)
   - **v1.5:** Email forwarding (`alice@wolfpaw.ai`, SES inbound, drafts-out to verified owner address only)
   - **v2:** Slack (OAuth workspace install)
8. **Auth:** Email + magic link (passwordless) + Google OAuth as v1. Plus per-user API keys for programmatic access. `X-API-Key` static header is dev-only.
9. **Web search:** Tavily.
10. **Embeddings:** Voyage AI `voyage-3` (1024 dims), behind a swap-friendly interface.
11. **Models (Anthropic SDK direct):**
    - Triage → Haiku 4.5
    - Quick Agent → Haiku 4.5 (with tools)
    - Planner → Sonnet 4.6 default; Opus 4.7 when Triage flags "ambitious" *and* user's tier permits
    - Executor reasoning steps → Sonnet 4.6
    - Post-Evaluator → Haiku 4.5
12. **Billing:** Stripe (Checkout + customer portal + webhooks). Tier specifics deferred — we set numbers after we have real usage data.
13. **Token metering:** Mandatory in the hot path for every model call from day one. Recorded per request, per agent, per model. Always-on regardless of subscription state.
14. **At cap:** Hard pause + one-click upgrade or opt-in overage. No silent overage. (Behavior locked; threshold numbers TBD.)
15. **`/usage` command:** First-class, available in every channel. Reports tokens in/out per model, estimated cost per model, sandbox-compute cost, and totals — for the current billing period and today.
16. **Code execution sandbox:** First-class v1 capability. Sandboxed Python environment per task, network egress restricted, time/memory/disk limits, compute time metered alongside tokens. Provider: E2B for hosted (purpose-built for agent code execution); Docker for self-host. Without this, Wolfpaw can't really *do* most things.
17. **Tasks as first-class objects:** Persistent units of work distinct from chat threads or single plans. Have status (pending/running/blocked/awaiting_user/completed/failed/cancelled), survive across days, can be paused/resumed, ping the user via their preferred channel when blocked or done. A thread can spawn many tasks; a task can have many plans over its life.
18. **Artifact production tools:** Wolfpaw produces real deliverables — `.xlsx`, `.pdf`, `.pptx`, charts as `.png`/`.svg`. Stored in user's workspace folder (Drive/Dropbox via OAuth, or Wolfpaw S3 prefix).
19. **Sub-agent delegation:** Planner can mark plan branches as parallelizable; executor spawns sub-tasks with allocated budget from the parent. Hard limits on depth (3 levels) and concurrency (5 sub-agents) per task.
20. **Framework choice:** Build the agent loop, planner, executor, all memory subsystems, prompt versioning, channel abstraction, and tool registry from scratch on the Anthropic SDK. No LangChain, no LangGraph in v1. Adopt **LangSmith** for LLM-specific observability (per-call tracing, replay, eval datasets). CloudWatch + structured logs continue to own ops-tier observability. Rationale, alternatives, and the named fallback (LangGraph for task checkpointing if persistence work blocks the timeline) in [docs/decisions/framework-choice.md](docs/decisions/framework-choice.md) and [docs/decisions/observability.md](docs/decisions/observability.md).
21. **Two persona inputs.** Every agent prompt is conditioned on both the **Soul File** (agent persona, shared across all users) and a per-user **User File** (the user's persona, preferences, working style, recurring constraints). Soul is global config; User File is a per-`user_id` record. Both are versioned and stamped on `threads` so procedural-memory retrieval can scope to "same persona × same user profile version."

## Architecture commitments

- **Pure web app.** Wolfpaw ships no native software to end users. The hosted product is web + Telegram + email. OS-level capabilities (local folders, browser sessions) come via integrations the user already has — Google Drive / Dropbox sync for files; eventually a browser extension for web sessions. No Wolfpaw-branded desktop app, no menu-bar utility, no native sync helper.
- **Browser automation, when it ships, is a v3+ browser extension** — not cloud-side headless, not a native helper. Same pattern as 1Password / Bitwarden: the extension lives in the user's actual browser and runs against their real sessions, while the cloud agent sends instructions over a secure channel. Cloud-side Playwright and OS-level browser-driving are explicitly off the roadmap.
- **One server-side runtime.** The hosted backend is the only thing we maintain. Self-host runs the same backend. OSS contributors hack on the same code. No separate Electron / native / mobile-native build to keep in sync.

## Stack

- **Web layer:** FastAPI with `StreamingResponse` for SSE; `uvicorn` behind Caddy.
- **DB:** Postgres (RDS for hosted) + `pgvector`. `asyncpg`. Raw SQL via numbered migration files.
- **Background jobs:** `arq` (Redis-backed) for cost notifications, scheduled tasks, email dispatch, Telegram message processing.
- **Cache / queue:** Redis (ElastiCache for hosted; local for self-host).
- **Object storage:** S3 for raw inbound emails, file uploads, plan artifacts.
- **Telegram:** webhook on `/channels/telegram/webhook`, dispatches into the same chat pipeline as web.
- **Email:** SES inbound → S3 → SES event Lambda → `POST /channels/email/inbound` (with shared secret) → chat pipeline.
- **Stripe:** webhook on `/billing/webhook`.
- **Logging (ops):** structured JSON to stdout → CloudWatch Logs. `trace_id` threaded everywhere.
- **LLM observability:** LangSmith for per-call tracing, replay, eval datasets. Gated on `LANGSMITH_ENABLED`. See [docs/decisions/observability.md](docs/decisions/observability.md).
- **Secrets:** AWS Secrets Manager (hosted) / `.env` (self-host).

## Repo layout (in `wolfpaw/` for now)

```
wolfpaw/
  pyproject.toml
  README.md
  LICENSE                            # TBD
  spec.md
  implementation_plan.md
  soul.md
  WolfPaw_00.pdf                     # canonical architecture diagram
  docs/
    uml_class_diagram.md             # anticipated class structure (Mermaid)
    decisions/
      framework-choice.md
      observability.md
  alake_memory_manager.py            # reference (course material)
  alake_toolbox.py                   # reference (course material)
  migrations/
    001_init.sql                     # incl. message_embeddings, thread_summaries
    002_billing.sql
    003_channels.sql
    004_seed_skills.sql              # v1 starter skill set (user_id = NULL rows)
  src/wolfpaw/
    config.py                        # env, model IDs, feature flags
    api.py                           # FastAPI app
    deps.py                          # FastAPI dependency wiring
    tracing.py                       # trace_id, structured JSON logger
    auth/
      __init__.py
      magic_link.py                  # passwordless email auth
      oauth_google.py                # Google OAuth
      api_keys.py                    # per-user API keys
      middleware.py                  # request → user resolution
    schemas.py                       # Plan, Step, TriageResult, etc.
    soul.py                          # loads soul.md
    user_profile.py                  # loads/saves per-user User File
    agents/
      triage.py
      quick.py
      planner.py
      executor.py
      post_evaluator.py
      pre_evaluator.py               # v2 — plan pre-check (incl. "is it better than past plans?")
    memory/
      db.py                          # asyncpg pool
      conversational.py              # fetch_recent / fetch_summaries / search_relevant; tiered compaction
      procedural.py                  # recipe-box: description + ingredients + steps + score
      skills.py                      # v1 retrieval + seeded starter set; v2 auto-emission by post-evaluator
      seed_skills.py                 # loads the v1 starter set on first boot
    toolbox/
      registry.py
      tools/
        web_search.py                # Tavily
        http_get.py
        calculator.py
        read_doc.py                  # reads through Storage
        write_doc.py                 # writes through Storage; collision → awaiting_user
        sql_query.py
        create_table.py
        ask_user.py                  # HITL: pauses task, dispatches question via originating channel
    storage/
      base.py                        # Storage ABC
      s3.py                          # hosted
      local.py                       # self-host default; also MinIO target
      drive.py                       # v2 OAuth target
      dropbox.py                     # v2 OAuth target
      signing.py                     # pre-signed URL issuance + server-side prefix scoping
    channels/
      __init__.py                    # Channel ABC
      web.py                         # /chat SSE endpoint
      telegram.py                    # webhook + bot client
      email.py                       # SES inbound dispatcher (v1.5)
      slack.py                       # workspace app (v2)
    billing/
      stripe_client.py
      webhook.py                     # Stripe webhook handler
      tiers.py                       # tier definitions, limits, prices
      cost_notifications.py
    metering/
      pricing.py                     # model_prices table accessor
      recorder.py                    # writes token_usage rows
      enforcer.py                    # checks cap before model calls
      summarizer.py                  # rolls token_usage → usage_summaries
      prompt_versions.py             # prompt_versions accessor + bump helper
      langsmith_client.py            # LangSmith trace forwarding (gated on LANGSMITH_ENABLED)
    workers/
      arq_app.py                     # arq worker entrypoint
      jobs/                          # scheduled tasks, notifications, compact_thread
        compact_thread.py            # rolls verbatim msgs → level-1 → level-2 summaries
  infra/
    terraform/                       # EC2, RDS, ElastiCache, SES, etc.
    dashboards/
      wolfpaw.json                   # CloudWatch dashboard
      insights/                      # saved Logs Insights queries
    deploy/
      systemd/                       # unit files
      caddy/                         # Caddyfile
  tests/
    test_*.py
```

## Postgres schema (v1)

### Identity & billing

- `users(id, email, email_verified, display_name, created_at)`
- `user_profiles(user_id, version, persona_md, preferences jsonb, timezone, updated_at)` — the "User File" from the diagram. `persona_md` is free-form Markdown the user (or Wolfpaw, with permission) edits; `preferences` is structured (preferred channel, quiet hours, formality, units, comm style, recurring constraints). Loaded into every agent's system prompt for that user. Versioned so we can stamp `threads.user_profile_version` and scope procedural-memory retrieval.
- `user_auth_methods(user_id, method enum, identifier, secret_hash, ...)` — magic link, Google OAuth, etc.
- `api_keys(id, user_id, prefix, hash, name, last_used_at, created_at)`
- `subscriptions(user_id, stripe_customer_id, stripe_subscription_id, tier enum, status, current_period_start, current_period_end, allowance_cents, overage_authorized bool, overage_cap_cents nullable)`
- `tier_limits(tier, allowance_cents, channels_allowed text[], opus_allowed bool, scheduled_tasks_allowed bool)` — seeded, code-managed
- `model_prices(model_id, input_per_mtok_cents, output_per_mtok_cents, cache_read_per_mtok_cents, cache_write_per_mtok_cents, effective_from, effective_to)` — versioned

### Memory

- `threads(id, user_id, channel enum, created_at, soul_version, user_profile_version)`
- `messages(id, thread_id, role, content, metadata jsonb, created_at)`
- `message_embeddings(message_id FK, embedding vector(1024))` — Voyage `voyage-3` embedding per message, written on append. IVFFlat index; lookups scoped per thread via `WHERE thread_id = ?` (cross-thread is a v2 opt-in).
- `thread_summaries(id, thread_id, level int, summary_md, range_start_message_id FK, range_end_message_id FK, created_at)` — append-only tiered compression of older messages (see [Conversational memory](#conversational-memory)). `level = 1` = summary of verbatim messages; `level = 2` = summary of level-1 summaries.
- `plans(id, user_id, thread_id, task_id nullable, query, query_embedding vector(1024), steps jsonb, final_answer, success bool, score int, error text, trace_id, created_at)` — IVFFlat index on `query_embedding`. The "recipe-box" entries: every row carries a `description` (what the user wanted), `ingredients` (tools used + inputs), and `steps` (the executed plan), all derivable from `query` + `steps`. Stored here regardless of success; retrieval filters by `score`. Each entry in `steps` carries an optional `parallel_group: int | null` — steps sharing a group ID in the same plan run concurrently in the executor; absence means sequential.
- `skills(id, user_id, name, description, ingredients jsonb, steps jsonb, source_plan_id FK, score, created_at)` — **v2.** Auto-emitted by the Post-Evaluator when a plan scores highly and looks reusable. A skill is a generalized procedure ("how to draft a vendor-comparison spreadsheet") that the planner can pull instead of re-deriving from raw plans.
- `tools(name, description, signature jsonb, embedding vector(1024))`
- `user_data` schema — sandboxed namespace where `create_table` / `sql_query` / `write_doc`-via-DB operate. Per-user schema (`user_data_42.*`) for clean isolation.

### Tasks & workspace

- `tasks(id, user_id, parent_task_id nullable, title, description, status enum, current_plan_id, budget_cents nullable, spent_cents, blocking_reason text nullable, channel_for_completion enum, schedule_pattern text nullable, created_at, started_at, completed_at, last_active_at)` — `status` ∈ {`pending`, `running`, `blocked`, `awaiting_user`, `completed`, `failed`, `cancelled`}
- `task_events(id, task_id, event_type, content jsonb, created_at)` — append-only log of state transitions, agent updates, user inputs
- `workspace_files(id, user_id, task_id nullable, source enum, filename, mime_type, storage_url, size_bytes, version int, supersedes_id FK nullable, sha256, created_at)` — every file in the user's workspace. `source` ∈ {`user_upload`, `agent_output`}; `task_id` is set for `agent_output` rows. `version` + `supersedes_id` track overwrite history (the "overwrite with confirmation" flow appends a new row pointing back to the prior version rather than mutating). Replaces what was previously called `artifacts`.
- `sandboxes(id, task_id, provider, external_id, status, started_at, terminated_at, compute_seconds, cost_cents)` — one sandbox per active task, torn down on task pause/complete

### Channels

- `channel_links(id, user_id, channel enum, external_id, external_username, metadata jsonb, created_at)` — Telegram user IDs, Slack workspace IDs, etc.
- `email_aliases(user_id, alias)` — `alice@wolfpaw.ai` → `user_id=42`
- `verified_owner_emails(user_id, email, verified_at)` — drafts-out destinations

### Metering

- `token_usage(id, user_id, task_id nullable, trace_id, request_id, agent enum, model, prompt_version_id FK, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_cents int, created_at)` — partitioned by `created_at` month
- `compute_usage(id, user_id, task_id, sandbox_id, compute_seconds, memory_gb_seconds, cost_cents int, created_at)` — sandbox time, separate dimension from tokens
- `usage_summaries(user_id, period_start, period_end, total_cost_cents, by_agent jsonb, by_model jsonb, compute_cost_cents)` — rolled up nightly
- `cost_notifications(user_id, period_start, threshold_pct, sent_at)` — dedup
- `prompt_versions(id, agent enum, version_label, content_hash, content_template jsonb, created_at)` — versioned agent prompts; every `token_usage` row references the version active at call time. See [docs/decisions/observability.md](docs/decisions/observability.md).

## Tools (v1)

### Information & data
| Tool | Purpose | Backed by |
|------|---------|-----------|
| `web_search` | LLM-friendly ranked web results | Tavily |
| `http_get` | Fetch URL → readable text | `httpx` + `readability-lxml` |
| `calculator` | Safe arithmetic / unit conversions | restricted-AST eval |
| `sql_query` | Read-only against user's `user_data_<id>` schema | `asyncpg` with `READ ONLY` tx |
| `create_table` | DDL into `user_data_<id>` schema, whitelisted patterns | `asyncpg` |

### Files & workspace
| Tool | Purpose | Backed by |
|------|---------|-----------|
| `read_doc` | Read file from per-user workspace | local FS / S3 / Drive / Dropbox |
| `write_doc` | Write text/markdown to per-user workspace | local FS / S3 / Drive / Dropbox |

### Code execution (sandbox)
| Tool | Purpose | Backed by |
|------|---------|-----------|
| `run_python` | Execute Python in the task's sandbox; returns stdout, stderr, return values, files written | E2B (hosted) / Docker (self-host) |
| `install_package` | `pip install` into sandbox | sandbox runtime |
| `sandbox_read_file` / `sandbox_write_file` | I/O against the sandbox FS | sandbox runtime |

### Artifact production
| Tool | Purpose | Backed by |
|------|---------|-----------|
| `create_spreadsheet` | `.xlsx` with sheets, formulas, formatting | `openpyxl` (in sandbox) |
| `create_pdf` | HTML/Markdown → `.pdf` | `weasyprint` (in sandbox) |
| `create_chart` | Chart from data → `.png`/`.svg` | `matplotlib` (in sandbox) |
| `create_slides` | `.pptx` decks | `python-pptx` (in sandbox) |

Artifacts land in the user's workspace folder and are recorded as `workspace_files` rows with `source = 'agent_output'` and `task_id` set to the originating task.

`write_doc` and `create_table` give Wolfpaw durable structured storage. Per-user schema/workspace keeps tenants isolated.

### Human-in-the-loop
| Tool | Purpose | Backed by |
|------|---------|-----------|
| `ask_user` | Pause the current task, ask the user a question via the channel the task originated from, resume on reply | task lifecycle + originating channel adapter |

`ask_user` is how Wolfpaw "asks before destructive actions" — without it the soul file's tread-lightly promise is aspirational. The agent calls `ask_user(question, options?, urgency?)` mid-plan; the executor transitions the parent task to `awaiting_user`, writes a `task_events` row of type `user_question`, and dispatches the question via the channel from `tasks.channel_for_completion`. The user's reply (captured by the channel adapter) lands in `task_events` as `user_answer` and resumes the paused step with the answer as the tool's return value. Reuses the existing `awaiting_user` machinery — no new state, no new notification path. Build placement is step 15 (needs task lifecycle).

## Code execution sandbox

A sandboxed Python environment is the difference between a chatbot and a worker. Every task that needs computation gets its own sandbox.

**Provider**
- **Hosted:** [E2B](https://e2b.dev) — purpose-built for AI agent code execution, handles isolation/networking/lifecycle, pay-per-second.
- **Self-host:** Docker container with a locked-down image. Slower spin-up but no third-party dependency.
- Abstracted behind a `Sandbox` interface so the rest of the code is provider-agnostic.

**Lifecycle**
- One sandbox per active task. Created on first `run_python` call. Torn down on task pause/complete or after idle timeout (15 min).
- State persists across calls within the same task — variables, installed packages, files all stick around.
- `sandboxes` table tracks lifetime + compute-seconds for metering.

**Security**
- Network egress disabled by default. Agent can request specific URL allowlists per task; the planner has to declare them up front.
- CPU time limit per execution (default 60s, max 5 min).
- Memory limit (default 1 GB, max 4 GB).
- Disk limit (default 1 GB, ephemeral — wiped on teardown).
- No shell access, no privileged operations, no kernel features.
- **Credential vault.** Sandboxes never hold raw API keys. When sandboxed code makes an outbound HTTP request, it routes through a Wolfpaw proxy that injects credentials at request time and enforces per-agent rate limits and access policy. Pattern adapted from NanoClaw. Effects: a malicious tool call or prompt injection that runs `os.environ`, scans files, or greps the disk finds nothing useful; keys can be rotated without rebuilding sandboxes; every credentialed request is logged with which task / agent / tool initiated it for audit.
  - Implemented as `wolfpaw/sandbox/proxy.py` — a small HTTP proxy each sandbox is configured to use as its egress.
  - Per-task policy: which credentials are reachable (Tavily, Anthropic, user OAuth tokens for Drive/Calendar/etc), which destinations are allowed, per-credential rate limits.
  - User OAuth tokens (Drive, Dropbox, Gmail, Calendar, Notion) flow through the same vault — sandboxed code never sees the user's tokens directly.

**Metering**
- Compute-seconds tracked in `compute_usage`, costed per second.
- Shows up in `/usage` as a separate line from tokens.
- Counted against user's allowance like inference is.

## Workspace (per-user files)

Wolfpaw needs a place where the user can drop files for the agent to read, and where the agent's deliverables land for the user to download. Every user gets a durable, isolated workspace — the same workspace whether they arrive via web, Telegram, or email.

**Storage layout**
- Per-user prefix in a single bucket: `s3://wolfpaw-workspace/<user_id>/...`. The prefix *is* the multi-tenant boundary — same shape as every other table.
- **Flat namespace in v1.** No nested folders in the UI. The S3 key is still hierarchical underneath, so introducing folders in v2 is a frontend change, not a data migration.
- One row per file in `workspace_files` (see schema). User uploads and agent outputs sit in the same listing.

**`Storage` interface**
- Abstract base in `src/wolfpaw/storage/base.py`. Adapters: `s3.py` (hosted), `local.py` (self-host default), `drive.py` / `dropbox.py` (v2 OAuth integrations).
- Self-host runs against `local.py` out of the box; users who want S3 semantics on their own infra can point the adapter at MinIO via the same code path. No bytes are ever stored in Postgres.
- Drive / Dropbox (v2) become *additional* storage targets the user can pick per file or per task — not a replacement for the Wolfpaw workspace.

**Frontend ↔ API**
- Uploads and downloads use **short-lived pre-signed URLs** issued by the API. Bytes never transit EC2.
- `POST /workspace/upload-url` → API validates the filename, issues a URL scoped to `s3://wolfpaw-workspace/<user_id>/<filename>` with a 5-minute TTL and a max-size header. Client PUTs directly to S3. On client success, `POST /workspace/files` registers the `workspace_files` row.
- `GET /workspace/files` lists the user's files (read from Postgres, not S3). `GET /workspace/files/<id>/download-url` issues a signed download URL with a 5-minute TTL.
- The signing endpoint validates the requested key starts with `<authenticated_user_id>/` server-side; the bucket policy denies cross-prefix access defense-in-depth.

**Sandbox interaction**
- Sandboxes do **not** talk to S3 directly. When a tool needs a file, the executor pre-stages it into the sandbox FS at a known path (e.g. `/workspace/<filename>`); when a tool writes a file, the executor uploads the result back into the user's prefix on tool completion. This keeps the sandbox's network policy locked down (the credential proxy already restricts egress) and gives a clear audit trail of which task touched which file.
- The agent never sees raw S3 URLs or signed URLs in its tool I/O — only logical filenames in `workspace_files`. Keeps prompts insensitive to storage backend and avoids leaking signing tokens into model context.

**Conflict resolution: overwrite with confirmation**
- When an agent tool would write to a filename that already exists in `workspace_files`, the executor pauses the task with `status = awaiting_user` and emits a `task_events` row of type `overwrite_request` containing the existing `workspace_files.id`, the incoming bytes' SHA-256, and a diff summary if it's a text-like file.
- The user gets pinged on their preferred channel with a yes / no / rename prompt (handled by the standard `awaiting_user` notification path; no new channel surface). On approval, the new row is inserted with `version = prior + 1` and `supersedes_id = prior.id`; the prior row stays for history. On rename, the user supplies a new filename. On no, the agent step records the decision and continues without writing.
- Fits the existing "ask before destructive actions" posture in the soul file. Uses task lifecycle that's already being built — no new state machine.

**Quotas + retention**
- Per-user storage quota enforced at upload time against `sum(size_bytes) WHERE user_id = ?`. Default quota lives in `tier_limits` (column added when tiers are set; placeholder for `dev` tier is generous).
- No automatic deletion of agent outputs in v1 — the user owns their files. Lifecycle policies (e.g. expire superseded versions after 90 days) are a v2 concern once we see real usage shapes.
- S3 bucket has versioning enabled at the AWS level as a belt-and-braces measure against the application layer; cost is negligible at expected file volumes.

**Build placement.** Workspace ships in the same step as the read/write tools — it's the substrate they need. The `Storage` ABC + S3 adapter + signing endpoints + `workspace_files` table land at **build step 7** alongside `read_doc` / `write_doc`. Sandbox pre-staging integrates at step 8.

## Tasks (long-running work)

A `Task` is a persistent unit of work distinct from a chat thread or a single plan. Tasks are how Wolfpaw "works while you sleep."

**Lifecycle**
```
pending → running → (blocked | awaiting_user) → running → completed
                                                        ↘ failed | cancelled
```

- **pending:** queued, not yet started.
- **running:** an agent is actively working on it (or just finished a plan and is choosing the next one).
- **blocked:** external dependency stalled (rate limit, downstream service, missing data the agent can't get).
- **awaiting_user:** needs the user's input or approval; pings them on their preferred channel.
- **completed:** all goals met; final artifacts attached; user notified.
- **failed:** unrecoverable error. Includes diagnosis of why.
- **cancelled:** user stopped it.

**Plans are subordinate to tasks.** A task can produce many plans over its life — initial attempt, retry after blocked, sub-plans for branches. Procedural memory stores plans (with their outcome scores) the same way as today; tasks add the persistence layer above them.

**Worker process**
- `arq` worker continuously picks up runnable tasks, runs the next plan step, persists state, repeats.
- Task spend tracked against `budget_cents` (defaults to remaining period allowance; user can cap a single task lower).
- Hard system ceiling: no single task exceeds $50 by default without user authorization (separate from the system-wide overage cap).

**User-facing**
- Web app shows a task list with status, last activity, spend, artifacts.
- Channels can return `/tasks` slash command for a quick text summary.
- Notifications on `awaiting_user` and `completed` via user's preferred channel.

**How tasks are created**
- Agent decides: if the user asks for something that needs more than a single chat turn or that produces a real deliverable, the planner creates a task and starts working. Quick lookups remain in-thread.
- User can also explicitly create a task ("Wolfpaw, take this on as a project: ...").
- Recurring tasks via `schedule_pattern` (cron-style) — tied into the v2 Sleep Cycle.

## Sub-agent delegation

Plans can mark branches as parallelizable. The executor spawns a sub-task per branch with its own sandbox, its own thread, and a budget allocated from the parent.

- **Depth limit:** 3 levels (sub-tasks of sub-tasks of root). Beyond that, the planner has to flatten.
- **Concurrency limit:** 5 sub-tasks running concurrently per root.
- **Budget:** parent allocates `budget_cents` per sub-task at spawn time. Sub-task spend rolls up to parent for `usage_summaries`.
- **Synthesis:** parent task waits for all sub-tasks to reach a terminal state, then runs a synthesis step (a reasoning step that combines the outputs).
- **Failure handling:** if a sub-task fails, parent decides: retry with a different plan, drop the branch and continue, or fail the whole task. Planner emits this policy at spawn time.

The schema piece is `tasks.parent_task_id`, which already lets us query "all sub-tasks of root X" and roll up costs. The execution piece is the worker spawning multiple `arq` jobs and coordinating their results.

## Channel abstraction

```python
class Channel(ABC):
    name: str
    async def receive(self, payload: dict) -> InboundMessage: ...
    async def send(self, user_id: UUID, content: str, **kwargs) -> None: ...
    def supports_streaming(self) -> bool: ...
```

Channels normalize inbound messages into a common shape. Before dispatching to the agent pipeline, an inbound message goes through a **slash-command interceptor** — `/usage`, `/help`, `/reset`, etc. — that handles the message directly without burning model tokens. If no command matches, the message goes to the pipeline as `(user_id, thread_id, content, channel_name)`.

### Slash commands (built-in, model-free)

| Command | Behavior |
|---|---|
| `/usage` | Current period + today, by model, tokens in/out + cost + sandbox compute. See "/usage" section below. |
| `/usage today` | Just today. |
| `/usage month` | Current billing period, with by-agent breakdown. |
| `/usage all` | Lifetime totals. |
| `/tasks` | List of active + recent tasks with status, spend, artifact count. |
| `/task <id>` | Detail on a specific task (current step, last activity, blocking reason if any). |
| `/cancel <task_id>` | Cancel a running task. |
| `/help` | Lists available commands and channels. |
| `/reset` | Starts a new conversation thread. (Doesn't delete memory — just opens a fresh thread.) |

Commands are dispatched in `channels/commands.py`, shared across all channel adapters. Output formatting per channel: web renders markdown tables; Telegram uses code blocks; email uses plain text.

### Telegram specifics

- One shared `@WolfpawBot` for hosted; bot token in Secrets Manager.
- Onboarding link: `https://t.me/WolfpawBot?start=link_<token>` from web app; `/start link_<token>` triggers `channel_links` write.
- Webhook receives all updates → enqueue to `arq` for processing → response back via Bot API.
- OSS: user supplies `TELEGRAM_BOT_TOKEN` env var; same code path.

### Email specifics (v1.5)

- MX records for `wolfpaw.ai` → SES inbound.
- Receipt rule writes raw `.eml` to S3, triggers Lambda.
- Lambda parses and calls back into Wolfpaw API at `/channels/email/inbound` with shared secret.
- Wolfpaw maps `to:` address → `user_id` via `email_aliases`, dispatches into chat pipeline.
- Outbound: agent can only send to `verified_owner_emails` for that user. Hard-coded constraint, not promptable.
- Forwarded email body treated as **untrusted user-provided text**, not as instructions. Built into the agent system prompt; subject to defense-in-depth review during build.

## Token metering

Metering is in the hot path. Every model call goes through a wrapper that records to all three observability layers:

```python
async def call_model(user_id, agent, model, messages, prompt_version_id, ...) -> ModelResponse:
    enforcer.check_can_spend(user_id)            # raises if at cap
    async with langsmith_client.trace(            # gated on LANGSMITH_ENABLED
        agent=agent, model=model, trace_id=trace_id,
        prompt_version_id=prompt_version_id, user_id=user_id, task_id=task_id,
    ):
        response = await anthropic.messages.create(...)
    cost_cents = pricing.compute(model, response.usage)
    await recorder.write(
        user_id, agent, model, prompt_version_id,
        response.usage, cost_cents, trace_id,
    )
    cost_notifications.maybe_send(user_id, cost_cents)
    return response
```

`prompt_version_id` is resolved by the caller from the active prompt template for that agent (see `metering/prompt_versions.py`). Recording it on every `token_usage` row is what lets procedural memory scope plan retrieval to "produced under prompt version X or later" — important once any agent's prompt has materially changed.

**Enforcer** checks `usage_summaries` for the current period. If `total_cost_cents >= allowance_cents` and overage not authorized → raise `OverCap`. Caller surfaces a "you've hit your cap" message and a link.

**Recorder** writes one row per model call. Async batch flush is fine — at-most-a-few-seconds delay is acceptable, but the row must persist before we promise the user the answer.

**Pricing** is computed from the `model_prices` table, joined on the model ID and `created_at` so historical rows compute against the price that was in effect.

**Estimation before long jobs.** Planner can estimate plan cost before execution (sum step estimates by step type). If estimate would push user over allowance, refuse and explain.

## `/usage` command

First-class slash command, available in every channel. Useful both as a user feature and as a dev tool — while building Wolfpaw, you'll want to see your own spend at a glance without leaving the channel you're testing in.

**Default output (`/usage`):**
```
Current period (May 1 – May 31, 2026)
  haiku-4-5     1,243,000 in /  340,000 out    $1.24 + $1.70 =  $2.94
  sonnet-4-6      284,000 in /   95,000 out    $0.85 + $1.42 =  $2.27
  voyage-3         45,000                                       $0.02
                                                       Total:   $5.23

Today (May 5)
  haiku-4-5        23,000 in /    8,000 out    $0.06
                                                       Total:   $0.06
```

**With `/usage month`:** adds an "**By agent**" section (triage / quick / planner / executor / post_evaluator) so you can see where the money is actually going. Critical during dev — if planner is eating 80% of spend, you know where to optimize.

**Implementation:**
- `metering/usage_report.py` builds the report from `token_usage` + `model_prices`.
- Cached for 30 seconds so users mashing `/usage` doesn't thrash Postgres.
- Channel adapters render the same `UsageReport` dataclass into channel-specific formatting.
- Web also has a richer dashboard view with charts; `/usage` in web returns the same compact text plus a link to the dashboard.

**Cost computation honesty.** Costs are computed from `model_prices` for the model+date of each call, not from a single snapshot. If we change prices mid-period, historical rows remain priced as they were at the time. The total is what we charged the user, not an estimate.

## Cost notifications

- 50% / 80% / 100% of allowance, sent through every connected channel (email always; Telegram if linked; web in-app banner).
- 100% notification includes upgrade link + overage opt-in toggle.
- Opt-in daily summary (one email/day with spend, plan count, top tools used).
- Dedup via `cost_notifications` table.
- Sent via `arq` jobs scheduled by the recorder when threshold crossed.

## Cap enforcement behavior

- **At allowance and overage off:** API returns `402 Payment Required`. Web UI shows paywall modal. Telegram bot replies with the cap message + upgrade link. New requests blocked until upgrade or overage opt-in. **In-flight requests** finish (we don't strand a user mid-stream).
- **Overage on:** Allow up to `overage_cap_cents`. Charged via Stripe usage-based pricing at end of period. 80% overage notification sent.
- **Hard ceiling:** Even with overage, no single user can exceed a system-wide hard ceiling (e.g. $500/mo) without manual approval — protects against runaway loops.

## Subscription tiers

**Specifics deferred.** Tier names, dollar amounts, allowances, and channel gating are placeholders until we have real usage data from a working app. The schema (`subscriptions`, `tier_limits`, `model_prices`, `token_usage`, `usage_summaries`) is built day one so we can switch to any tier shape without migrations later.

What is locked in:
- Flat-rate plans with an included inference allowance, denominated in **dollars**, not tokens.
- **Hard pause at cap.** No silent overage. New requests blocked; in-flight requests finish.
- **Opt-in overage** with a per-user cap and a system-wide hard ceiling.
- **Cost notifications** at 50% / 80% / 100% of allowance, plus opt-in daily summary, delivered through every connected channel.
- **Self-host:** tier = `self_host`, no enforcement, but token usage is still recorded so the user can see their own spend.

Until tiers are decided, every authenticated user runs as `tier = "dev"` with metering on but no enforcement — same shape as self-host. Lets us build the app and accumulate real usage telemetry before pricing.

## Soul file & User file integration

Every agent prompt is conditioned on **two** persona blocks:

- **Soul File (`soul.md`).** Agent persona — global, shared across all users, loaded once at startup. Versioned via `threads.soul_version` so we know which Wolfpaw was talking when a thread started. A plan that worked under one Wolfpaw persona may not under a different one.
- **User File (`user_profiles` row).** Per-user persona/preferences — name, timezone, preferred channel, formality, units, quiet hours, recurring constraints, free-form Markdown the user maintains about themselves. Loaded per-request from `user_profiles` for the authenticated `user_id`, prepended to the agent prompt after Soul. Versioned via `threads.user_profile_version` so procedural-memory retrieval can scope to "same persona × same user profile version."

The User File is editable by the user at any time (web UI text editor + structured preferences form) and may be appended-to by Wolfpaw with the user's permission ("I'll remember that you prefer kilometers"). The Triage Agent reads it on every turn to set tone and routing; downstream agents inherit it through the system prompt.

## Conversational memory

Three tiers, all **per-thread** in v1 (cross-thread retrieval is a v2 user-toggle).

- **Verbatim recent window.** The most recent ~20 messages of a thread are loaded verbatim into every agent prompt.
- **Tiered summaries.** Older messages are compacted into rolling level-1 summaries (windows of ~20 messages); when level-1 summaries accumulate past a threshold, they fold into level-2 summary-of-summaries. Two levels in v1; deeper levels deferred.
- **Vector recall.** Every message is embedded with Voyage `voyage-3` on append into `message_embeddings`. The **Planner** issues a per-thread top-k lookup (k≈5) and includes the hits alongside the verbatim window + summaries. Retrieval lives at the Planner deliberately — running it at Triage (Haiku, every turn) would inflate the cheapest path; the Planner only runs on non-trivial requests.

**Who sees what:**
- Triage Agent + Quick Agent: verbatim window + summaries (no vector lookup).
- Planning Agent: verbatim window + summaries + vector recall hits.

**Compaction worker.** An `arq` job (`workers/jobs/compact_thread.py`) summarizes the oldest unsummarized window when a thread crosses N messages, and emits a level-2 summary when level-1 count crosses M. Also runs on explicit thread-close events.

**`ConversationalMemory` interface:**
- `fetch_recent(thread_id, n)` — verbatim tail.
- `fetch_summaries(thread_id)` — all level-1 + level-2 summaries for the thread.
- `search_relevant(thread_id, query_embedding, k)` — per-thread vector recall.
- `append(message)` — writes the message and enqueues an embedding job.

## Seed skills

A small starter set of generalized skills ships in v1 — three to five hand-written exemplars that:

- Give the Planner something to retrieve from on day one (the `SkillsMemory.search_by_task` path is v1; **auto-emission** by the Post-Evaluator remains v2).
- Establish the shape future auto-emitted skills should match.
- Double as marketing-ready proof of what Wolfpaw does out of the box.

Initial set (subject to refinement during build):

- **Vendor-comparison spreadsheet** — research N vendors on user-supplied criteria, normalize attributes, emit `.xlsx` with columns + recommendation.
- **Research one-pager** — read a paper/report (URL or upload), produce a single-page `.pdf` with key findings, methodology, and limitations.
- **Newsletter digest** — ingest forwarded newsletters over a date range, group by theme, produce a Markdown digest.
- **Receipt → ledger row** — extract date, vendor, amount, tax from a receipt image/PDF, append to a sandboxed SQL table the user maintains.
- **Inventory snapshot** — read a user-supplied list of items, look up current data per item, emit `.xlsx` snapshot with deltas vs prior snapshot.

Seeded skills land via a Python seeder (or `migrations/004_seed_skills.sql`) on first boot. Seeded rows have `user_id = NULL`; the Planner's retrieval matches `WHERE user_id = ? OR user_id IS NULL`.

## Observability

End-to-end traceability is a v1 requirement. Three layers, each scoped to a different question:

| Layer | Tool | Question it answers |
|---|---|---|
| Operational metrics + logs | CloudWatch (structured JSON) | "Is the system healthy? Where's the latency? Are users hitting their cap?" |
| LLM-call detail | LangSmith | "What did the planner generate on request X? Replay it. Diff prompts. Score against a dataset." |
| Prompt versioning | Custom (`prompt_versions` table) | "Which prompt template produced this output? Did v8 outperform v7 across the procedural-memory dataset?" |

`trace_id` is the through-line. It's emitted on every structured log line, attached to every LangSmith trace, and recorded on every `token_usage` row alongside `prompt_version_id`. Pulling a `trace_id` out of a CloudWatch log lands you on the matching LangSmith trace and the relevant `token_usage` rows.

Full design rationale and rejected alternatives in [docs/decisions/observability.md](docs/decisions/observability.md).

### Operational layer (CloudWatch)

**Logging shape**
- Structured JSON to stdout (one event per line). systemd → CloudWatch via the CloudWatch agent.
- Every line includes: `trace_id`, `user_id`, `thread_id`, `agent`, `step_id`, `event`, `model`, `prompt_version_id`, `input_tokens`, `output_tokens`, `latency_ms`, `extra` (jsonb).
- Events: `request.start`, `agent.start`, `agent.end`, `tool.call`, `tool.result`, `step.start`, `step.end`, `model.call`, `cap.exceeded`, `notification.sent`, `request.end`, `error`.

**Metrics**
- CloudWatch metric filters extract: `wolfpaw.requests` (count by tier × triage_path × channel), `wolfpaw.latency_ms` (p50/p95/p99 by agent), `wolfpaw.tokens` (input/output by model), `wolfpaw.cost_cents` (by tier), `wolfpaw.plan_score`, `wolfpaw.errors` (by agent × class), `wolfpaw.cap_pause` (count).

**Dashboards**
- CloudWatch Dashboard JSON in `infra/dashboards/wolfpaw.json` — single pane: request rate, latency, token spend, plan-success rate, cap-pause rate, errors. Public/shared URL for the "online dashboard."

### LLM-call layer (LangSmith)

- Every model call is wrapped in a LangSmith trace alongside the token-recorder write. Implementation in `metering/langsmith_client.py`.
- Trace tags: `trace_id`, `user_id`, `agent`, `model`, `prompt_version_id`, `task_id` (when applicable).
- LangSmith handles: per-call replay, prompt-version diffs, eval-dataset runs against historical prompts, dashboards for plan-success rate by prompt version.
- Gated by `LANGSMITH_ENABLED` config flag — defaults on in dev/private-beta, evaluated before public launch (third-party data handler; privacy disclosure required).

### Prompt-versioning layer (custom)

- `prompt_versions` table holds every version of every agent's prompt template, with `content_hash` for change detection.
- Bumping a prompt is intentional: edit the template, bump the version label, run an idempotent loader to insert the new row, deploy.
- Every `token_usage` row carries the active `prompt_version_id` so we can ask "what did planner v8 do that v7 didn't" in raw SQL or as a LangSmith eval dataset.
- Procedural memory respects this: plan retrieval can scope to plans produced by a given prompt version range, so we don't recommend old plans that ran under a meaningfully different planner.

### Why not OpenTelemetry for v1

OTel is the long-term-correct answer for cross-service tracing. v1 is a single FastAPI process — `trace_id` in structured JSON logs plus LangSmith for LLM-specific traces gives us the same query power at ~10% of the setup cost. Reconsider OTel when the system grows beyond one runtime.

## Deferred to v2+

- Tool Creator agent (auto-creates new tools beyond `create_table`)
- **Plan Pre-Evaluator.** Runs between planner and executor. Three checks: (1) will the plan achieve the objective? (2) can it be simplified? (3) **is it an improvement over what we found in procedural memory from past attempts?** On pass → executor; on fail → back to planner with the diagnosis as additional context. Deferred because v1 can ship without it — the planner consults procedural memory directly — but it tightens the loop once we have enough plan history to compare against.
- **Skills auto-emission + community marketplace** ("Pawhub"?). The `skills` table, the Planner's **retrieval** path, and a **seeded** starter set ship in **v1** (see [Seed skills](#seed-skills)). What stays in v2 is the Post-Evaluator's auto-emission of new skills from high-scoring reusable plans. The share/import marketplace flow remains v3.
- Sleep Cycle cron (memory organization, summarization, plan re-scoring, skill consolidation across users in the marketplace era)
- Entity / Summary / Knowledge-Base memory types
- OAuth integrations (v2):
  - **Notion** — read pages user shares; create pages. ~3 days, fast review.
  - **Google Calendar** (`calendar.events`) — read + create events. ~3 days, sensitive-tier verification (weeks).
  - **Microsoft Calendar** (`Calendars.ReadWrite`) — read + create events. ~5 days, easy verification.
  - **Google Drive** (`drive.file`) — read/edit/delete files in folders the user explicitly grants Wolfpaw access to. Serves as a "Wolfpaw folder" workspace via Drive desktop sync. ~3 days, sensitive-tier verification.
  - **Dropbox (App folder)** — sandboxed `/Apps/Wolfpaw/` folder, full read/edit/delete inside it. Same workspace use case as Drive; cleanest of the three. ~3 days, light verification, no audit.
  - **Gmail readonly** (`gmail.readonly`) — read only, never send/delete/modify. ~5 days code. Restricted-tier scope: build + test under OAuth test mode (100-user cap with unverified warning) for dev and private beta; CASA audit ($15k-$75k/year) timed to public launch when revenue justifies it. Drafts still flow via Wolfpaw → owner-email pattern, never via Gmail API.
- OAuth integrations explicitly excluded:
  - **Gmail send / compose / modify / delete (any write scope):** never, by policy. Wolfpaw never holds the capability to send, delete, or modify mail in a user's Gmail account.
  - **Microsoft Outlook mail read:** if/when there's clear demand, do alongside a Gmail policy refresh. v3+.
  - **OneDrive (workspace folder):** v3 — same pattern as Dropbox/Drive but lower demand than either.
  - **iCloud Drive:** no public API for hosted services. Would require a native Mac app. Off the roadmap.
  - **GitHub:** wrong audience for hosted Wolfpaw. Self-host users can wire their own.
  - **Plaid / financial:** v3+ pending revenue and compliance investment.
- Slack channel
- Voice / iMessage / WhatsApp channels
- Mobile app
- Scheduled / proactive tasks ("heartbeats")
- OpenTelemetry tracing
- Self-host packaging polish (Docker compose, install script, docs)

## Open questions

- **Tier specifics.** Deferred — set after real usage data accumulates. Schema is ready when we are.
- **Domain.** `wolfpaw.ai` confirmed? Worth checking `wolfpaw.com` / `wolfpaw.app` availability — `.com` deliverability is materially better for outbound email.
- **Postgres location for hosted.** RDS (managed, ~$15/mo for db.t4g.micro) vs co-located on the EC2 instance (cheaper, recoverable from snapshot). Lean RDS for hosted, co-located for self-host.
- **Anthropic vs Bedrock.** Direct Anthropic API for v1 (simpler, cleaner usage data). Reconsider Bedrock if AWS Activate credits move it.
- **Web app stack.** React + Vite (matching dmitris-fabulous frontend), or something else? Lean React + Vite — known stack, fast.
- **Mobile app.** Out of scope for v1, but consider whether the Telegram bot is *good enough* as a mobile experience for the first year. (Probably yes.)

## Build order

1. ✅ **completed** — **Foundation.** `pyproject.toml`, FastAPI skeleton, `config.py`, `tracing.py`, health endpoint.
2. ✅ **completed** — **Database.** `001_init.sql` (users, user_profiles, threads, messages, message_embeddings, thread_summaries, plans, skills, tools, user_data schema, tasks, task_events, workspace_files, sandboxes, token_usage, compute_usage, usage_summaries, model_prices). `memory/db.py` pool. `model_prices` seeded.
3. ✅ **completed** — **Auth.** Magic link via SES. `users` + `user_auth_methods`. Auth middleware → `request.state.user`. Default `tier = "dev"`.
4. ✅ **completed** — **Metering + observability harness (before any model calls).** `metering/pricing.py`, `metering/recorder.py`, `metering/prompt_versions.py`, `metering/langsmith_client.py`. Token-recording `call_model()` wrapper writes a `token_usage` row (with `prompt_version_id`), forwards a trace to LangSmith (gated on `LANGSMITH_ENABLED`), and emits a structured log line with `trace_id`. Enforcer no-op stub. The `prompt_versions` table is seeded as each agent comes online in steps 10+.
5. ✅ **completed** — **Channel skeleton + slash commands.** `Channel` ABC, web channel with SSE, command dispatcher with `/help`.
6. ✅ **completed** — **`/usage` command.** Against `token_usage` + `compute_usage` + `model_prices`. Returns empty/zero state cleanly.
7. ✅ **completed** — **Workspace + information & data tools.** `Storage` ABC + S3 adapter (local-FS adapter for self-host); `workspace_files` table; pre-signed upload/download endpoints with server-side prefix scoping; minimal frontend list view. Then the tools: `web_search`, `http_get`, `calculator`, `sql_query`, `create_table`, `read_doc`, `write_doc` (the doc tools read/write through `Storage`, never raw paths). _Frontend list view deferred to step 19 (React app); backend endpoints are complete._
8. ✅ **completed** — **Code execution sandbox.** `Sandbox` interface + E2B adapter (Docker adapter for self-host). `run_python`, `install_package`, `sandbox_read_file`, `sandbox_write_file`. Executor pre-stages workspace files into the sandbox FS on tool invocation and uploads outputs back to S3 on completion (sandbox never talks to S3 directly). `sandboxes` table tracks lifecycle. Compute metering wired through `compute_usage`. _Also added: a SubprocessSandbox provider (dev/test default, real REPL, not a security boundary), and `migrations/003_sandbox.sql` to relax `task_id NOT NULL` on `sandboxes` + `compute_usage` since tasks lifecycle lands in step 15. Executor pre-staging of workspace files is the executor's responsibility (step 13) and isn't built here — the sandbox primitives are._
9. ✅ **completed** — **Artifact production tools.** `create_spreadsheet`, `create_pdf`, `create_chart`, `create_slides` — all run inside the sandbox. Outputs land in the user's workspace via the pre-staging path from step 8 and are registered in `workspace_files` with `source = 'agent_output'`. Overwrite-with-confirmation flow wired: collisions pause the task with `awaiting_user`. _All four tools route through a shared `_artifact.py` helper that JSON-serializes structured inputs into the sandbox, runs a tool-specific static script, reads the bytes back, and registers as `agent_output`. weasyprint is the optional `[pdf]` extra (system deps). Collisions raise `WorkspaceCollision` (same exception `write_doc` raises); the executor's `awaiting_user` wiring lands in step 15. Discovered + fixed an unrelated bug: `SubprocessSandbox` was launching the REPL via file path, which set `sys.path[0]` to the directory containing the package's `subprocess.py` provider and shadowed stdlib `subprocess` inside the child — switched to `python -m wolfpaw.sandbox._repl_server`._
10. ✅ **completed** — **Quick Agent + basic conversational memory.** Haiku + non-sandbox tools. `ConversationalMemory.fetch_recent` lands here; messages are appended on every turn. First step where `/usage` shows real numbers. _`memory/conversational.py` provides `get_or_create_thread`, `append`, `fetch_recent` (verbatim window). `agents/quick.py` runs the tool loop with the 7 non-sandbox tools, hard-capped at 10 iterations, persists user + final-assistant turns only (intermediate tool calls live in-process per the verbatim-window semantics). Web channel wires the agent into `/channels/web/chat`: emits `thread` event on stream start, `tool` events as the agent invokes tools, `delta` with the final text, `done` at end. Sandbox tools are explicitly blocked from this agent's allow-list. Prompt is seeded lazily into `prompt_versions` on first call (degraded mode on DB failure). Tiered summaries + vector recall land in step 12.5; full Soul + User File integration replaces the inline system prompt in step 17._
11. ✅ **completed** — **Triage.** Routes between Quick, Plan, and Task creation. Reads `fetch_recent` + `fetch_summaries` (summaries empty until step 12.5). _`TriageAgent` uses Haiku with a forced `classify` tool_use so the verdict is structured JSON, not parsed text. Returns `TriageVerdict(route, complexity, reasoning)`. Triage is read-only — it never writes to `messages`; persistence stays with the downstream handler. A new `Router` composes Triage + downstream agents; the web channel now calls the Router rather than Quick directly. Plan and Task routes fall through to Quick with an honest preamble since the Planner (step 12) and Tasks (step 15) aren't online yet. SSE stream now includes a `triage` event right after `thread`._
12. ✅ **completed** — **Planner + memory retrieval.** Procedural-memory retrieval is the planner's first step (the diagram's "Check Procedural memory to see if a similar challenge has been attempted before"). If a high-scoring past plan matches the query embedding, the planner adapts it; otherwise it generates fresh. Also pulls relevant **skills** via `SkillsMemory.search_by_task` against the seeded starter set, and per-thread vector-recall hits from `ConversationalMemory.search_relevant`. Decides if request needs a Task (long-running) or just a single plan. _New `embeddings/` subsystem with `VoyageEmbedder` (real HTTP via httpx, mocked in tests) + `StubEmbedder` (deterministic SHA-based vectors for dev without a Voyage key). New `memory/procedural.py` (`search_similar`, `store`, `update_outcome`) and `memory/skills.py` (`search_by_task` + idempotent `seed_starter_skills` for the 5 hand-written exemplars). `schemas.py` ships `Plan`/`Step` dataclasses. `PlannerAgent` uses Sonnet (Opus for ambitious-complexity verdicts under dev tier) with a forced `generate_plan` tool_use, inlines retrieved past plans + skills into the system prompt, and persists the generated plan to procedural memory. Router now calls Planner on the "plan" verdict and emits a `plan` SSE event with a step-count summary plus a markdown plan-preview as the `delta` — the Executor (step 13) will replace the preview with real results. Vector-recall over `message_embeddings` (the `search_relevant` interface) and the compaction worker land in step 12.5. Migration `004_seed_skills.sql` is a marker — actual seeding is Python-driven because computing embeddings requires Voyage._
12.5. **Tiered conversational memory.** `message_embeddings` writes on append; `thread_summaries` table + `workers/jobs/compact_thread.py` (level-1 windows + level-2 summary-of-summaries). Wires `fetch_summaries` / `search_relevant` into the agents from step 11–12. Seed skills loaded here (or in step 12) so the Planner's skill retrieval has content from day one.
13. ✅ **completed** — **Executor.** Functional / reasoning / evaluation step types, with `parallel_group` honored — steps in the same group dispatch concurrently within the plan (distinct from sub-agent delegation, which is inter-plan; see step 16). SSE event stream. Handles sandbox-tool calls. Renders the planner's plan into the running **Execution Plan** with per-step status. _`ExecutorAgent` walks a `Plan` in execution order, batching contiguous parallel-group steps via `asyncio.gather`. On step failure, marks remaining steps `SKIPPED` and returns an error summary as the final answer (no retries in v1). Three step kinds: functional invokes a registered tool; reasoning is a Sonnet call with prior step results inlined; evaluation runs as reasoning in v1 (structured continue/retry/branch verdicts are v2). Synthesis: if the last completed step is a reasoning step, its output is the final answer (planner already synthesized — saves a model call); otherwise the Executor does one Sonnet synthesis call. Sandbox is torn down in `finally` via `SandboxManager.close_for_task` (no-op when nothing was created). New `StepStatus`/`StepResult`/`ExecutionPlan` dataclasses in `schemas.py`. `procedural.update_outcome` is now partial-write — Executor writes `final_answer`/`success`/`error`, Post-Evaluator (step 14) follows up with `score`. Router's plan path now calls Planner → Executor → returns the synthesized text; the plan-preview placeholder is gone. SSE stream gains `step.start` / `step.end` / `step.error` events per step._
14. ✅ **completed** — **Post-Evaluator.** Scoring + plan persistence into procedural memory (recipe-box rows). Emits `task_events` for the score and any error diagnosis. _`PostEvaluatorAgent` uses Haiku with a forced `record_score` tool_use returning a structured `PostEvalVerdict(score, summary, what_went_well, what_went_wrong, improvements)` on a 0-100 scale. Scores get clamped server-side. The Router invokes it synchronously after the Executor; on Post-Eval failure the user still gets the Executor's answer (scoring is best-effort, never blocks response). Score is persisted via `procedural.update_outcome(score=...)` (the partial-write semantics added in step 13 mean this doesn't clobber the Executor's `final_answer`/`success` writes); the full verdict goes into a `task_events` row with `event_type='plan_scored'` carrying the plan_id in `content`. Migration `005_post_evaluator.sql` relaxes `task_events.task_id NOT NULL` so events can fire before the tasks lifecycle lands in step 15. SSE stream gains a `score` event between the executor's last step and the final `delta`. Skills auto-emission from high-scoring reusable plans stays v2._
15. ✅ **completed** — **Tasks: persistent layer + `ask_user`.** Task lifecycle (pending → running → blocked → … → completed), `arq` worker that picks up runnable tasks, `task_events` log, channel notifications on `awaiting_user` / `completed`. `/tasks`, `/task <id>`, `/cancel <id>` commands. The `ask_user` tool ships here, riding on `awaiting_user` to pause + dispatch a question via the originating channel and resume on reply (see [Human-in-the-loop](#human-in-the-loop)). _New `memory/tasks.py` DAO with the full state machine + cross-user-scoped reads/cancels; idempotent terminal transitions. New `tasks/` package: `service.py` (TaskService.create_and_run wraps Planner→Executor→Post-Eval with per-step status transitions + task_event emissions), `ask_user_registry.py` (process-local pending-question dict with cross-user reject), `commands.py` (the three slash commands). New `ask_user` tool requires `ctx.task_id`, registers a question, awaits an asyncio.Future, surfaces timeouts as `blocked` state + ToolError. Router's `task` verdict now invokes the TaskService (no more Quick-fallback preamble); SSE stream emits `task` event with the new task id. New `POST /channels/web/answer` endpoint resolves pending questions. **Deferred**: arq async worker (needs Redis; tasks run synchronously in the request for v1; once arq lands the same `TaskService.run` is invoked by the worker instead). Proactive channel notifications on terminal status also deferred to step 18 (Telegram, which has real push). The `WorkspaceCollision` exception from step 7 still needs an executor hook to translate it into an `ask_user` pause — small follow-up, substrate is here._
16. ✅ **completed** — **Sub-agent delegation.** Planner can mark parallel branches; executor spawns sub-tasks via `parent_task_id`. Budget allocation, depth/concurrency limits, synthesis step. _New `subagent` step kind in `schemas.StepKind`; planner's `generate_plan` tool_use enum + system prompt updated (when + how to use, `inputs: {query, title?, budget_cents?, complexity_hint?}` shape). Executor's `_run_subagent` lazy-imports `TaskService.create_and_run` (dodges the agents↔tasks cycle), passes `parent_task_id` + `budget_cents` through, captures the child's `final_answer`/`score` as the step's output. Depth capped at 3 ancestors via new `memory.tasks.get_depth` (walks `parent_task_id`, bounded). Subagent execution doesn't propagate the parent's `emit` callback — multiple parallel subagents would interleave step events into the parent's SSE stream confusingly. Multiple subagent steps in the same `parallel_group` run concurrently via the existing `asyncio.gather` path. Synthesis is implicit — a trailing reasoning step in the parent plan sees the subagent outputs in prior_results and combines them. Planner.plan now accepts `thread_id: UUID | None` (subagents run with no thread context — fresh start). **Deferred**: per-root concurrency cap of 5 (asyncio.Semaphore follow-up), per-task `spent_cents` rollup (`/usage` already aggregates per-user), structured retry/drop/continue failure policies (v1: child failure = parent step failure)._
17. **Soul + User File integration.** `soul.md` and per-user `user_profiles` row into every system prompt; `soul_version` and `user_profile_version` on threads. Web UI for the user to view/edit their profile; Markdown body + structured preferences form. Triage Agent uses it for tone and routing.
18. **Telegram channel.** `@WolfpawBot`, webhook, `channel_links`, deep-link onboarding. All slash commands work here. `002_channels.sql`.
19. **Web app.** React + Vite. Onboard, chat, task list, artifact browser, usage dashboard, channel settings.
20. **CloudWatch dashboards + Logs Insights queries.** Committed in `infra/dashboards/`.
21. **Infra: Terraform.** EC2 + RDS + ElastiCache + SES + Secrets + IAM + Caddy + systemd. E2B account/keys.
22. **OSS packaging (v1.5 entry).** Docker Compose (incl. Docker-based sandbox runtime for self-host), install script, README, env-template.
23. **Email forwarding (v1.5).** SES inbound, Lambda dispatcher, `email_aliases`, verified owners, drafts-out constraint.
24. **Billing + tier enforcement (when ready to monetize).** Stripe Checkout + customer portal + webhook → `subscriptions`. Flip enforcer from no-op to real cap checks. Cost notifications at 50/80/100%. `003_billing.sql`.
25. **Slack (v2).** OAuth workspace install, app manifest, slash command + DMs.

The reordering keeps metering + `/usage` ahead of any model call and adds sandbox + artifact tools (steps 8–9) before any agent uses them, so from step 10 forward every model call is metered AND every code execution is metered AND every artifact is tracked. Tasks (15) and sub-agents (16) sit between the agent loop and the channels — once they exist, Wolfpaw can take on multi-day work.
