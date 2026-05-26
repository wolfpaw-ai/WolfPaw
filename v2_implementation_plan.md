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

### 22. Tiered conversational memory (was v1 step 12.5) — M

Closes the only unbuilt v1 step. Long threads degrade gracefully instead of
losing context past the recent-window cap.

- New: writes to `message_embeddings` on every `conv.append`
- New: `workers/jobs/compact_thread.py` — when a thread crosses N messages, summarize the oldest unsummarized window → `thread_summaries` (level 1); when level-1 count crosses M, fold into level 2
- New: `conv.fetch_summaries(thread_id)` + `conv.search_relevant(thread_id, query_embedding, k)`
- Wire: Triage + Quick see verbatim window + summaries; Planner additionally sees vector recall hits
- Tables already exist (`message_embeddings`, `thread_summaries` from `001_init.sql`)
- Depends on: step 23 (arq) for the worker, OR shipped synchronously first with arq taking over later

### 23. arq worker — M

Redis-backed background job runner. Substrate for #22 compaction, scheduled tasks, proactive notifications, and restart-safe long tasks.

- Add `redis` to `docker-compose.yml`
- New: `src/wolfpaw/workers/arq_app.py` (worker entrypoint), `workers/jobs/` (compaction, notifications, scheduled-task tick)
- Refactor `TaskService.create_and_run` → split into `create()` + `run(task_id)`. Router enqueues; worker runs.
- Channel fire-and-forget (`asyncio.create_task` in web + Telegram + Slack) moves to arq enqueue
- New env: `WOLFPAW_REDIS_URL`, `WOLFPAW_WORKERS_ENABLED` (so single-process dev keeps working)

---

## Phase B — Self-improving agent

### 24. Plan Pre-Evaluator — S

New agent between Planner and Executor. Three forced-tool checks: (1) plan achieves objective? (2) simplifiable? (3) better than past plans found in procedural memory? On fail → back to Planner with diagnosis.

- New: `agents/plan_pre_evaluator.py` (Haiku, forced `evaluate_plan` tool)
- Router wires it into the plan/task path
- One retry max per request, then ship whatever the Planner produces with a warning
- SSE event: `pre_eval` (verdict + diagnosis)

### 25. Skills auto-emission — M

Post-Evaluator promotes high-scoring reusable plans → distilled Skills.

- New: `agents/skill_distiller.py` (Sonnet, forced `emit_skill` tool — produces `name`, `description`, `ingredients`, `generalized_steps`)
- Trigger: Post-Evaluator score ≥ 90 AND plan looks reusable (heuristic — multi-step, uses tools, not a one-shot answer)
- Persist via `memory.skills.store(...)` with `source_plan_id`
- Open: dedup logic (don't emit Nth copy of "vendor research" skill — check `search_by_task` for near-duplicates first)
- Threshold tunable via `WOLFPAW_SKILL_EMIT_MIN_SCORE`

### 26. Sleep Cycle cron — M

Periodic background job (arq scheduled task). Re-scores old plans against current Post-Evaluator prompts, consolidates near-duplicate skills, garbage-collects orphan threads.

- New: `workers/jobs/sleep_cycle.py`
- Default: opt-in (`WOLFPAW_SLEEP_CYCLE_ENABLED=false`); cadence weekly
- Operations: re-score N oldest plans; merge skills with high cosine similarity; mark threads with zero messages > 30 days for cleanup

### 27. Structured subagent failure policies — S

Today: any subagent failure fails the parent step. v2: planner declares per-branch policy.

- Extend `subagent` step's `inputs` schema with `on_failure: "fail" | "drop" | "retry"`
- Executor honors the policy in `_run_subagent` exception handling
- Retry uses Planner with the failure diagnosis as added context
- Planner's `generate_plan` tool-use input schema gets the new field

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
