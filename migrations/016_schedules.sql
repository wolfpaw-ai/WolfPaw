-- Schedules — link a recurring/one-shot trigger to a re-runnable instruction.
--
-- A `schedules` row is a *template*: it stores the "what" (a self-contained
-- instruction for one run + resolved context + mutable cross-run state) and
-- the "when" (once / interval / cron, in the user's timezone). A per-minute
-- dispatcher (workers/jobs/dispatch_schedules.py) finds rows whose
-- `next_run_at` is due and spawns a normal `tasks` row from the instruction,
-- so every firing flows through the existing plan -> execute -> notify
-- pipeline and accrues history/cost in tasks/task_events for free.
--
-- The conditional half of a request ("...message me IF rain") is plain text
-- inside `instruction`; the agent evaluates it each run and calls
-- `send_telegram_message` (or not). Scheduled runs are silent unless the
-- instruction tells the agent to reach out.

CREATE TYPE schedule_status AS ENUM ('active', 'paused', 'done', 'cancelled');

CREATE TABLE IF NOT EXISTS schedules (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- the "what"
    instruction      TEXT  NOT NULL,
    context          JSONB NOT NULL DEFAULT '{}'::jsonb,   -- resolved-once params (location, ...)
    state            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- mutable scratch across runs (dedup)

    -- the "when"
    recurrence       TEXT  NOT NULL,                       -- 'once' | 'interval' | 'cron'
    cron_expr        TEXT,                                 -- when recurrence='cron'
    interval_seconds INTEGER,                              -- when recurrence='interval'
    timezone         TEXT  NOT NULL DEFAULT 'UTC',

    -- scheduling bookkeeping
    next_run_at      TIMESTAMPTZ,                          -- dispatcher watches this; NULL when terminal
    last_run_at      TIMESTAMPTZ,
    run_count        INTEGER NOT NULL DEFAULT 0,
    max_runs         INTEGER,                              -- optional cap (cost guard)
    until            TIMESTAMPTZ,                          -- optional end date

    -- delivery / management
    channel          channel,                             -- where messages go (telegram, ...)
    status           schedule_status NOT NULL DEFAULT 'active',
    title            TEXT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT schedules_recurrence_chk CHECK (recurrence IN ('once', 'interval', 'cron'))
);

-- The dispatcher's hot path: "which active schedules are due now?"
-- Partial index keeps it tight — terminal rows never match.
CREATE INDEX IF NOT EXISTS schedules_due_idx
    ON schedules (next_run_at)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS schedules_user_idx ON schedules (user_id);

-- Trace each spawned run back to the schedule that produced it.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS schedule_id UUID REFERENCES schedules(id) ON DELETE SET NULL;
