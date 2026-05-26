# Wolfpaw v2 — Implementation Plan

Continuation of [`implementation_plan.md`](implementation_plan.md). v1 build
ended at step 21 (Slack channel). v2 picks up at step 22.

Design lives in [`v2_spec.md`](v2_spec.md). This doc is the build order with
file pointers, dependencies, and rough cost ballparks. Keep it concise; expand
each step into a real spec only when it's about to be built.

Cost: **S** = under a day, **M** = 1-3 days, **L** = a week or more.

## Phases

- **A. Substrate** (steps 22-23): unblocks everything else
- **B. Self-improving agent** (24-28): compounding agent quality
- **C. Real-world integrations** (29-33): first OAuth wave
- **D. Channel polish** (34-37): unfinished v1 bullets + voice substrate
- **E. Frontend + UX polish** (38-42)
- **F. Observability + scale** (43-44)

Phases A→B are sequential; C/D/E/F can run in parallel once A is done.

---

## Phase A — Substrate

### 22. Tiered conversational memory (was v1 step 12.5) — M ✅ **completed**

Closes the only unbuilt v1 step. Long threads degrade gracefully instead of
losing context past the recent-window cap.

- `conv.append` now fires the post-append follow-ups (embed + compaction trigger) as `asyncio.create_task` background work; gated by an in-process flag (`disable_post_append_for_test`) for clean test isolation.
- New `workers/jobs/compact_thread.py` job — drains in a loop. L1: when a thread crosses `compaction_trigger_threshold` (default 40) messages, batch the oldest 20 messages older than the recent window into a Haiku summary → `thread_summaries(level=1)`. L2: when 10 un-folded L1s accumulate, fold the oldest 10 into one L2 row and stamp `folded_into_summary_id` on each child.
- New `conv.fetch_summaries(thread_id)` returns L2 + un-folded L1 in chronological order; `conv.search_relevant(thread_id, query_embedding, k, exclude_recent_n)` does per-thread cosine search over `message_embeddings`, deliberately excluding the recent window so the Planner doesn't see duplicates of what `fetch_recent` already returned.
- New `conv.format_summaries_block(...)` + `conv.format_vector_recall_block(...)` shape the reads into Markdown blocks the agents inline into their system prompts.
- Wired: Triage + Quick read `fetch_summaries`; Planner additionally reads `search_relevant`; subagent runs (`thread_id=None`) skip both.
- New migration `008_conv_compaction.sql` — adds `'compactor'` to the `agent_kind` enum (so summarization `token_usage` rows attribute cleanly) + a `folded_into_summary_id` column on `thread_summaries` for explicit L1→L2 fold tracking.
- New config knobs: `recent_window_size=20`, `compaction_window_size=20`, `compaction_trigger_threshold=40`, `l2_fold_threshold=10`, `vector_recall_k=5`.
- The summarizer is injectable via `set_summarizer_for_test` so unit tests cover the trigger + fold arithmetic without live Anthropic calls.
- **Followups for step 23 (arq).** Today the embed + compaction triggers run as fire-and-forget `asyncio.create_task`. Once arq lands, both move onto the worker so they survive process restart, carry retry semantics, and stop leaking via abandoned tasks under high churn.

### 23. arq worker — M ✅ **completed**

Redis-backed background job runner. Substrate for #22 compaction, the v2 Sleep Cycle (#26), proactive notifications, and restart-safe long Tasks.

