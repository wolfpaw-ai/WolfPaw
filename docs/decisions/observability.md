# ADR: Three-layer observability — CloudWatch for ops, LangSmith for LLM, custom for prompts

**Status:** Accepted
**Date:** 2026-05-07

## Context

Wolfpaw has three observability needs that don't share a natural single tool:

1. **Operational metrics and logs.** Request rate, latency, error rates, cap-pause counts, infra health — the things an on-call engineer pages on. Aggregate, structured, time-series.
2. **LLM-call detail.** Full prompts, completions, tool calls, intermediate state. Reviewed when debugging "why did the planner do that?" or running an eval pass over a dataset of past plans. Per-call, replay-shaped, slow access pattern.
3. **Prompt versioning.** Which prompt template produced which output, diffs across versions, "did the planner v8 prompt outperform v7 on the procedural-memory dataset?" Cross-cuts the other two and ties tightly to procedural memory's "how Wolfpaw solved similar problems before" semantics.

Each layer has a different consumer (operator vs. LLM engineer vs. prompt author) and a different access pattern. A single tool that does all three either does not exist or is poor at one of them.

## Decision

A three-layer split:

| Layer | Tool | Question it answers |
|---|---|---|
| Operational metrics + logs | CloudWatch (structured JSON) | "Is the system healthy? Where's the latency? Are users hitting their cap?" |
| LLM-call detail | LangSmith | "What did the planner generate on request X? Replay it. Diff prompts. Score against a dataset." |
| Prompt versioning | Custom (`prompt_versions` table) | "Which prompt template produced this output? Did v8 outperform v7 across the procedural-memory dataset?" |

`trace_id` is the through-line. It's emitted on every structured log line, attached to every LangSmith trace, and recorded on every `token_usage` row alongside `prompt_version_id`. Pulling a `trace_id` out of a CloudWatch log lands you on the matching LangSmith trace and the relevant `token_usage` rows.

## Alternatives considered

### All-in CloudWatch

Rejected. CloudWatch handles ops well but cannot replay an LLM call, diff prompts across versions, or anchor an eval workflow without months of custom UI work that would not earn its keep.

### All-in LangSmith

Rejected. LangSmith is not built for ops-tier dashboards, infra alerting, or on-call paging. Using it as the operational layer would mean either weak alerting or paying for a second product anyway.

### All custom

Rejected for v1. Re-implementing LangSmith's replay UI, dataset-runner, and prompt-version-diff workflow is multi-month work that produces a UI, not an architecture. No portfolio value, real time cost. (See also [framework-choice.md](framework-choice.md) on where NIH stops earning its keep.)

### Off-the-shelf prompt versioning (PromptLayer, Helicone, LangSmith's own prompt-management features)

Rejected. Prompt versioning in Wolfpaw has small surface area and tight coupling to procedural memory: we want to ask "which plans under planner prompt v7 scored highest?" using the same `plans` and `token_usage` tables we already query, and we want to scope plan retrieval to "produced under prompt version range X–Y" so old plans don't mislead a meaningfully evolved planner. Building this from scratch is on the order of one table plus a handful of helpers and keeps prompts versioned alongside the data they generated.

### OpenTelemetry tracing

Considered. Long-term-correct answer for cross-service tracing; adds setup complexity disproportionate to v1 needs (single FastAPI process). Reconsider when the system grows beyond one runtime.

## Consequences

- **Three places to look when debugging.** Mitigated by `trace_id` threading: a single `trace_id` pulled from a CloudWatch log line lands you on the LangSmith trace and on the matching `token_usage` rows.
- **LangSmith is a third-party data handler.** Privacy disclosure required pre-public-launch; the integration is gated by `LANGSMITH_ENABLED` so it can be disabled per environment. This decision interacts with the cloud-only / OSS positioning — see also [framework-choice.md](framework-choice.md) and the privacy posture in [spec.md](../../spec.md).
- **Custom prompt versioning adds one table plus a small surface.** `prompt_versions(id, agent, version_label, content_hash, content_template jsonb, created_at)`. `token_usage` gets `prompt_version_id` FK. Bumping a version is a manual, intentional action by the prompt author.
- **Procedural memory becomes versioned-by-prompt.** Plan retrieval can scope to "plans produced by planner prompt v8 or later." This matters because procedural memory is meant to encode "how Wolfpaw solved similar problems before"; if the planner prompt has materially changed, older plans may be misleading.
- **CloudWatch dashboards remain the on-call source of truth.** Latency, error rates, cap-pause counts. LangSmith is for LLM-quality investigations, not paging.

## Implementation notes

- `call_model()` wrapper records to all three: a `token_usage` row (with `prompt_version_id` FK), a structured log line, and a LangSmith trace tagged with `trace_id`, `user_id`, `agent`, `model`, `prompt_version_id`, and `task_id` when applicable.
- `prompt_versions` table seeded with v1 of each agent's prompt as that agent comes online (build-order steps 10+).
- Prompt-author workflow: edit the prompt template file → bump the version label → run an idempotent loader that inserts the new row → deploy. Future automation in v2 if churn justifies it.
- LangSmith client wired in `metering/langsmith_client.py` alongside `metering/recorder.py`; both gated on env config so dev, staging, and self-host can opt in or out independently.
