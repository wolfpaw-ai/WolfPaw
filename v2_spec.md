# Wolfpaw v2 — Design Spec

v1 is the agent skeleton: it plans, executes, scores, remembers verbatim, ships
artifacts, runs in channels. v2 is the *learning* skeleton: the agent gets
measurably smarter over time, reaches into the user's real files + calendar,
and runs the long jobs reliably across process restarts.

Three themes drive v2:

1. **Self-improving agent** — past plans + scores feed the next planner; high-scoring reusable plans get promoted to Skills automatically; older memory compacts itself; bad plans get caught before they run.
2. **Real-world integrations** — first OAuth wave (Dropbox, Notion, Calendar, Gmail-read). Wolfpaw stops being a sandbox and starts touching the user's actual workspace.
3. **Durable async** — arq worker substrate. Long-running tasks survive restarts. Scheduled / recurring tasks become possible. Proactive notifications get a home.

## Capabilities added

| Capability | What it enables |
|---|---|
| Tiered conversational memory (12.5) | Long threads stay useful — older messages compact to summaries; per-thread vector recall surfaces relevant older context |
| arq worker | Tasks survive restarts, scheduled jobs, background compaction, proactive notifications |
| Plan Pre-Evaluator | Catches "this won't achieve the objective" / "this isn't better than a past plan" before executing |
| Skills auto-emission | High-scoring reusable plans get distilled into named Skills the Planner pulls first next time |
| Sleep Cycle cron | Periodic reorganization: re-score old plans against current scoring, consolidate duplicate skills, garbage-collect dead threads |
| Structured subagent failures | Parent chooses retry/drop/continue per failed branch instead of "any failure kills the plan" |
| Dropbox / Notion / Calendar / Gmail-read | Real files, real schedule, real inbox context. Read-only by policy where applicable (Gmail) |
| Tool Creator agent | Agent proposes new tools when it hits a gap; human approves; tool registers and becomes available |
| Entity / Summary / KB memory | Cross-thread memory types beyond the per-thread tier |
| Frontend polish | Usage charts, file upload UI, theming, vitest setup, PWA |
| Proactive push | Task-completed notifications via Telegram (substrate exists, no caller wired) + email-out (when SMTP/SES backend is added) |
| Slack channel-mentions | `@wolfpaw` in a channel works, not just DMs. `thread_ts` ↔ `thread_id` mapping |
| OpenTelemetry tracing | Cross-service tracing when the system grows past one runtime |

## Out of scope for v2

- **SaaS deployment** — separate plan in `saas.md` (private).
- **Gmail write / Outlook mail / Plaid / OneDrive / iCloud / Mobile-native app / Skills marketplace ("Pawhub")** — all v3+.
- **Browser extension** — v3 at the earliest; the kind of work Wolfpaw is designed for doesn't need it.
- **Voice (STT/TTS) in OSS**

## Open questions

- **Skills auto-emission threshold.** What score qualifies a plan for promotion? Start at 90+, refine after dogfooding.
- **Sleep Cycle cadence.** Nightly? Weekly? Per-user opt-in? Default off until we see what it costs.
- **Plan Pre-Evaluator model tier.** Haiku to keep it cheap, or Sonnet for quality? Run both, A/B on plan-success-rate.
- **Entity / KB memory shape.** Tables exist in the diagram but the retrieval/write semantics are undecided. Defer the design until skills auto-emission is shipped and we know what *isn't* covered by skills + tiered conversational memory.

## What's not changing

- Channel-native posture (web, Telegram, Slack, eventually email).
- Read-only-by-default scopes, hard pause at cap, opt-in overage.
- "Tread lightly" — ask before destructive, surface uncertainty, never surprise the user with a bill.
- Single codebase, single runtime; self-host stays first-class.
- OSS / hosted distribution split; no licensing changes.

## Success criteria

v2 ships when:
- A user can ask "what did I do last week?" and get an accurate answer from compacted thread summaries, not the most-recent-20-message window.
- A long-running task survives a `docker compose restart app` and resumes from the last completed step.
- The Planner's `procedural.search_similar` regularly returns *Skills* (auto-emitted), not just raw past plans.
- A user can ask "summarize the document at `/Wolfpaw/Q3-plan.md`" and Wolfpaw reads it from Dropbox.
- A user gets a Telegram ping when their long-running task completes.
- `/usage` shows charts, not just tables.
