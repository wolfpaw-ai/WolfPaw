# Wolfpaw

### *Tread lightly.*

A careful, capable, general-purpose AI worker for people who want an intelligent employee in the channels they already use — web, Telegram, email — without the setup tax of running their own agent.

---

## What is Wolfpaw?

Wolfpaw is an agentic *worker*, not just an assistant. It runs code in a sandbox, takes on multi-day tasks, produces real deliverables (spreadsheets, PDFs, slide decks, charts), and works in the background while you're doing other things. You decide what it can see and how you want to talk to it. You control your data; Wolfpaw earns its keep through quiet, careful, useful work.

Its motto, *Tread lightly,* is also its design constraint: take the smallest action that meets the goal, ask before doing anything destructive, surface uncertainty plainly, and never surprise the user with a hidden bill. Wolfpaw learns the person it works for over time — their tasks, their preferences (the User File), their past plans (procedural memory) — but it stays general, not specialist.

**One sentence:** *Wolfpaw is the trustworthy long-running agent for the channels you already use — at a cost you can see, with capabilities you explicitly grant.*

## Two ways to use Wolfpaw

- **Wolfpaw Cloud (paid, hosted).** Sign up at wolfpaw.ai, pick a plan, talk to your agent in minutes. No API keys, no installation, no developer setup. Inference is included up to your tier's allowance. Pay-as-you-go is opt-in, never automatic.
- **Wolfpaw Open Source (self-hosted, free).** Same code, deployable to your own server, laptop, or homelab. Bring your own API keys. Operate your own Telegram bot. You own everything. License TBD.

One codebase, two distributions. The hosted product exists so non-developers can use Wolfpaw without setup; the OSS version exists so developers can hack on it, run it privately, and verify what it does with their data.

---

## Usage

Wolfpaw is channel-native: you reach it where you already work.

### Channels

- **Web chat** — sign in at `wolfpaw.ai` (or your self-host URL), type in the chat box. Replies stream live. *(Backend endpoint built; React UI lands step 19.)*
- **Telegram** *(v1, step 18)* — DM `@WolfpawBot` after linking your account from web. One shared bot for hosted; OSS users provision their own via BotFather.
- **Email** *(v1.5, step 23)* — forward anything to `alice@wolfpaw.ai`; Wolfpaw reads, plans, and drafts a reply back to your verified inbox. Never sends on your behalf.
- **Slack** *(v2, step 25)* — workspace install, slash command + DM.

### Slash commands

Intercepted before the model in every channel so they don't burn tokens.

| Command | Behavior |
|---|---|
| `/help` | List available commands |
| `/usage`, `/usage today`, `/usage month`, `/usage all` | Token + sandbox-compute spend. `month` adds a by-agent breakdown. |
| `/tasks`, `/task <id>`, `/cancel <id>` | Task list + control *(step 15)* |
| `/reset` | Start a new conversation thread *(step 11)* |

### Workspace

Upload files for Wolfpaw to work with; pick up deliverables it produces. Same workspace whether you arrived via web, Telegram, or email — per-user prefix in S3 for hosted, local FS for self-host. v1 is a flat namespace; folders and Drive / Dropbox sync are v2.

### Tasks

Anything beyond a single chat turn becomes a **Task** — a persistent unit of work that can run for hours or days, pause when blocked, and ping you on your preferred channel when it needs input or has results ready. You see status, spend, and artifacts in the web app; cancel at any time. *(Step 15.)*

### Cost control

Every model call and every sandbox-second is metered. Hard pause at allowance — no silent overage. Opt-in overage with a per-user cap. Notifications at 50% / 80% / 100% of allowance. `/usage` is the always-on receipt.

### Trying it locally today (steps 1–8 shipped)

```bash
uv sync --extra dev
.venv/bin/uvicorn wolfpaw.api:app --reload
```

