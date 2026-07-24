# Wolfpaw — UML Class Diagram (v1)

Anticipated classes for the agent. Maps every box in [`WolfPaw_00.pdf`](../WolfPaw_00.pdf) to a Python class (or abstract base + adapters), plus the supporting infrastructure called out in [`implementation_plan.md`](../implementation_plan.md). v2-only classes are marked `[v2]`.

The diagram is split into five views to stay readable. Open this file in a Markdown viewer that renders Mermaid (GitHub, VS Code with the Mermaid extension, Obsidian).

---

## View 1 — Channels, Persona, and Triage

The user-facing edge: a message comes in on a channel, hits the slash-command dispatcher, and (if not a built-in command) enters the agent pipeline conditioned on the Soul File and the User File.

```mermaid
classDiagram
    direction LR

    class Channel {
        <<abstract>>
        +str name
        +receive(payload) InboundMessage
        +send(user_id, content, **kwargs)
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
        +dict raw
    }

    class SlashCommandDispatcher {
        +dispatch(InboundMessage) Response|None
        -handlers: dict
    }

    class Soul {
        +str version
        +str content_md
        +load() Soul
        +render_system_block() str
    }

    class UserProfile {
        +UUID user_id
        +int version
        +str persona_md
        +dict preferences
        +str timezone
        +load(user_id) UserProfile
        +save()
        +render_system_block() str
    }

    class TriageAgent {
        +str model = "haiku-4-5"
        +classify(InboundMessage, Soul, UserProfile) TriageResult
    }

    class TriageResult {
        +Path path
        +Severity severity
        +bool needs_task
        +str rationale
    }

    Channel ..> InboundMessage : produces
    InboundMessage --> SlashCommandDispatcher
    SlashCommandDispatcher --> TriageAgent : if no command match
    TriageAgent --> Soul : reads
    TriageAgent --> UserProfile : reads
    TriageAgent --> TriageResult
```

---

## View 2 — Planning Loop

The Planner is its own agentic loop: pull from procedural memory first, get tools for each step, optionally call the Pre-Evaluator, optionally request a new tool, and emit a Plan. Each Step is one of three subtypes (functional / reasoning / evaluation), matching the diagram's labeling.

```mermaid
classDiagram
    direction TB

    class PlanningAgent {
        +str model = "sonnet-4-6"
        +bool can_use_opus
        +plan(query, Soul, UserProfile, ConversationalMemory) Plan
        +adapt(past_plan, query) Plan
    }

    class Plan {
        +UUID id
        +UUID user_id
        +str query
        +Vector query_embedding
        +List~Step~ steps
        +str rationale
        +str~bool~ success
        +int score
    }

    class Step {
        <<abstract>>
        +UUID id
        +str description
        +StepStatus status
        +int parallel_group
        +Any result
        +run(ExecutionContext) StepResult
    }
    note for Step "parallel_group: steps in the\nsame plan sharing this group ID\nrun concurrently. Null = sequential.\nIntra-plan parallelism, distinct from\nsub-agent (inter-task) delegation."

    class FunctionalStep {
        +Tool tool
        +dict inputs
    }
    class ReasoningStep {
        +str model
        +str instruction
        +List~str~ input_step_ids
    }
    class EvaluationStep {
        +str model
        +str criteria
        +bool blocking
    }
    Step <|-- FunctionalStep
    Step <|-- ReasoningStep
    Step <|-- EvaluationStep

    class PlanPreEvaluator {
        +str model
        +evaluate(Plan, List~Plan~ past_plans) PreEvalVerdict
    }
    note for PlanPreEvaluator "v2 — three checks:\n• achieves objective?\n• can be simplified?\n• improvement over past plans?"

    class PreEvalVerdict {
        +bool pass
        +str reason
        +List~str~ suggested_changes
    }

    class ToolCreatorAgent {
        +propose_tool(gap_description) Tool
        +evaluate(Tool) bool
    }
    note for ToolCreatorAgent "v2 — also creates new databases.\nHas a built-in evaluator."

    PlanningAgent --> Plan
    PlanningAgent --> ProceduralMemory : "1. check first (recipe-box lookup)"
    PlanningAgent --> ToolRegistry : "get_tools_for_task"
    PlanningAgent --> ToolCreatorAgent : if gap
    PlanningAgent --> PlanPreEvaluator : v2
    PreEvalVerdict --> PlanningAgent : on fail → replan
    Plan o-- Step
```

---

## View 3 — Execution, Tasks, and Sub-agents