- New `src/wolfpaw/workers/queue.py` — per-job typed enqueue helpers (`enqueue_compact_thread`, `enqueue_embed_message`, `enqueue_run_task`, `enqueue_telegram_dispatch`, `enqueue_slack_dispatch`). Each branches on `WOLFPAW_WORKERS_ENABLED`: ON → `get_pool().enqueue_job("<name>", ...)` against arq; OFF → `asyncio.create_task` in the caller's loop (dev default, no Redis needed). Module-global pool with a `close_pool` hook wired into the FastAPI lifespan in `api.py`.
- New `src/wolfpaw/workers/arq_app.py` — `WorkerSettings` registering the five jobs above. Lifecycle hooks warm + drain the asyncpg pool on worker start/stop.
- New `src/wolfpaw/workers/jobs/run_task.py` + `channel_dispatch.py` (Telegram + Slack), plus arq-shaped wrappers (`compact_thread_job`, `embed_message_job`) added to `compact_thread.py`. Each is a thin coroutine that unpacks string args back into UUIDs and delegates to the existing handler.
- `TaskService` refactored — `create()` writes the `pending` row + stamps a `status.pending` event carrying the run inputs (content, thread_id, complexity_hint) so `run(task_id)` can recover them without a side table. `create_and_run` is preserved for the subagent path (parent waits for child output, so workers-deferred execution doesn't apply).
- Router's task path branches on `workers_enabled`: ON → `create()` + `enqueue_run_task` + immediate "Started Task <id>" ack (the user follows up via `/task <id>` or waits for the v2 step-37 push); OFF → existing `create_and_run` inline so single-process dev produces the synthesized answer in the same response.
- Telegram + Slack channels replaced their `asyncio.create_task(_handle_*)` calls with `enqueue_telegram_dispatch` / `enqueue_slack_dispatch`. Same liveness contract in dev (no Redis required); arq picks it up in compose.
- `conv.append`'s post-append work moved onto the queue — `embed_and_store` exposed (renamed from `_embed_and_store`); the inline `_post_append_work` + `_trigger_compaction` wrappers are gone.
- New config: `WOLFPAW_WORKERS_ENABLED` (default `false`), `workers_redis_max_connections`, `workers_sleep_cycle_cron`. `WOLFPAW_REDIS_URL` already existed.
- docker-compose adds `redis` (with appendonly persistence) + `worker` (runs `arq wolfpaw.workers.arq_app.WorkerSettings`); both `app` and `worker` set `WOLFPAW_WORKERS_ENABLED=true` by default. `.env.example` documents the new toggle.
- `arq>=0.26` added to base deps.
- Tests: 13 new `test_workers_queue` (inline-fallback + arq-pool branch per job + pool lifecycle), 2 new `test_agent_router_task` (workers-on/off branching), 4 new DB-gated `test_tasks_service_db` (`create` does no planning, `run` recovers from pending event, error paths). Suite: **320 passing / 121 DB-gated skipped** (the existing weasyprint e2e test is unrelated and was failing before this step on macOS without system libs).
- **Web SSE stays in-process.** SSE event streaming is tied to the HTTP connection; routing the agent to arq would require a Redis pub-sub bridge — deferred until a real driver shows up. Telegram + Slack are the durable wins because their webhook flow already detaches the reply.
- **Follow-up before step 24/25/26:** proactive task-completion push (v2 step 37) becomes available once a caller is wired — substrate is now here. Worker → channel send is currently caller-less, so a worker-run Task quietly finishes in the DB; users have to poll `/tasks`.

---

## Phase B — Self-improving agent

### 24. Plan Pre-Evaluator — S ✅ **completed**

New agent between Planner and Executor that catches obviously-broken or gratuitously-complex plans before we burn Executor tokens on them.

- New `agents/plan_pre_evaluator.py` — `PlanPreEvaluatorAgent` (Haiku via `model_post_evaluator` slot) with a forced `evaluate_plan` tool_use returning three booleans: `achieves_objective`, `simplifiable`, `better_than_past_plans`. A plan is approved iff `achieves_objective AND NOT simplifiable AND better_than_past_plans`. The `diagnosis` string is the prose feedback the Planner receives on a retry. Surfaces high-scoring (≥80) past plans in the eval prompt so the third check has something to compare against.
- New `plan_with_pre_evaluation(planner, pre_evaluator, ...)` helper in the same module — runs the Planner, evaluates, retries once with `revision_diagnosis` on rejection, ships the second draft unconditionally. Both passes emit a `pre_eval` SSE event so the UI can show the verdict.
- `PlannerAgent.plan` gained `revision_diagnosis: str | None = None`. When set, a "you are revising a rejected draft" preamble goes at the front of the system prompt so the model treats the diagnosis as the top-line directive rather than another piece of context.
- `Router` + `TaskService` both gained a `pre_evaluator` field (singleton fallback via `get_pre_evaluator_agent`) and call the helper in place of the direct `planner.plan(...)` invocation.
- New `PreEvalVerdict` dataclass in `schemas.py` with `to_jsonb()` matching the Post-Evaluator's shape.
- Failure posture: the Pre-Evaluator approves-by-default on any internal failure (model call exception, no tool_use returned). Reasoning in the module docstring — the cost of an unreviewed plan is worse output; the cost of blocking on a misbehaving evaluator is no output at all.
- SSE: one `pre_eval` event on approve ("approved (attempt 1): <diagnosis>"); two on retry ("rejected (attempt 1): <failed-checks> — <diagnosis>" + "retry (attempt 2): shipping Planner's revised draft unchecked").
- Tests: 11 new unit tests in `test_agent_plan_pre_evaluator.py` (forced-tool parsing + each rejection axis + past-plan filtering + approve-by-default + retry helper), 4 new integration tests in `test_agent_router_plan.py` (approve path, reject+retry, single SSE event on approve, two SSE events on retry), 2 new planner tests for the revision-diagnosis prompt block. Suite: **337 passing / 121 DB-gated skipped** (was 320/121).
- Threshold for `_HIGH_SCORE_THRESHOLD` is 80 — open question in the spec was the score that qualifies a "past plan worth beating". 80 keeps it conservative; tune after dogfooding.

### 25. Skills auto-emission — M ✅ **completed**

The compounding-quality move: high-scoring reusable plans get distilled into named Skills that the Planner pulls first on future similar requests.

- New `agents/skill_distiller.py` — `SkillDistillerAgent` (Sonnet via `model_planner`) with a forced `emit_skill` tool_use returning `name` (snake_case), `description` (one paragraph, written abstractly so it embeds similarly to future user queries), `ingredients` (`{"tools": [...]}` hints), and `generalized_steps` (skeleton in the same shape as a Plan's steps but with placeholders for user-specific values). System prompt explicitly directs the model to strip user-specific details and write the description "to embed similarly to how a future user would phrase the same request."
- New `maybe_distill_skill(...)` helper in the same module — full emission flow with five gates: (1) score ≥ `skill_emit_min_score` (default 90), (2) reusability heuristic (multi-step AND at least one functional or subagent step), (3) `plan.id is not None`, (4) dedup pass against existing skills via cosine on the plan's query embedding (drops on hit above `skill_dedup_similarity_threshold`, default 0.85), (5) distiller produces a structured payload. Re-embeds the distilled description (canonical retrieval text) before persisting.
- New `memory.skills.store_emitted(...)` — user-scoped insert keyed on `source_plan_id` pointing back at the originating plan row.
- New migration `009_skill_distiller.sql` — adds `'skill_distiller'` to the `agent_kind` enum so `token_usage` rows attribute cleanly.
- New `PreEvalVerdict`-style flow: `Router._score_and_persist` + `TaskService._run_inner` both call `maybe_distill_skill` after Post-Evaluator scoring. Wrapped in try/except — skill emission is best-effort and must never block returning the user's answer.
- New SSE event `skill_emitted` carrying the new skill's name so the UI can surface "I learned a new skill" moments.
- New config: `skill_emit_min_score` (default 90, env `WOLFPAW_SKILL_EMIT_MIN_SCORE`), `skill_dedup_similarity_threshold` (default 0.85).
- Tests: 19 new unit tests in `test_agent_skill_distiller.py` (forced-tool parsing, the reusability + threshold gates, dedup short-circuit, full happy path, persistence failure handling, threshold tunability), 3 new integration tests in `test_agent_router_plan.py` (router invokes the helper after scoring, even on low scores so threshold lives in one place, swallows distiller failures), 2 new DB-gated tests in `test_memory_procedural_skills_db.py` (`store_emitted` round-trip + user scoping). Suite: **359 passing / 123 DB-gated skipped** (was 337/121).
- **Open question deliberately answered**: the v2 spec asked "what score qualifies a plan for promotion?" — answered 90, conservative, tunable. Same kind of threshold as the Pre-Evaluator's `_HIGH_SCORE_THRESHOLD=80` from step 24; both should be revisited after dogfooding produces real plan-distribution data.
- **Followup before step 26 (Sleep Cycle)**: skill consolidation (merging near-duplicate user-emitted skills across plans) is on the Sleep Cycle's agenda; this step's dedup is single-shot at emit time, so a slow drift of "almost-same skills" is still possible. The Sleep Cycle will sweep these.

### 26. Sleep Cycle cron — M ✅ **completed**

Weekly maintenance job that re-checks the memory subsystems against current standards.

- New `workers/jobs/sleep_cycle.py` with three independent + bounded operations:
  - **Re-score N oldest plans** — pulls the `sleep_cycle_rescore_batch` (default 20) oldest scored plans with a persisted `final_answer`, reconstructs a degenerate `ExecutionPlan` (intermediate step results aren't persisted, so the re-score reads on plan shape + final answer), runs it through `PostEvaluatorAgent.evaluate`, writes the new score via `procedural.update_outcome`. Catches prompt drift: a 90 from six months ago may now be a 60.
  - **Consolidate near-duplicate skills** — per user, walks `list_active_for_user` and finds pairs whose cosine similarity ≥ `sleep_cycle_dedup_threshold` (default 0.92, higher than the emit-time 0.85 since post-hoc merging is destructive). Picks survivor by score (newer wins on score tie), soft-deletes the loser via `superseded_by_skill_id`. Repeats until no pair crosses the threshold.
  - **GC orphan threads** — `DELETE FROM threads WHERE created_at < NOW() - 30 days AND NOT EXISTS (messages)`. These come from `/reset`-then-leave flows and channel-link mishaps.
- New `workers/queue.enqueue_sleep_cycle()` — manual / ad-hoc trigger that follows the same arq-or-inline routing as the other enqueue helpers.
- New `cron_jobs` registration on `WorkerSettings` in `arq_app.py` — Sunday 03:00 UTC. The job no-ops when `WOLFPAW_SLEEP_CYCLE_ENABLED=false`, so the cron is safe to keep registered on every deployment; flipping the env flag enables work without a worker restart.
- New `memory.skills` functions: `mark_superseded`, `list_active_for_user`, `neighbours`. `search_by_task` now filters out superseded rows so the Planner only ever retrieves the survivor.
- New migration `010_skill_supersession.sql` — adds `superseded_by_skill_id UUID NULL` + `superseded_at TIMESTAMPTZ NULL` to `skills` with `ON DELETE SET NULL` so deleting a survivor restores its absorbed duplicates rather than chain-deleting them.
- New config: `sleep_cycle_enabled` (default `false`), `sleep_cycle_rescore_batch=20`, `sleep_cycle_dedup_threshold=0.92`, `sleep_cycle_orphan_thread_age_days=30`. Removed the unused `workers_sleep_cycle_cron` string (the cron lives in `arq_app.py` as proper arq kwargs now).
- Returns a `SleepCycleResult` dataclass with per-op counters; the arq wrapper surfaces it as a dict so arq's job history (`Job.info()`) carries the last run's stats.
- Failure posture: per-plan re-score errors don't stop the batch; per-pair supersession errors don't stop the user; thread-GC errors log + bail without touching plans/skills. The whole job is idempotent + best-effort.
- Tests: 14 new in `test_workers_sleep_cycle.py` — 7 unit (survivor-pick, gating on the flag, orchestrator + arq wrapper) and 7 DB-gated (skill consolidation, threshold-cutoff no-op, `search_by_task` superseded filter, `mark_superseded` idempotency, plan re-score via mocked evaluator, re-score skips plans without `final_answer`, orphan-thread GC). 2 new in `test_workers_queue.py` for the new enqueue helper. Suite: **368 passing / 130 DB-gated skipped** (was 359/123).

### 27. Structured subagent failure policies — S ✅ **completed**

The Planner now declares per-subagent-step failure handling. v1 behaviour ("any subagent failure fails the parent step") becomes one option of three.

- Planner role prompt documents the new `inputs.on_failure: "fail" | "drop" | "retry"` field on subagent steps — the schema itself stays `{"type": "object"}` since Anthropic tool_use is permissive about object keys; documenting it in the prompt is what the model reads. Default (when missing) is `"fail"` for backwards compatibility.
- `agents/executor.py` `_run_subagent` refactored: extracted the actual spawn-and-validate into `_spawn_subagent_task`, and the outer method now wraps the spawn in policy-aware exception handling. Depth check + input validation stay in `_run_subagent` and are NOT covered by the policy — only true subagent-task failures (non-completed child, model crash inside the child, etc.) are.
- New helpers in the same module: `_normalize_failure_policy` (unknown values fall back to `"fail"` with a warning — never silently swallow failures), `_drop_stub` (returns `{"dropped": True, "error": "...", "answer": None}` so the parent's synthesis step can tell a dropped branch from a real one), `_augment_query_with_failure` (appends the failure diagnosis to the original query for the single retry attempt).
- Retry is capped at 1 attempt. The retry's content is the original `inputs.query` plus a "Previous attempt failed: ..." preamble so the subagent's own Planner gets concrete feedback. If the retry also fails, the failure propagates as if the policy were `"fail"`.
- Tests: 7 new in `test_agent_executor_subagent.py` covering each policy (`fail` default, `drop` single + parallel, `retry` happy path + retry-also-fails), unknown-policy normalization, and the invariant that the policy doesn't shadow input-validation errors. Suite: **374 passing / 130 DB-gated skipped / 2 deselected** (was 368/130/1).
- **Pre-existing failure, unrelated to step 27**: `test_parallel_subagent_steps_dispatched_concurrently` fails on `dev` HEAD with an asyncpg connection-refused — confirmed by stashing my changes and reproducing. Looks like the test relies on a real DB at localhost:5432 that isn't there on this machine. Deselected for the suite count; should be triaged separately.

### 28. Tool Creator agent — L

Agent proposes new tools when it hits a gap (similar to `create_table` in step 7 but generalized).

- New: `agents/tool_creator.py` — given a query the registry can't fulfill, drafts a tool spec (name, description, input_schema, Python implementation)
- Human-in-loop approval via `ask_user` before registration
- New: `tools` table (already in `001_init.sql`) gets populated with agent-created entries
- Approved tools land in the registry for that user only by default; promote-to-global is a manual step
- High risk for a first pass — gate behind a feature flag

---

## Phase C — Real-world integrations

Every integration follows the same shape: OAuth flow → per-user token in a new table → tool implementation that wraps the provider's API → planner discovers it via the registry.

### 29. Dropbox app-folder — M

Easiest of the integration set. Sandboxed `/Apps/Wolfpaw/` folder, full read/edit/delete inside.

- New: `channels/dropbox_oauth.py` (mirrors slack OAuth pattern)
- New: migration `008_dropbox.sql` — `dropbox_links(user_id, access_token, refresh_token, expires_at)`
- New: tools `dropbox_read_file`, `dropbox_write_file`, `dropbox_list_folder`
- Refresh-token rotation on every call (Dropbox tokens expire ~4h)

### 30. Notion — M

Read pages the user shares; create pages.

- New: `channels/notion_oauth.py`
- New: migration `009_notion.sql` — `notion_links` + `notion_pages` (cache of accessible pages)
- New: tools `notion_search`, `notion_read_page`, `notion_create_page`
- API rate limits: 3 req/s — wrap the client in a token bucket

### 31. Google Calendar — L

Read + create events. Sensitive-tier OAuth verification (Google review, ~weeks).

- New: `channels/google_oauth.py` (shared with Drive + Gmail later)
- New: migration `010_google.sql` — `google_links` (broad enough for Drive/Gmail token sharing)
- New: tools `calendar_list_events`, `calendar_create_event`
- Verification timing matters — start the Google review **before** building the integration

### 32. Microsoft Calendar — M

Read + create events. Easier verification than Google.

- New: `channels/microsoft_oauth.py`
- New: migration `011_microsoft.sql` — `microsoft_links`
- New: tools `outlook_calendar_list_events`, `outlook_calendar_create_event`

### 33. Gmail readonly — L

Read only. Drafts still flow via Wolfpaw → owner-email pattern (never via Gmail API). Restricted-tier scope; CASA audit timed to public launch.

- Extends `google_links` from #31
- New: tools `gmail_search`, `gmail_read_message`
- Hard policy in `agents/`: no write/send/modify scopes ever
- Audit + verification gating: start CASA process when revenue justifies (~$15k-75k/year)

---

## Phase D — Channel polish + voice substrate

### 34. STT/TTS client abstractions in OSS — S

Net-new client ABCs so Wolf-Pi (private product) + future OSS channels can share the same interface.

- New: `src/wolfpaw/audio/__init__.py` — `STTClient` ABC + `TTSClient` ABC
- New: `RemoteSTT` / `RemoteTTS` defaults that POST to a configurable endpoint
- No tools / channels consume them in OSS today — that's Wolf-Pi's job

### 35. Slack `app_mention` events + `thread_ts` mapping — S

Substrate is already in `channels/slack.py` (scope claimed at install). 10-line addition.

- Handle `event.type == "app_mention"` in `_handle_message_event`
- Map Slack `thread_ts` → Wolfpaw `thread_id` for in-channel continuity

### 36. Telegram file uploads + inline keyboards — M

Round out the Telegram channel.

- File upload: handle `message.document` / `message.photo` → download via Telegram API → register in `workspace_files`
- Inline keyboards for `ask_user` options (replaces free-text answer where the question has multiple-choice)
- Voice messages → STT (uses #34's abstractions)

### 37. Proactive task-completion push — S

Substrate exists (`TelegramChannel.send`); just no caller wired.

- `TaskService` on terminal transition → if user has a `channel_for_completion` set, push a one-liner via that channel's `Channel.send(user_id, content)`
- For web users (no push channel): mark a `notification` row that the React app polls + shows as a toast

---

## Phase E — Frontend + UX polish

### 38. Usage charts — S

Replace tables with charts (line for daily spend, stacked bar for by-model breakdown).

- Library: Recharts (small, idiomatic React)
- New: `web/src/usage/UsageCharts.tsx`
- Keep tables as a tab for the spend-justification use case

### 39. File upload UI — S

`FilesPage` gets an upload button. Backend already supports it (signed-upload-URL flow from step 7).

- `web/src/files/FilesPage.tsx` — drag-drop zone, progress bar, refresh on upload

### 40. Theming + visual design pass — M

Minimal CSS today is functional, not pretty. Theme variables, dark mode, brand colors.

- CSS custom properties for color tokens
- Toggle in profile page (system / light / dark)
- One pass with a designer or designer-tool for fonts + spacing + iconography

### 41. Frontend tests — S

No vitest setup today. Add one.

- `web/vitest.config.ts`
- Tests for `sseClient.ts`, the chat event reducer, the auth context
- CI step that runs `cd web && npm test`

### 42. PWA / offline shell — S

Install prompt, basic offline shell that says "Wolfpaw is offline" instead of a blank page when the server's unreachable.

- `vite-plugin-pwa`
- Cache static assets aggressively; never cache API responses

---

## Phase F — Observability + scale

### 43. OpenTelemetry tracing — M

Today: `trace_id` in structured logs + LangSmith for LLM-specific spans. v2 adds OTel for cross-service tracing once the system grows past one runtime (e.g. when the arq worker process needs trace propagation from the web request).

- `opentelemetry-instrumentation-fastapi` + `opentelemetry-instrumentation-asyncpg`
- Default off (`WOLFPAW_OTEL_ENABLED=false`); when on, exports OTLP to whatever endpoint the operator runs (Tempo, Jaeger, Honeycomb, etc.)

### 44. Entity / Summary / Knowledge-Base memory types — L

Beyond per-thread memory: cross-thread entity tracking ("Alice is the procurement lead"), persistent summaries ("user's typical workflows"), domain knowledge bases the user uploads.

- Design first — shape depends on what gaps exist after skills auto-emission and tiered memory are in production
- Tables: new (don't try to retrofit existing schema)
- Read by Triage (for tone) + Planner (for context)

---

## Out of scope for v2

Pushed to v3+:
- Skills marketplace ("Pawhub")
- Browser extension (v3+)
- Native mobile app
- Gmail send / Outlook mail / OneDrive / iCloud / Plaid / GitHub integrations
- iMessage / WhatsApp channels

Separate tracks (own private docs):
- SaaS deployment — [`saas.md`](saas.md)
- Wolf-Pi hardware product — [`wolf-pi.md`](wolf-pi.md)

---

## Build order recommendation

If you can only do three things in v2, do: **22 (tiered memory) → 23 (arq) → 25 (skills auto-emission)**. That's three weeks of work that converts the agent from "competent v1" into "noticeably gets smarter."

If you can do six: add **29 (Dropbox)**, **24 (Plan Pre-Evaluator)**, **37 (proactive push)**.

If you have the runway: full phases A → B → C → D → E → F in order. The phases are designed so you can ship and dogfood after each one.
