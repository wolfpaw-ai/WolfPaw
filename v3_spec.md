# Wolfpaw v3 — Design Spec

v2 makes the agent self-improving and reaches into real-world files + calendars
([`v2_spec.md`](v2_spec.md)). v3 starts when v2 has been dogfooded long enough
to know what's actually missing.

This doc is a living working surface — not every section will be filled in
before work begins. New sections get added as decisions firm up.

---

## Cost Guardrails

Make Wolfpaw cost-aware on two timescales: **per-task** (interactive budget
prompts + mid-flight pause at 80% / hard stop at 100%) and **per-month** (user-
configurable spend-checkpoint reminders). Aligns with the "tread lightly /
never surprise the user with a bill" posture.

Most of the infra exists from v1; what's missing is the interactive layer.

### What exists already

| Piece | State |
|---|---|
| `tasks.budget_cents` column | Set at task creation; passed to subagents. Informational only today. |
| `tasks.spent_cents` rollup | Computed on terminal transition via `memory.tasks.rollup_spent_cents` (step 19.5). **Not** computed mid-flight. |
| `ask_user` tool | Pause + structured-options + asyncio.Future wait. Exactly what the 80% prompt needs. |
| `cost_notifications` table | Schema + dedup pattern. No caller wires it yet. |
| `Enforcer.check_can_spend(user_id)` | Interface; no-op stub in OSS. SaaS swaps real impl. |
| `user_profile.preferences` jsonb | Free-form; holds per-user knobs like `cost_checkpoint_step_dollars`. |
| Proactive push via `Channel.send(...)` | Substrate built. First caller (task-completion) lands in v2 step 37. |

### What needs to be built

Four pieces, ship as one coherent feature (~3 days):

1. **Mid-flight `spent_cents` recompute.** Cheap incremental — every N model calls or on a timer in the Executor's loop. Updates `tasks.spent_cents` so the budget check below has fresh data.

2. **Executor budget check.**
   - At 80% of `task.budget_cents` → invoke `ask_user` with structured options: *cancel* / *increase by $N* / *continue until hard cap*.
   - At 100% → mark task `failed` with reason `budget_exceeded`; push a notification via the user's preferred channel.

3. **Router budget prompt at task creation.** When Triage routes to the task path *and* projected cost exceeds a threshold (use the complexity hint as a proxy — `ambitious` → ask, `moderate` → silent default, `simple` → no task), Router injects an `ask_user`-style budget prompt before kicking off the Planner.

4. **Per-user monthly checkpoint emitter.** Recorder bumps a running monthly total after every model + compute write. When it crosses `N × cost_checkpoint_step_dollars` (default $5, user-configurable in their profile), fire one notification per crossing via `Channel.send(...)`, dedup'd in `cost_notifications`.

### Dependencies

- v2 step 37 (proactive push) for delivery. Without it, fall back to a web in-app banner / `cost_notifications` row the React app polls.
- v2 step 23 (arq worker) if checkpoint notifications should run as a background job rather than synchronously inside the model-call hot path.

### Open questions

- **Default per-task budget** when the user doesn't answer the prompt — proceed cautiously with a small default? Or block until they answer?
- **Budget increase UX in `ask_user`.** Free-form dollar amount (hard to do in Telegram), or fixed options ($1 / $5 / $20 / unlimited)?
- **Subagent budget enforcement.** Inherited from parent today; should subagent budget overruns roll up to parent's check, or fail the child independently?
- **Reset semantics for the monthly checkpoint** — calendar month, billing-period-aligned, or rolling 30 days? Probably calendar month to match `usage_summaries`.
- **Soft caps in self-host.** No real billing — does the user want any of this active, or is it noise for a single-user deployment? Make it config-gated; default on for hosted, off for self-host.

---

## Monitoring & Dashboards

*(TBD — user is thinking through what they want.)*

Will likely cover: in-app operator dashboards (system health, per-agent latency
+ success, sandbox usage, memory-system stats, self-improvement-loop
visibility, channel activity, cost-by-task rollups), surfacing through the
existing React app rather than the hosted-only CloudWatch path in `saas.md`.

---

## Other v3 candidates

Loose backlog — likely-v3 items pulled from `implementation_plan.md`'s
"Deferred to v2+" section. Each gets its own section here when scoped:

- **Browser extension** — last-resort general-purpose actuator for sites with no API; piggybacks on the user's real browser sessions (1Password pattern). Always stops at `ask_user` confirmation before destructive actions.
- **Skills marketplace ("Pawhub")** — share/import skills across users.
- **Native mobile app** — pending v2 Telegram-as-mobile-experience verdict.
- **Gmail write / Outlook mail / OneDrive / iCloud / Plaid / GitHub** integrations.
- **iMessage / WhatsApp** channels.
- **Entity / Summary / Knowledge-Base memory types** — if v2's tiered conversational memory + skills auto-emission don't cover the gap.

---

## Out of scope

Same exclusions as v1 + v2:
- SaaS deployment infra — [`saas.md`](saas.md) (private)
- OS-level browser-driving, native sync helpers, cloud-side Playwright — explicitly off the roadmap (architecture commitment in [`implementation_plan.md`](implementation_plan.md))