```bash
# Mint a magic link (the `console` email backend prints the URL to logs)
curl -X POST localhost:8000/auth/magic-link \
  -H 'content-type: application/json' \
  -d '{"email": "you@example.com"}'

# Follow the printed URL → sets the wp_session cookie

# Try a slash command via the SSE chat endpoint
curl -N -X POST localhost:8000/channels/web/chat \
  -H 'content-type: application/json' \
  -b 'wp_session=<value from /auth/verify>' \
  -d '{"content": "/usage"}'
```

Non-slash messages currently return a placeholder — the Quick Agent (step 10) is what makes free-form chat actually do something.

---

## How Wolfpaw differs from its neighbors

| System | Optimized for | Wolfpaw's contrast |
|---|---|---|
| **OpenClaw** | Open-source, local-first, full system access on a power user's own hardware. Hackable kernel, mature ecosystem. | Wolfpaw is hosted-first and intentionally conservative on local capability. Audience is people who *don't* want an agent with root on their laptop. Same OSS option for those who do. |
| **NanoClaw** | A minimal agent kernel — small, focused, a building block you wire up yourself. | Wolfpaw is a full product, not a kernel: channels, tasks, billing, memory, sandbox, observability all in one. Wolfpaw borrows NanoClaw's credential-vault pattern but ships the whole stack around it. |
| **Claude Desktop** | A first-party chat client for Claude on macOS / Windows, with MCP tool integration and local file access. Conversational, in-the-moment. | Wolfpaw runs *across* sessions, not inside one. It owns long-running tasks that pause when blocked and resume across days, pings you on whichever channel suits the moment, and produces real deliverables — not just a chat reply. |
| **Claude Cowork** | Anthropic's hosted agentic system for knowledge workers — bundled inference, desktop companion, deep first-party integrations (Drive, Gmail, Slack, DocuSign, FactSet). | Wolfpaw differentiates on channel mix (Telegram + email forwarding, not desktop), explicit cost mechanics (hard pause at cap, opt-in overage, `/usage`), read-only-by-default scopes, model portability (v2), and the OSS / self-host distribution. |

The through-line: **OpenClaw maximizes capability on your own machine. NanoClaw is the minimum viable kernel. Claude Desktop is a chat client. Cowork is the model vendor's first-party hosted agent. Wolfpaw is the careful, channel-native worker that gets real work done at a cost you can see.**

A longer feature-by-feature comparison lives in [`spec.md`](spec.md).

---

## Architecture

The full system flow is captured in the architecture diagram:

[**WolfPaw architecture diagram (PDF)**](WolfPaw_00.pdf)

<object data="WolfPaw_00.pdf" type="application/pdf" width="100%" height="700px">
  <a href="WolfPaw_00.pdf">View WolfPaw_00.pdf</a>
</object>

A user message — typed in the web app, sent to the Telegram bot, or forwarded by email — flows through a structured loop:

