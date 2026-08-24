# Implementation Plan: Content-by-Reference (Blackboard)

**Status:** Proposed · Phase 0 complete · **Phase 1 blocked pending scope decision — see Open Issues below.**
**Date:** 2026-07-23 · **Revised 2026-08-24** — phases reordered so deterministic
work ships before prompt-dependent work; sentinel changed from `$ref` to
`__ref__`; resolution failures now bypass the input-repair loop; the synthesis
prompt added as a third truncation site.

## Open Issues (paused mid-session, resume here)

### 1. Phase 1's dependency signal doesn't exist

Phase 1 as written says: un-clip prior-step content and inject the full bytes "for the steps the current step actually depends on." That framing assumes the executor knows which prior step a given step reads from. It doesn't. [`Step`](src/wolfpaw/schemas.py) has `id`, `kind`, `description`, `tool`, `inputs`, `parallel_group` — no dependency field. `_build_step_prompt` currently dumps **every** prior result (clipped to 2 KB each), and there is no signal available to narrow that.

Consequence: Phase 1 as written can't do what its acceptance criterion says. Removing the clip without narrowing which steps get injected makes prompts strictly **bigger**, which is the opposite of what this whole plan is for. The "deterministic Phase 1 → prompt-dependent Phase 2" framing in the Phasing Principle table quietly assumed a dependency edge that isn't in the data model.

### 2. Options for resolving it

**A. Merge Phase 1 into Phase 2.** The refs in a step's `inputs` *are* the dependency signal: if step 4 carries `{"__ref__": "step:3.output"}`, then step 4 depends on step 3 and step 3 gets injected in full. Steps whose outputs nobody references don't get injected at all. But that needs the planner emitting refs, so there's no separable pre-Phase 2 phase — ship them as one.

**B. Add `depends_on: list[str]` to `Step`.** Explicit dependency list the planner fills. Simpler planner-prompt change than teaching refs ("list which prior step ids this reads"), and the executor consumes it deterministically. Keeps Phase 1 as a real phase, but does need one planner prompt update — no longer purely deterministic.

**C. Split Phase 1 into infrastructure-only (recommended).** Ship the store + handle-writing for step outputs. Don't touch `_build_step_prompt` at all. Acceptance changes from "step sees full content" to "step outputs are addressable by handle by end of Phase 1." Phase 2 then does both the output side (planner emits refs in `inputs`) **and** the input side un-clipping, using those refs as the dependency signal. Smallest honest Phase 1 — pure infra, no behavior change, no prompt work.

Recommendation: **C**. Matches how the dependency signal actually enters the system (via refs), keeps Phase 1 side-effect-free, and puts both prompt-touching changes in one phase where they belong.

### 3. Non-issue: prompt-versioning with two running instances

Investigated during the same session, confirmed not a blocker. `prompt_versions` has one row per `(agent, label)`; `bump_prompt_version` upserts on that key at process startup. With two instances of Wolfpaw running, whichever booted last overwrites the row — but `get_active_prompt_version` has **zero callers**. The table is write-only bookkeeping today; agents use hardcoded in-code constants. So the row-clobbering only misattributes which prompt text produced a logged call, and doesn't affect behavior. Bumping `VERSION_LABEL` when Phase 2 lands is cosmetic; do it for hygiene, not correctness.

Deeper implication (for a later ADR, not this plan): the versioning table was designed for an admin-editable, DB-driven prompt path that was never wired. Either build that read path or drop the table — carrying it as-is is a design half-move.

### 4. Also captured for later

- The "As built" note on Phase 0 originally cited a `scripts/blackboard_baseline.py` measurement tool. Deleted mid-session — nothing gated on the measurement, and the retention window on `model_call_logs` (14 days default) made a "run once before, once after" comparison shaky anyway. Warning-based counting from the structured logs is enough.
- If Phase 2 lands as recommended: prompts get bigger for a step that references a large predecessor, and smaller for the step that produced the predecessor's content (because `write_doc` etc. no longer re-emit it). Net direction depends on plan shape. Worth measuring after Phase 2 rather than predicting.
**Motivation:** the `write_doc` silent-failure bug (found via the model-call trace store) — a document routed through the model as tool-call arguments overran `max_tokens` and truncated. Raising the budget treated the symptom; this plan removes the root cause.

