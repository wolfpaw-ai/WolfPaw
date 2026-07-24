-- Model-call trace log — the payload half of observability.
--
-- `token_usage` (001_init.sql) already carries one row per *successful* model
-- call, but only the countable parts: tokens, cost, agent, model. It is small,
-- billing-relevant, and kept for a long time. It deliberately holds no prompt
-- or response text.
--
-- This table is the other half: one row per model call *attempt*, holding the
-- payloads needed to actually debug a failure — system prompt, messages in,
-- content out, stop reason, latency, and the exception when there was one.
-- Two properties follow from that split:
--
--   1. Failures land here. A call that raises before Anthropic returns never
--      produces a `token_usage` row, which is exactly why failures have been
--      invisible. Every attempt writes here, `status` says how it ended.
--   2. Payloads are fat and age badly. They live on their own retention clock
--      (days) while the metering rows keep their own (months), so a long cost
--      history doesn't drag gigabytes of prompt text along with it.
--
-- Partitioned monthly by `created_at` so retention is `DROP TABLE partition` —
-- instant, and it reclaims disk immediately. A bulk `DELETE` on a table this
-- wide would leave dead tuples for autovacuum to chew through and never return
-- the space to the filesystem. Partition maintenance (create next month, drop
-- expired) runs in `workers/jobs/prune_traces.py`.

CREATE TABLE IF NOT EXISTS model_call_logs (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Correlation. `run_id` identifies this call; `parent_run_id` nests
    -- sub-agent calls under the call that spawned them, which is what turns a
    -- flat list into a trace tree. `trace_id` groups everything triggered by
    -- one inbound request (from the request-scoped contextvar in tracing.py).
    run_id              UUID NOT NULL,
    parent_run_id       UUID,
    trace_id            TEXT,
    request_id          TEXT,

    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id             UUID,
    -- Join key back to the metering row. Not a FK: on a failed attempt there
    -- is no `token_usage` row to point at, and `token_usage` is unpartitioned
    -- with its own lifetime — a FK would couple the two retention clocks.
    token_usage_id      UUID,

    -- TEXT, not the `agent_kind` enum: this table also logs callers that are
    -- not first-class agents (the compactor, ad-hoc utility calls), and a log
    -- table should never reject a write because of an unmigrated enum value.
    agent               TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version_id   UUID,

    -- Request side.
    system_prompt       TEXT,
    request_messages    JSONB NOT NULL DEFAULT '[]'::jsonb,
    request_params      JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Response side. `response_content` is the raw block list (tool_use blocks
    -- included); `response_text` is the flattened text for cheap display.
    response_content    JSONB,
    response_text       TEXT,
    stop_reason         TEXT,

    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_cents          INTEGER NOT NULL DEFAULT 0,

    latency_ms          INTEGER,
    -- 1-based. >1 means a retry of the same logical call; retries share a
    -- `trace_id` so "how many attempts did this take" is a GROUP BY away.
    attempt             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'ok',   -- 'ok' | 'error'
    error_type          TEXT,
    error_message       TEXT,
    -- TRUE when payloads were clipped to the configured byte budget, so a
    -- reader never mistakes a truncated prompt for the real one.
    truncated           BOOLEAN NOT NULL DEFAULT FALSE,

    -- The partition key must be part of every unique constraint, hence the
    -- composite PK. `run_id` alone is unique in practice but can't be declared
    -- so across partitions.
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Indexes on the parent propagate to every partition, existing and future.
CREATE INDEX IF NOT EXISTS model_call_logs_user_time_idx
    ON model_call_logs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS model_call_logs_trace_idx
    ON model_call_logs (trace_id, created_at);
CREATE INDEX IF NOT EXISTS model_call_logs_task_idx
    ON model_call_logs (task_id, created_at);
CREATE INDEX IF NOT EXISTS model_call_logs_run_idx
    ON model_call_logs (run_id);
-- Partial: the failure feed is the common monitoring query and errors are a
-- small fraction of rows, so this index stays tiny.
CREATE INDEX IF NOT EXISTS model_call_logs_errors_idx
    ON model_call_logs (created_at DESC)
    WHERE status = 'error';

-- ---------------------------------------------------------------------------
-- Partition maintenance
-- ---------------------------------------------------------------------------

-- Idempotent: create the monthly partition covering `at`, if absent. Called
-- here for the initial window and monthly thereafter by the pruner job.
-- Writes outside any existing partition would otherwise error, so the job
-- provisions ahead rather than on demand.
CREATE OR REPLACE FUNCTION ensure_model_call_log_partition(at TIMESTAMPTZ)
RETURNS TEXT AS $$
DECLARE
    start_ts  TIMESTAMPTZ := date_trunc('month', at);
    end_ts    TIMESTAMPTZ := date_trunc('month', at) + INTERVAL '1 month';
    part_name TEXT := 'model_call_logs_' || to_char(start_ts, 'YYYY_MM');
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF model_call_logs'
        ' FOR VALUES FROM (%L) TO (%L)',
        part_name, start_ts, end_ts
    );
    RETURN part_name;
END;
$$ LANGUAGE plpgsql;

-- Seed last / current / next month so the table is writable the moment this
-- migration lands, even if the worker process isn't running yet.
SELECT ensure_model_call_log_partition(NOW() - INTERVAL '1 month');
SELECT ensure_model_call_log_partition(NOW());
SELECT ensure_model_call_log_partition(NOW() + INTERVAL '1 month');