1. **Triage.** A small fast model (Haiku) figures out what the user actually wants and how big a job it is. Conditioned on the Soul File (agent persona) and the User File (the user's persona and preferences).
2. **Plan** (when the job is non-trivial). A larger model (Sonnet, sometimes Opus) checks procedural memory first — *have we solved something like this before?* — then assembles a plan. If the work is multi-step or produces deliverables, Wolfpaw creates a **Task**: a persistent unit of work that can run for hours or days, pause when blocked, and resume later.
3. **Execute.** Each step runs as a functional step (pure tool call), a reasoning step (model), or an evaluation step. Big plans branch into parallel sub-agents with their own budgets, then synthesize their outputs. Code runs in a sandboxed Python environment.
4. **Evaluate.** The Post-Evaluator scores the outcome, persists the plan into procedural memory (a recipe-box of description + ingredients + steps), and — when the result is reusable — emits a generalized **Skill** the planner can pull next time.

### Runtime wiring (top-level)

```mermaid
classDiagram
    direction LR
    class InboundMessage
    class TriageAgent
    class QuickAgent
    class PlanningAgent
    class Executor
    class PostEvaluator
    class ProceduralMemory
    class SkillsMemory
    class Task
    class Sandbox
    class ModelClient

    InboundMessage --> TriageAgent
    TriageAgent --> QuickAgent : simple
    TriageAgent --> PlanningAgent : non-trivial
    PlanningAgent --> ProceduralMemory : 1st step
    PlanningAgent --> Executor
    Executor --> Sandbox : tool calls
    Executor --> PostEvaluator
    PostEvaluator --> ProceduralMemory : persist
    PostEvaluator --> SkillsMemory : "Creates skills"
    Executor --> Task : long-running
    TriageAgent --> ModelClient
    QuickAgent --> ModelClient
    PlanningAgent --> ModelClient
    Executor --> ModelClient
    PostEvaluator --> ModelClient
```

Five detailed class views below; full UML doc with the box-to-class crosswalk lives in [`docs/uml_class_diagram.md`](docs/uml_class_diagram.md).

<details>
<summary><strong>View 1 — Channels, Persona, Triage</strong></summary>

```mermaid
classDiagram
    direction LR

    class Channel {
        <<abstract>>
        +str name
        +receive(payload) InboundMessage
        +send(user_id, content)
        +supports_streaming() bool
    }
    class WebChannel
    class TelegramChannel
    class EmailChannel
    class SlackChannel
    Channel <|-- WebChannel
    Channel <|-- TelegramChannel
    Channel <|-- EmailChannel : v1.5
    Channel <|-- SlackChannel : v2

    class InboundMessage {
        +UUID user_id
        +UUID thread_id
        +str content
        +str channel_name
    }

    class SlashCommandDispatcher {
        +dispatch(InboundMessage) Response|None
    }

    class Soul {
        +str version
        +str content_md
        +load() Soul
    }

    class UserProfile {
        +UUID user_id
        +int version
        +str persona_md
        +dict preferences
        +str timezone
        +load(user_id) UserProfile
    }

    class TriageAgent {
        +str model
        +classify(InboundMessage, Soul, UserProfile) TriageResult
    }

    Channel ..> InboundMessage : produces
    InboundMessage --> SlashCommandDispatcher
    SlashCommandDispatcher --> TriageAgent
    TriageAgent --> Soul : reads
    TriageAgent --> UserProfile : reads
```

</details>

<details>
<summary><strong>View 2 — Planning Loop</strong></summary>

```mermaid
classDiagram
    direction TB

    class PlanningAgent {
        +str model
        +bool can_use_opus
        +plan(query, Soul, UserProfile) Plan
        +adapt(past_plan, query) Plan
    }

    class Plan {
        +UUID id
        +str query
        +Vector query_embedding
        +List~Step~ steps
        +bool success
        +int score
    }

    class Step {
        <<abstract>>
        +UUID id
        +StepStatus status
        +int parallel_group
        +run(ExecutionContext) StepResult
    }

    class FunctionalStep
    class ReasoningStep
    class EvaluationStep
    Step <|-- FunctionalStep
    Step <|-- ReasoningStep
    Step <|-- EvaluationStep

    class PlanPreEvaluator {
        +evaluate(Plan, List~Plan~ past_plans) PreEvalVerdict
    }
    note for PlanPreEvaluator "v2 — three checks:\nachieves objective? simplifiable?\nimprovement over past plans?"

    class ToolCreatorAgent {
        +propose_tool(gap) Tool
    }
    note for ToolCreatorAgent "v2"

    PlanningAgent --> Plan
    PlanningAgent --> ProceduralMemory : "1. check first"
    PlanningAgent --> ToolRegistry : "get_tools_for_task"
    PlanningAgent --> ToolCreatorAgent : if gap
    PlanningAgent --> PlanPreEvaluator : v2
    Plan o-- Step
```

</details>

<details>
<summary><strong>View 3 — Execution, Tasks, Sub-agents</strong></summary>

```mermaid
classDiagram
    direction TB

    class QuickAgent {
        +str model
        +answer(InboundMessage, ToolRegistry) Response
    }

    class Executor {
        +execute(Plan, ExecutionContext) ExecutionPlan
        +run_step(Step) StepResult
    }

    class ExecutionPlan {
        +Plan plan
        +PlanStatus status
        +Dict results
    }

    class ExecutionContext {
        +UUID trace_id
        +User user
        +Soul soul
        +UserProfile user_profile
        +Task task
        +Sandbox sandbox
        +ToolRegistry tools
        +ModelClient models
    }

    class Task {
        +UUID id
        +UUID parent_task_id
        +TaskStatus status
        +int budget_cents
        +int spent_cents
        +str blocking_reason
        +str schedule_pattern
    }

    class TaskStatus {
        <<enumeration>>
        pending
        running
        blocked
        awaiting_user
        completed
        failed
        cancelled
    }

    class Artifact {
        +str filename
        +str mime_type
        +str storage_url
    }

    class PostEvaluator {
        +str model
        +evaluate(ExecutionPlan) PostEvalResult
        +persist(Plan, score) None
        +maybe_emit_skill(Plan) Skill
    }
    note for PostEvaluator "Diagram edge:\n'Creates skills'."

    Executor --> ExecutionPlan
    Executor --> ExecutionContext
    Task "1" o-- "*" Task : parent/child
    Task "1" o-- "*" Plan
    Task "1" o-- "*" Artifact
    ExecutionPlan --> PostEvaluator
    PostEvaluator --> ProceduralMemory
    PostEvaluator --> SkillsMemory : v2
```

</details>

<details>
<summary><strong>View 4 — Memory, Tools, Sandbox</strong></summary>

```mermaid
classDiagram
    direction LR

    class ConversationalMemory {
        +fetch_recent(n) List~Message~
        +fetch_summaries() List~ThreadSummary~
        +search_relevant(query_embedding, k) List~Message~
        +append(Message)
    }
    note for ConversationalMemory "Per-thread tiered (v1):\nverbatim window + L1/L2 summaries\n+ vector recall at Planner."

    class ProceduralMemory {
        +search_similar(query_embedding, k) List~Plan~
        +store(Plan)
    }
    note for ProceduralMemory "Recipe-box:\ndescription, ingredients, steps, score."

    class SkillsMemory {
        +store(Skill)
        +search_by_task(query_embedding) List~Skill~
    }
    note for SkillsMemory "v1: retrieval + seeded starter set.\nv2: auto-emission by PostEvaluator."

    class Skill {
        +str name
        +str description
        +dict ingredients
        +List~Step~ steps
        +UUID source_plan_id
    }

    class ToolRegistry {
        +register(Tool)
        +get_tools_for_task(query) List~Tool~
    }

    class Tool {
        <<abstract>>
        +str name
        +dict signature
        +bool needs_sandbox
        +run(inputs, ExecutionContext) ToolResult
    }
    class WebSearchTool
    class HttpGetTool
    class CalculatorTool
    class SqlQueryTool
    class CreateTableTool
    class ReadDocTool
    class WriteDocTool
    class RunPythonTool
    class CreateSpreadsheetTool
    class CreatePdfTool
    class CreateChartTool
    class CreateSlidesTool
    class AskUserTool
    Tool <|-- WebSearchTool
    Tool <|-- HttpGetTool
    Tool <|-- CalculatorTool
    Tool <|-- SqlQueryTool
    Tool <|-- CreateTableTool
    Tool <|-- ReadDocTool
    Tool <|-- WriteDocTool
    Tool <|-- RunPythonTool
    Tool <|-- CreateSpreadsheetTool
    Tool <|-- CreatePdfTool
    Tool <|-- CreateChartTool
    Tool <|-- CreateSlidesTool
    Tool <|-- AskUserTool

    class Sandbox {
        <<abstract>>
        +start()
        +stop()
        +run_python(code, timeout) ExecResult
        +read_file(path) bytes
        +write_file(path, content)
    }
    class E2BSandbox
    class DockerSandbox
    Sandbox <|-- E2BSandbox
    Sandbox <|-- DockerSandbox

    class CredentialProxy {
        +allow(credential, destinations)
        +proxy(request) response
    }
    Sandbox --> CredentialProxy : egress through

    ToolRegistry o-- Tool
    SkillsMemory o-- Skill
```

</details>

<details>
<summary><strong>View 5 — Model client, metering, observability, identity</strong></summary>

```mermaid
classDiagram
    direction TB

    class ModelClient {
        +call(user_id, agent, model, messages, prompt_version_id) ModelResponse
    }

    class Enforcer {
        +check_can_spend(user_id) None
    }
    class Recorder {
        +write(user_id, agent, model, prompt_version_id, usage, cost_cents)
    }
    class Pricing {
        +compute(model, usage, at_time) int
    }
    class PromptVersionStore {
        +active(agent) PromptVersion
        +bump(agent, version_label, content)
    }
    class PromptVersion {
        +UUID id
        +str agent
        +str version_label
        +str content_hash
    }
    class LangSmithClient {
        +bool enabled
        +trace(agent, model, trace_id) ContextManager
    }
    class CostNotifier {
        +maybe_send(user_id, cost_cents)
    }

    ModelClient --> Enforcer
    ModelClient --> LangSmithClient
    ModelClient --> Recorder
    ModelClient --> Pricing
    ModelClient --> PromptVersionStore
    ModelClient --> CostNotifier

    class User {
        +UUID id
        +str email
    }
    class Subscription {
        +str tier
        +int allowance_cents
        +bool overage_authorized
    }
    User "1" --> "1" Subscription

    class TokenUsage {
        +int input_tokens
        +int output_tokens
        +int cost_cents
    }
    class ComputeUsage {
        +int compute_seconds
        +int cost_cents
    }
    Recorder --> TokenUsage
    Sandbox --> ComputeUsage

    class SleepCycle {
        +run()
        +reorganize_procedural(user_id)
        +rescore_plans(user_id)
    }
    note for SleepCycle "v2 — cron job"
```

</details>

---

## Current repo layout

```
wolfpaw/
  pyproject.toml
  README.md                          # this file
  LICENSE                            # TBD
  spec.md                            # product spec + neighbor comparison
  implementation_plan.md             # schema, infra, build order (✅ marks shipped steps)
  soul.md                            # agent persona
  WolfPaw_00.pdf                     # architecture diagram (canonical)
  docs/
    uml_class_diagram.md             # anticipated class structure (Mermaid)
    decisions/                       # architecture decision records
      framework-choice.md
      observability.md
  migrations/
    001_init.sql                     # users, threads, plans, tasks, sandboxes, token_usage, …
    002_auth.sql                     # magic_link_tokens
    003_sandbox.sql                  # nullable task_id on sandboxes + compute_usage
  src/wolfpaw/
    api.py                           # FastAPI app — mounts auth, web channel, workspace
    config.py                        # env, model IDs, feature flags, backend selection
    tracing.py                       # trace_id contextvar + structured JSON logger
    agents/                          # Quick Agent (step 10); Triage/Planner/Executor land 11+  [README]
    auth/                            # magic-link auth, sessions, user bootstrap  [README]
    channels/                        # Channel ABC, web SSE channel, /help, /usage  [README]
    memory/                          # asyncpg pool + conversational memory (verbatim window)  [README]
    metering/                        # cost recording, prompt versions, ModelClient, /usage  [README]
    sandbox/                         # Sandbox ABC, Subprocess/Docker/E2B providers, manager  [README]
    storage/                         # Storage ABC, LocalStorage, S3Storage, signing  [README]
    toolbox/                         # tool registry + 15 tools (info, docs, SQL, sandbox, artifacts)  [README]
    workspace/                       # workspace_files DAO + REST API  [README]
  tests/
    test_*.py                        # 122 unit + 33 DB-gated tests as of step 8
```

Each subfolder has its own README — start there when extending that domain. The grouping matches the architecture views: every diagram block has a home, and the cross-cutting concerns (metering, tracing) are sibling packages rather than mixed into the agents.

**Not built yet:** `billing/` (step 24), `tasks/` (step 15), `workers/` (step 12.5 + 15), `web/` React frontend (step 19), `infra/` Terraform (step 21). The implementation plan tracks order and scope.

---

## Status

Steps 1–10 of the v1 build order are shipped (see [`implementation_plan.md`](implementation_plan.md) for the numbered list with ✅ markers). Concretely:

- **Foundation, DB, auth, metering** (steps 1–4) — FastAPI app + `001_init.sql` + magic-link auth + the `ModelClient` wrapper that records every token call.
- **Channels + slash commands** (step 5) — `Channel` ABC, web SSE endpoint, `/help` dispatcher.
- **`/usage`** (step 6) — first-class command, prices each call at its own `created_at` via LATERAL join, 30s cached.
- **Workspace + info/data tools** (step 7) — `Storage` ABC, LocalStorage + S3 adapters, workspace REST API, and seven tools: `calculator`, `http_get`, `web_search`, `sql_query`, `create_table`, `read_doc`, `write_doc`.
- **Sandbox** (step 8) — `Sandbox` ABC, Subprocess (dev/test default, real REPL), Docker, and E2B providers, `SandboxManager`, compute metering, and four sandbox tools.
- **Artifact tools** (step 9) — `create_spreadsheet`/`create_chart`/`create_slides`/`create_pdf`, all running inside the sandbox via a shared `_artifact.py` helper; outputs land in `workspace_files` with `source='agent_output'`.
- **Quick Agent + conversational memory** (step 10) — Haiku-driven tool loop over the 7 non-sandbox tools, hard-capped at 10 iterations. `memory/conversational.py` ships the verbatim recent window (threads + messages). The web channel routes non-slash messages through the agent and streams `thread` / `tool` / `delta` / `done` SSE events. **First step where `/usage` shows real numbers.**

**Tests:** `pytest` runs 146 unit tests in <1s; 40 DB-gated tests skip without a `WOLFPAW_TEST_DATABASE_URL`.

**Next up:** step 11 (Triage Agent — routes between Quick / Plan / Task), then Planner + procedural memory (12), tiered conversational compaction (12.5), Executor (13).

The repo currently lives inside [`dmitris-fabulous/wolfpaw/`](.) for incubation; it will move to its own standalone repo before public release.

---

## Documentation map

| File | What it covers |
|---|---|
| [`README.md`](README.md) | This file — pitch, comparison, architecture overview, status |
| [`spec.md`](spec.md) | Product spec; long feature-by-feature comparison with OpenClaw and Cowork; scope decisions |
| [`implementation_plan.md`](implementation_plan.md) | Locked-in decisions, Postgres schema, tools, sandbox, tasks, metering, build order |
| [`soul.md`](soul.md) | Agent persona — loaded into every agent prompt |
| [`WolfPaw_00.pdf`](WolfPaw_00.pdf) | Canonical architecture diagram |
| [`docs/uml_class_diagram.md`](docs/uml_class_diagram.md) | Anticipated class structure across five views, plus diagram-box-to-class crosswalk |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records (framework choice, observability) |
| `src/wolfpaw/<subpackage>/README.md` | Per-domain orientation for maintainers — one in each of `auth/`, `channels/`, `memory/`, `metering/`, `sandbox/`, `storage/`, `toolbox/`, `workspace/`. Start here when extending that domain. |

---

## License

TBD. Will be open-source-friendly; specific license decided later.