The Executor runs the Plan as an `ExecutionPlan` (the diagram's renamed "Execution Plan"). Long-running work is promoted to a `Task` with status, budget, and sub-task delegation. The Post-Evaluator scores the plan, persists it to procedural memory, and — per the new "Creates skills" edge in the diagram — emits a generalized `Skill` when the result is reusable.

```mermaid
classDiagram
    direction TB

    class QuickAgent {
        +str model = "haiku-4-5"
        +answer(InboundMessage, ToolRegistry) Response
    }

    class Executor {
        +execute(Plan, ExecutionContext) ExecutionPlan
        +run_step(Step) StepResult
        -handle_eval_failure(EvaluationStep)
    }

    class ExecutionPlan {
        +Plan plan
        +Dict~UUID, StepResult~ results
        +PlanStatus status
        +int retries
        +datetime started_at
        +datetime ended_at
        +to_event_stream() SSE
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
        +UUID user_id
        +UUID parent_task_id
        +str title
        +TaskStatus status
        +UUID current_plan_id
        +int budget_cents
        +int spent_cents
        +str blocking_reason
        +str schedule_pattern
        +start()
        +pause()
        +resume()
        +complete(artifacts)
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

    class TaskEvent {
        +UUID task_id
        +str event_type
        +dict content
        +datetime created_at
    }

    class Artifact {
        +UUID task_id
        +str filename
        +str mime_type
        +str storage_url
        +int size_bytes
    }

    class PostEvaluator {
        +str model = "haiku-4-5"
        +evaluate(ExecutionPlan) PostEvalResult
        +persist(Plan, score, error) None
        +maybe_emit_skill(Plan) Skill
    }
    note for PostEvaluator "Diagram edge: 'Creates skills'.\nWrites plan → ProceduralMemory.\nIf score high & reusable, emits Skill."

    class PostEvalResult {
        +bool achieved_objective
        +int score
        +str error_diagnosis
        +bool emit_skill
    }

    Executor --> ExecutionPlan
    Executor --> ExecutionContext
    ExecutionContext --> Sandbox
    Task "1" o-- "*" Task : parent/child
    Task "1" o-- "*" Plan : has many over life
    Task "1" o-- "*" TaskEvent
    Task "1" o-- "*" Artifact
    ExecutionPlan --> PostEvaluator
    PostEvaluator --> ProceduralMemory
    PostEvaluator --> SkillsMemory : v2
```

---

## View 4 — Memory, Tools, Sandbox

Three memory subsystems (conversational, procedural, skills), the tool registry with concrete tools, and the sandbox abstraction backing `run_python` and the artifact tools.

```mermaid
classDiagram
    direction LR

    class ConversationalMemory {
        +UUID thread_id
        +fetch_recent(n) List~Message~
        +fetch_summaries() List~ThreadSummary~
        +search_relevant(query_embedding, k) List~Message~
        +append(Message)
    }
    note for ConversationalMemory "Three tiers, per-thread in v1:\nverbatim recent window,\ntiered summaries (L1, L2),\nvector recall over all msgs.\nVector lookup runs at Planner only."

    class ThreadSummary {
        +UUID id
        +UUID thread_id
        +int level
        +str summary_md
        +UUID range_start_message_id
        +UUID range_end_message_id
    }
    ConversationalMemory o-- ThreadSummary

    class ProceduralMemory {
        +search_similar(query_embedding, k) List~Plan~
        +store(Plan)
        +score_history(query) ScoreSummary
    }
    note for ProceduralMemory "Recipe-box (per diagram):\nrows have description,\ningredients, steps, score."

    class SkillsMemory {
        +store(Skill)
        +search_by_task(query_embedding, k) List~Skill~
    }
    note for SkillsMemory "v1: retrieval + seeded starter set\n(user_id = NULL rows).\nv2: auto-emission by PostEvaluator."

    class Skill {
        +UUID id
        +UUID user_id
        +str name
        +str description
        +dict ingredients
        +List~Step~ steps
        +UUID source_plan_id
        +int score
    }

    class ToolRegistry {
        +register(Tool)
        +get(name) Tool
        +get_tools_for_task(query) List~Tool~
        +describe_all() str
    }

    class Tool {
        <<abstract>>
        +str name
        +str description
        +dict signature
        +Vector embedding
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
    class InstallPackageTool
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
    Tool <|-- InstallPackageTool
    Tool <|-- CreateSpreadsheetTool
    Tool <|-- CreatePdfTool
    Tool <|-- CreateChartTool
    Tool <|-- CreateSlidesTool
    Tool <|-- AskUserTool
    note for AskUserTool "HITL: pauses task to awaiting_user,\ndispatches question via originating\nchannel, resumes on user reply."

    class Sandbox {
        <<abstract>>
        +UUID task_id
        +SandboxStatus status
        +start()
        +stop()
        +run_python(code, timeout) ExecResult
        +install_package(name)
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

    class Storage {
        <<abstract>>
        +put(user_id, path, bytes) str
        +get(user_id, path) bytes
    }
    class S3Storage
    class LocalStorage
    class DriveStorage
    class DropboxStorage
    Storage <|-- S3Storage
    Storage <|-- LocalStorage
    Storage <|-- DriveStorage : v2
    Storage <|-- DropboxStorage : v2

    ToolRegistry o-- Tool
    SkillsMemory o-- Skill
```

---

## View 5 — Model client, metering, observability, identity

Every model call funnels through a single `ModelClient` wrapper that enforces cap, records usage, writes a trace run to `model_call_logs`, and stamps the active prompt version on the row. Identity & billing classes are included for completeness — they live below the agent layer but every agent class transits through them.

```mermaid
classDiagram
    direction TB

    class ModelClient {
        +call(user_id, agent, model, messages, prompt_version_id) ModelResponse
        -anthropic: AnthropicClient
    }

    class Enforcer {
        +check_can_spend(user_id) None
        -fetch_period_usage(user_id) UsageSummary
    }
    class Recorder {
        +write(user_id, agent, model, prompt_version_id, usage, cost_cents, trace_id)
    }
    class Pricing {
        +compute(model, usage, at_time) int
        -model_prices: List~ModelPrice~
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
        +dict content_template
    }
    class PostgresTraceSink {
        +bool enabled
        +trace(user_id, agent, model, messages, system, ...) ContextManager~ModelRun~
    }
    class ModelRun {
        +UUID run_id
        +UUID parent_run_id
        +str status
        +mark_response(raw, text)
        +mark_cost(usage, cost_cents, token_usage_id)
        +mark_error(exc)
    }
    class CostNotifier {
        +maybe_send(user_id, cost_cents)
    }

    ModelClient --> Enforcer : pre-check
    ModelClient --> PostgresTraceSink : wrap
    PostgresTraceSink ..> ModelRun : yields
    ModelClient --> Recorder : post-record
    ModelClient --> Pricing : compute cost
    ModelClient --> PromptVersionStore : resolve version
    ModelClient --> CostNotifier : threshold check
    PromptVersionStore o-- PromptVersion

    class User {
        +UUID id
        +str email
        +bool email_verified
        +str display_name
    }
    class AuthMethod {
        +str method
        +str identifier
    }
    class ApiKey {
        +str prefix
        +str hash
        +str name
    }
    class Subscription {
        +str stripe_subscription_id
        +str tier
        +int allowance_cents
        +bool overage_authorized
        +int overage_cap_cents
    }
    class TierLimits {
        +str tier
        +int allowance_cents
        +List~str~ channels_allowed
        +bool opus_allowed
        +bool scheduled_tasks_allowed
    }
    User "1" --> "*" AuthMethod
    User "1" --> "*" ApiKey
    User "1" --> "1" Subscription
    Subscription --> TierLimits

    class TokenUsage {
        +UUID user_id
        +UUID task_id
        +UUID trace_id
        +UUID prompt_version_id
        +str agent
        +str model
        +int input_tokens
        +int output_tokens
        +int cost_cents
    }
    class ComputeUsage {
        +UUID user_id
        +UUID task_id
        +UUID sandbox_id
        +int compute_seconds
        +int cost_cents
    }
    Recorder --> TokenUsage : writes
    Sandbox --> ComputeUsage : writes on teardown

    class SleepCycle {
        +run() None
        +reorganize_procedural(user_id)
        +rescore_plans(user_id)
        +consolidate_skills(user_id)
    }
    note for SleepCycle "v2 — cron job that\norganizes memories."
```

---

## How the views connect

The five views are facets of one runtime; the same `ExecutionContext` flows through them.

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

---

## Class-to-diagram-box crosswalk

Quick reference: every block in `WolfPaw_00.pdf` and its corresponding class(es).

| Diagram box | Class(es) | v1/v2 |
|---|---|---|
| User | `User` | v1 |
| User File *(new)* | `UserProfile` | v1 |
| Soul File | `Soul` | v1 |
| Triage Agent | `TriageAgent`, `TriageResult` | v1 |
| Quick Agent | `QuickAgent` | v1 |
| Planning Agent | `PlanningAgent`, `Plan`, `Step` (+ subtypes) | v1 |
| Plan Pre-Evaluator | `PlanPreEvaluator`, `PreEvalVerdict` | v2 |
| Agent Loop / Execution Plan *(renamed)* | `Executor`, `ExecutionPlan`, `ExecutionContext` | v1 |
| Tool Creator Agent | `ToolCreatorAgent` | v2 |
| Toolbox | `ToolRegistry`, `Tool` + subclasses (incl. `AskUserTool` for HITL) | v1 |
| Skills | `SkillsMemory`, `Skill` | v1 retrieval + seeded set; v2 auto-emission |
| Procedural memory *(recipe-box)* | `ProceduralMemory` | v1 |
| Conversational Memory | `ConversationalMemory`, `ThreadSummary` (tiered: verbatim / L1 / L2 + vector recall) | v1 |
| Plan Post-Evaluator / Error Handler | `PostEvaluator`, `PostEvalResult` | v1 |
| Sleep Cycle | `SleepCycle` | v2 |
| Response | rendered via `Channel.send()` | v1 |
| (implicit) Sandbox | `Sandbox`, `E2BSandbox`, `DockerSandbox`, `CredentialProxy` | v1 |
| (implicit) Model calls | `ModelClient`, `Enforcer`, `Recorder`, `Pricing`, `PromptVersionStore`, `PostgresTraceSink` | v1 |
| (implicit) Tasks | `Task`, `TaskStatus`, `TaskEvent`, `Artifact` | v1 |