## Problem

Data between the model and tools, and between steps, currently flows **through the model in both directions**:

- **Output side.** A functional step's `inputs` are values the model generates. `write_doc`'s `content` is model output — so saving a pasted resume means the model re-emits the entire resume as tokens. That is what truncated.
- **Input side.** [`executor._build_step_prompt`](src/wolfpaw/agents/executor.py) hands a step its predecessors' results by inlining them into the prompt as text, **truncated at `_PRIOR_RESULT_TRUNCATE`** (2 KB). Large prior output is silently clipped before the next step sees it.
- **Synthesis side.** [`executor._build_synthesis_prompt`](src/wolfpaw/agents/executor.py) formats *every* step result through the same `_format_prior_result` helper under a separate `_SYNTHESIS_TRUNCATE` cap. The final user-facing answer is composed from clipped inputs. Same helper, same fix as the input side — but worth naming separately because it's the one the user actually reads.

All three are the same anti-pattern: the model acting as a photocopier for content that already exists. The cost is tokens, latency, and — when content exceeds the output budget — silent data loss.

## Core idea

Give existing content a **handle** in a task-scoped store. Let `Step.inputs` (and, later, model-emitted tool inputs) carry a **reference** — `{"__ref__": "<handle>"}` — instead of the value. The **executor resolves the reference to real bytes immediately before calling the tool**, so:

- Tools stay plain functions that receive real values. `Tool.run(ctx, **inputs)` is unchanged; no tool is rewritten. Every existing tool gains reference support for free.
- The content travels store → function, never through the model's output.
- The model's job shrinks to *deciding to act*, *naming the target*, and *wiring the reference* — all small.

This is the **blackboard** pattern; Anthropic's *programmatic tool calling* is the managed equivalent (model writes a script, tool results stay in the execution environment, only the final result returns to context). We run a custom loop on the SDK, so we build the lightweight version ourselves.

## What stays a model call (scope boundary)

Content the model **authors** — a summary it writes, an analysis it produces — does not exist until generated, so it has no handle to reference and must come out as tokens. That generation is bounded by the task and is work we want. The blackboard only removes the round-trip for content that **already exists**: user-pasted text, prior step/tool outputs, existing workspace files.

## Design decisions

### 1. The store

A durable, task-scoped content store. **Durable, not in-RAM**, because a Task can run in the arq worker process and pause on `ask_user` (`pending_questions`) — content set aside must survive a process boundary and a wait. In-RAM is only a per-execution cache; the source of truth is the store.

- New table `content_refs` (handle → metadata). Small values inline in a `JSONB`/`TEXT` column; large values spill to the existing `Storage` blob backend (Local/S3), mirroring how `workspace_files` holds a `storage_url`. The row carries `size_bytes`, `sha256`, and `mime` — **metadata only, no content preview**. Nothing about the bytes is auto-materialized into any model context; the model works from metadata and pulls samples on demand (see Phase 4, `inspect`).
- Scoped by `task_id` (and `user_id`) — a handle is never addressable from another task or user. Resolution enforces the scope.
- Retention tied to task lifecycle: purge on terminal transition, plus a sweep for orphans (a sibling of [`workers/jobs/prune_traces.py`](src/wolfpaw/workers/jobs/prune_traces.py)).

### 2. The reference type

A JSON shape the resolver recognizes anywhere in an `inputs` dict (including nested):

```json
{"__ref__": "step:3.output"}            // a prior step's output
{"__ref__": "msg:<message_id>"}         // an inbound message's content
{"__ref__": "file:<workspace_file_id>"} // an existing workspace file
{"__ref__": "handle:<named>"}           // a named blackboard entry
```

Handles are opaque strings with a typed prefix. `workspace_files` are **already** content-addressable by id — `file:` references reuse that.

**Why `__ref__` and not `$ref`.** `$ref` is JSON Schema's own reserved keyword, and every `Tool` carries an `input_schema` ([registry.py](src/wolfpaw/toolbox/registry.py)) that is sent verbatim to Anthropic as the tool definition. Using the same token for "blackboard handle" would mean the planner's `generate_plan` schema has to permit a `$ref` object at every `inputs` value — inside a document that also uses `$ref` structurally. Any future schema validation, `$defs` usage, or schema-composition step then has to disambiguate the two by context. An unambiguous sentinel costs nothing now and is annoying to change once plans are persisted in `plans`.

