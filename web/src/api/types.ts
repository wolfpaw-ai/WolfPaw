// Shapes mirroring the backend Pydantic models. Hand-maintained — when a
// backend route adds a field, update here too (or auto-gen from
// OpenAPI in a future cleanup).

export interface UserMe {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
}

export interface WorkspaceFile {
  id: string;
  filename: string;
  mime_type: string | null;
  size_bytes: number;
  version: number;
  source: string;
  sha256: string | null;
  created_at: string;
}

export interface UserProfile {
  user_id: string;
  version: number;
  persona_md: string;
  preferences: Record<string, unknown>;
  timezone: string;
  updated_at: string | null;
}

export type TaskStatus =
  | "pending"
  | "running"
  | "blocked"
  | "awaiting_user"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  id: string;
  title: string;
  description: string | null;
  status: TaskStatus;
  current_plan_id: string | null;
  spent_cents: number;
  budget_cents: number | null;
  blocking_reason: string | null;
  parent_task_id: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  last_active_at: string | null;
}

export type ScheduleStatus = "active" | "paused" | "done" | "cancelled";

export interface Schedule {
  id: string;
  title: string | null;
  instruction: string;
  recurrence: "once" | "interval" | "cron";
  cadence: string;
  cron_expr: string | null;
  interval_seconds: number | null;
  timezone: string;
  next_run_at: string | null;
  last_run_at: string | null;
  run_count: number;
  max_runs: number | null;
  channel: string | null;
  status: ScheduleStatus;
  created_at: string | null;
}

export interface TaskEvent {
  id: string;
  event_type: string;
  content: Record<string, unknown>;
  created_at: string;
}

export interface TaskDetail {
  task: Task;
  events: TaskEvent[];
}

export interface UsageModelRow {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  input_cost_cents: number;
  output_cost_cents: number;
  cache_cost_cents: number;
  total_cost_cents: number;
}

export interface UsageAgentRow {
  agent: string;
  cost_cents: number;
}

export interface UsagePeriod {
  label: string;
  start: string;
  end: string;
  models: UsageModelRow[];
  by_agent: UsageAgentRow[];
  compute_cost_cents: number;
  total_cost_cents: number;
}

export type UsageScope = "default" | "today" | "month" | "all";

export interface UsageReport {
  scope: UsageScope;
  generated_at: string;
  timezone: string;
  include_by_agent: boolean;
  periods: UsagePeriod[];
}

export interface TelegramLinkResponse {
  url: string;
  expires_in_minutes: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
}

export interface ChatHistoryPage {
  thread_id: string | null;
  messages: ChatMessage[];
  has_more: boolean;
}

// --- monitoring ---------------------------------------------------------

export type MonitorWindow = "1h" | "24h" | "7d" | "30d";

export interface MonitorAgentStat {
  agent: string;
  calls: number;
  errors: number;
  cost_cents: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
}

export interface MonitorErrorStat {
  error_type: string;
  error_message: string;
  agent: string;
  count: number;
  last_seen: string;
}

export interface MonitorSummary {
  window: MonitorWindow;
  since: string;
  calls: number;
  errors: number;
  error_rate: number;
  cost_cents: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  by_agent: MonitorAgentStat[];
  top_errors: MonitorErrorStat[];
}

export interface MonitorTrace {
  trace_id: string;
  started_at: string;
  ended_at: string;
  calls: number;
  errors: number;
  cost_cents: number;
  total_latency_ms: number;
  agents: string[];
  task_id: string | null;
}

/** One model call. Payload fields are only populated by the trace
 *  drill-down endpoint; the list views omit them to keep responses small. */
export interface MonitorCall {
  id: string;
  run_id: string;
  parent_run_id: string | null;
  trace_id: string | null;
  task_id: string | null;
  agent: string;
  model: string;
  status: "ok" | "error";
  attempt: number;
  created_at: string;
  latency_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  cost_cents: number;
  stop_reason: string | null;
  error_type: string | null;
  error_message: string | null;
  truncated: boolean;
  system_prompt?: string | null;
  request_messages?: unknown;
  request_params?: unknown;
  response_content?: unknown;
  response_text?: string | null;
}
