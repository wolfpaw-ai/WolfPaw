# ADR: Build the agent loop from scratch; adopt LangSmith for observability

**Status:** Accepted
**Date:** 2026-05-07

## Context

Wolfpaw is a multi-agent system: a triage agent, quick agent, planner, executor, and post-evaluator coordinated through a structured loop, with custom memory subsystems (conversational, procedural, and a planned ambitious extension), tasks as first-class persistent objects that survive across days, sub-agent delegation with parent-child budget rollup, sandboxed Python execution, and multiple inbound channels (web, Telegram, email).

Three plausible framework strategies were on the table for the agent orchestration and observability layer:

1. **Full LangChain / LangGraph / LangSmith** — adopt the ecosystem, build on its abstractions.
2. **From-scratch on the Anthropic SDK** — direct API calls, custom orchestration, custom memory.
3. **Hybrid** — direct SDK for the loop, selectively adopt parts of the LangChain ecosystem where they earn their place.

Wolfpaw doubles as a portfolio artifact. A senior reviewer should be able to skim `agents/planner.py` and `memory/procedural.py` and form a judgment about the author's design instincts in five minutes — something that's much harder when the agent loop is `from langgraph.prebuilt import create_react_agent` and the memory layer is a configured LangChain class.

## Decision

We adopt the hybrid approach with strong "from-scratch" defaults:

- **Agent loop, planner, executor, sub-agent coordination, all memory subsystems (conversational, procedural, and the planned ambitious extension), prompt versioning, channel abstraction, and tool registry** — all built from scratch on the Anthropic SDK.
- **LangSmith** — adopted for LLM-specific observability: per-call tracing, replay, eval datasets. Treated as a deliberate "use the right tool" choice for an observability slice that is expensive to replicate in-house and adds no portfolio value when reproduced.
- **CloudWatch + structured logs** — operational metrics, latency, error rates, cap-pause counts. (See [observability.md](observability.md) for the full split.)

## Alternatives considered

### Full LangChain / LangGraph / LangSmith

Rejected. Three reasons:

1. **The agent loop and memory layers are the most architecturally interesting code in Wolfpaw**, both for the design itself and for the portfolio goal. Wrapping them in framework abstractions hides exactly the judgment a reader is trying to evaluate.
2. **Several distinctive Wolfpaw patterns would fight the framework's assumptions** — the credential vault, soul-file persona injection, the multi-channel abstraction, and especially the planned ambitious memory system. Workarounds against the grain are slower and uglier than custom code aligned with the system's actual shape.
3. **Reputation drag in senior engineering circles is real**, even where the criticism is unfair to LangGraph specifically. For a portfolio piece, this matters.

### Hybrid: from-scratch loop + LangGraph for task checkpointing only

Held in reserve. LangGraph's checkpointing is a clean solution to the "tasks survive across days" problem. If our custom serialization for planner/executor state becomes a v1 timeline blocker, we will swap LangGraph in for that layer alone — not the agent loop, not memory.

This is documented as the explicit fallback so that, if the swap happens, it happens deliberately rather than by accident, and as a follow-up ADR rather than a quiet decision.

### Custom observability instead of LangSmith

Rejected for v1. Per-call replay, prompt-version diffs across runs, and dataset-driven evals are LangSmith's core competency, and replicating them is multi-month UI/tooling work with no portfolio value (a custom dashboard is not the architectural surface a reviewer is here to evaluate). Adopting LangSmith here is a deliberate "use the right tool" decision; building this from scratch would be NIH-flavored.

**Prompt versioning specifically is the exception** — built from scratch because it has small surface area, sits naturally inside our existing metering schema, and is tightly coupled to procedural memory in ways no off-the-shelf solution maps to. See [observability.md](observability.md).

## Consequences

- **Roughly 2–4 weeks more v1 work** on the agent orchestration layer than the LangGraph path. Worth it for the depth of understanding gained and for the portfolio surface that emerges.
- **We own task-state serialization, sub-agent budget rollup, retries, streaming, and all memory subsystems.** All documented in [implementation_plan.md](../../implementation_plan.md).
- **LangSmith introduces a third-party data handler** (prompts and completions traverse a vendor). Pre-public-launch privacy disclosure required; LangSmith forwarding is gated by a config flag (`LANGSMITH_ENABLED`) so it can be disabled per environment if needed.
- **The repo's "interesting code" is actually our code.** A reviewer reading `agents/planner.py`, `memory/procedural.py`, or `metering/recorder.py` is reading our judgment, not framework idioms.
- **Fallback path is named.** If task persistence becomes painful, we adopt LangGraph for checkpoints alone, document the swap in a follow-up ADR, and keep the rest of the system as designed.