### 3. Resolution point (the key architectural choice)

Resolution happens in the **executor / tool-dispatch path**, transparently, so the tool layer never sees a reference. Both current dispatch sites call `tool.run(ctx, **inputs)`:

- [`executor._run_tool_with_recovery`](src/wolfpaw/agents/executor.py) (functional steps)
- [`quick._run_tool`](src/wolfpaw/agents/quick.py) (the quick agent's own loop)

Introduce one shared helper — `resolve_inputs(ctx, inputs) -> inputs` — called at both sites before `tool.run`. Dangling ref, cross-scope ref, or a ref that resolves too large for the tool → raise, **never** pass an unresolved sentinel through.

**What it raises matters: `ContentRefError`, deliberately *not* a `ToolError` subclass.** The obvious choice is `ToolError`, on the reasoning that it's loud and the existing repair loop can recover it. That reasoning is wrong here. The repair loop is [`_call_tool_with_recovery`](src/wolfpaw/agents/executor.py), which on `ToolError` calls `_propose_corrected_inputs` — a stateless Haiku call handed the step description, the tool schema, the failed inputs, and the error message, and asked to propose corrected inputs.

Give that model `{"content": {"__ref__": "step:3.output"}}` plus "handle not found," and the only correction available to it is to **replace the reference with a literal value it invents.** The repairer cannot read the store. We would be trading a silent-truncation bug for a silent-fabrication bug, which is strictly worse — truncated output looks wrong, hallucinated output looks fine.

So resolution failures propagate past the recovery path and fail the step cleanly. If a repair path is wanted later, the repairer must first be given the handle catalog, so "fix the ref" is an option it can actually take.

### 4. Handle producers

- **Inbound content:** an inbound message already lives in `messages` and is addressable by id. Triage's role is to recognize "this content is the subject of the request" and let the plan reference it by `msg:` handle rather than have the model re-emit it.
- **Step outputs:** after each functional step, store `StepResult.output` under `step:<id>.output` (spill to blob if large).
- **Model-authored outputs:** reasoning-step output is stored too, so a later step can reference it without a second inline copy.

### 5. Planner changes

- The `generate_plan` tool schema must permit a `__ref__` object wherever `inputs` values appear.
- The planner prompt learns the rule: *when a step needs content that already exists, reference it; do not copy it into the plan.* Include one worked example (the resume → `write_doc(content={"__ref__": ...})`).

## Phases

### Phasing principle — deterministic work ships first

The phases below are **not** ordered by conceptual scope (store → agents → sandbox). They're ordered by a property that matters more for actually landing the feature: **whether a model has to cooperate for the phase to work at all.**

| Work | Who must choose to use a reference | Ships deterministically? |
|---|---|---|
| Reasoning + synthesis steps read full prior content | nobody — the executor already knows a step's predecessors | ✅ yes |
| Step outputs get handles | nobody — executor writes them after each step | ✅ yes |
| `write_doc(content={"__ref__": ...})` | the **Planner**, at plan-generation time | ❌ prompt-dependent |
| Quick agent emits a reference | the **model**, mid-loop | ❌ prompt-dependent |

The output-side fix — the one that motivated this plan — is prompt-dependent: it only happens if the Planner chooses to emit a reference. If that prompt needs three iterations, a store-first phasing delivers a store nobody reads. The input-side fix needs no model cooperation whatsoever: `_build_step_prompt` already has the prior `StepResult` objects in hand and clips them at a hardcoded 2 KB.

So the deterministic half goes first. It exercises the schema, the scoping, and the spill behavior under real load **before** anything depends on a prompt landing correctly.

### Phase 0 — Make the silent loss loud ✅ **completed**

Instrument the clip sites. [`_format_prior_result`](src/wolfpaw/agents/executor.py) emits a structured warning (`executor.prior_result.truncated`, with `site`, `step_id`, `kind`, `original_bytes`, `cap`, `dropped_bytes`) whenever it actually clips — once per clipped step, on both the step-prompt and synthesis paths. No behavior change.

Phase 1's acceptance test asserts this event stops firing, so it has to exist first. Tests: [`tests/test_executor_truncation_unit.py`](tests/test_executor_truncation_unit.py).

### Phase 1 — Store + executor-side handles + the input/synthesis fix

Contains no model-dependent behavior.

- Migration for `content_refs` + the blob-spill convention. Mirrors how `workspace_files` holds a `storage_url`; the spill reuses the existing [`Storage`](src/wolfpaw/storage/base.py) ABC, whose `StorageObject` already returns exactly `storage_url` / `size_bytes` / `sha256`.
- `content_store.py`: `put(ctx, value) -> handle`, `get(ctx, handle) -> value`, scope-checked against `ctx.user_id` / `ctx.task_id` on every read.
- Store every functional step's output under a `step:` handle.
- `_build_step_prompt` and `_build_synthesis_prompt` resolve handles and inject the **full** content for the steps the current step actually depends on, replacing the truncated all-prior-results dump.
- `ContentRefError` defined and raised by the resolver.

**Acceptance:** a step whose predecessor produced 50 KB of output sees all 50 KB — proven by a test asserting no `executor.prior_result.truncated` warning fires on a plan that logged one in Phase 0. No prompt changes required to get there.

### Phase 2 — `resolve_inputs` + planner emits references

The output side, with the store already proven in production.

- `resolve_inputs(ctx, inputs)`, recursing into nested structures, wired into the executor's functional dispatch before `tool.run` (leave the quick agent for Phase 3). Literal inputs pass through byte-identical.
- Planner: schema + prompt + the worked resume example.
- `msg:` handles for inbound content, so Triage can mark "this content is the subject" without the model re-emitting it.
- **Acceptance:** the resume round-trips end to end, and the `write_doc` trace in `/monitor` shows the content **absent from the model's output** (`response_content`) and present on disk. This is the concrete proof the round-trip was removed, not just widened.

This is the phase that may need prompt iteration. Because Phase 1 already shipped, a slow landing here delays a win rather than blocking the feature.

### Phase 3 — Quick agent + handle catalog

- `resolve_inputs` at the second dispatch site, [`quick._run_tool`](src/wolfpaw/agents/quick.py).
- Surface available handles to the quick agent (a system-prompt catalog, or a `list_handles` / `read_handle` tool) so a directly-emitted tool call can reference them. This is the hardest adoption problem in the plan — the *model* must choose to emit a reference, so the catalog has to be legible to it — which is exactly why nothing earlier is load-bearing on it.

### Phase 4 — Sandbox↔store bridge (scripts operate on data by reference)

This is what makes "Wolfpaw writes scripts to programmatically manage data" work by reference instead of by inlining. The generic resolver (Phase 2) turns a reference into a *tool argument value*; a script instead needs its input as a **file inside the sandbox** and its output captured back out as a **new handle**. Without this, [`run_python`](src/wolfpaw/toolbox/tools/run_python.py) still tempts the model to paste data into the `code` string — the exact photocopier failure, relocated.

The pattern already half-exists: [`_dynamic_user_tool.py`](src/wolfpaw/toolbox/tools/_dynamic_user_tool.py) writes `inputs.json` into the sandbox and reads a `result` back. Generalize that into `run_python` (and, by extension, `DynamicUserTool`):

- **Input:** `run_python` gains an `input_handles` param (`{sandbox_path: {"__ref__": "<handle>"}}`). Before execution the executor resolves each handle and, via [`SandboxManager`](src/wolfpaw/sandbox/manager.py) + `sandbox_write_file`, materializes it at the given path. The `code` receives paths, never payloads.
- **Output:** `run_python` gains an `output_paths` (or glob) declaration; after a clean exit the executor reads those files out of the sandbox and stores each as a new `step:`/`handle:` entry, returning the handles — not the bytes — to the model.
- **Result:** a plan can chain `read a workspace file → run_python transforms it → write_doc saves the output`, with the data living in files and handles the whole way and never entering the model's output. This is the "store-and-point" loop end to end, with no in-sandbox tool bridge and no new trust surface (the sandbox still holds no credentials; the host mediates every materialize/capture).

**Understanding data before writing code — pull-only.** When a task needs the model to write code against uploaded data (e.g. "process this CSV"), the model is given the prompt plus the file **metadata** (`filename`, `mime`, `size_bytes`) and nothing else — no ingest-time preview, no per-filetype sampling boilerplate. If it needs to know more before writing code, it *pulls* what it wants via an `inspect(handle, ...)` tool that runs a bounded inspection in the sandbox against the full file by handle and returns a capped summary (e.g. header + first rows, dtypes, row count, distinct values) — never the raw data. The model decides what it needs to know; the host doesn't guess with a generic preview. This is the same store-and-point discipline: `inspect` reads the full file inside the sandbox and returns a bounded summary, not the bytes. Its output cap is the trace-sink cap discipline reused, so a pathological file can't defeat the point.

**Acceptance:** a script-driven data task (e.g. "dedupe this CSV and save the result") round-trips with the CSV contents absent from every `model_call_logs.response_content` in the trace.

### Phase 5 — Generalize + harden

- Retention/cleanup tied to task lifecycle; orphan sweep.
- Size policy (inline threshold vs. blob spill) as config.
- Ref cycle / self-reference detection.
- Metric: tokens saved per task (compare `model_call_logs` before/after), surfaced in `/monitor`.

## Testing

- **Unit:** `resolve_inputs` — nested refs, dangling ref → `ContentRefError`, cross-scope ref rejected, literal inputs pass through untouched (backward compat).
- **Regression guard on the repair loop:** a dangling ref must *not* reach `_propose_corrected_inputs`. Assert the repair path is never entered — a test that fails if `ContentRefError` ever becomes a `ToolError` subclass by accident.
- **DB-backed:** `content_store` put/get, blob spill above threshold, scope enforcement, purge on task terminal.
- **Input side (Phase 1):** a step whose predecessor produced more than `_PRIOR_RESULT_TRUNCATE` bytes sees the full content, and no `executor.prior_result.truncated` warning fires. Same assertion for the synthesis prompt.
- **E2E:** the resume round-trip, asserting content never appears in the model-call trace (query `model_call_logs.response_content`).
- **Sandbox bridge (Phase 4):** a handle materializes to the declared sandbox path before the script runs; declared output files are captured as handles after a clean exit; a script crash (`exit_code != 0`) captures nothing and surfaces the error, not a partial handle.
- Backward compatibility is a first-class test: every existing plan with literal `inputs` must behave identically.

## Risks & failure modes

- **Planner emits a malformed or dangling ref.** Mitigation: schema constrains the shape; resolver fails the step loud and clean. Deliberately *not* routed into the repair loop — see Design decision 3 for why "let the repairer fix it" produces fabrication.
- **Resolved content still exceeds a tool's own limits** (a genuinely huge doc). The blackboard removes the *model-output* ceiling but not downstream limits; tools validate their resolved inputs.
- **Reference adoption is model-dependent** — both for the Planner (Phase 2) and, harder, the quick agent (Phase 3). This is the reason for the phase ordering: Phase 0 and Phase 1 are entirely deterministic, so the plan delivers real wins even if the prompt work needs several iterations. Nothing before Phase 2 depends on a model choosing to emit a reference.
- **Scope leak** — a handle addressable across tasks/users would be a data-exposure bug. Scope check is mandatory in the resolver, tested explicitly.
- **Sandbox path collision / escape (Phase 4)** — a materialized `input_handles` path must be validated (absolute, inside the sandbox workdir, no traversal) exactly as the text-editor tool validates paths; a script must not be able to redirect a capture at an arbitrary host file. Capture reads happen inside the sandbox, so this is bounded by the backend's isolation — another reason the capture step is only as trustworthy as the sandbox backend (subprocess is not a boundary).

## Open questions

- Inline-vs-blob threshold: a fixed byte cap, or model-window-relative?
- Do reasoning-step outputs get handles by default, or only when a downstream step declares a dependency? (Default-on is simpler; costs store writes.)
- Should `file:` refs be readable across threads for the same user (a saved doc reused later), or strictly task-scoped? Leans toward user-scoped for files, task-scoped for intermediate `step:`/`handle:` entries.

## Out of scope

- The separate token-budget / prompt-assembly work (allocating the context window across memory tiers, adding a real `count_tokens` layer). Related but independent; the blackboard reduces how often that budget is stressed but doesn't replace it.
- Anthropic's server-side programmatic tool calling / code execution — a possible future substrate, not this plan.
