-- Pending questions — the durable half of human-in-the-loop (`ask_user`).
--
-- When a task needs input mid-plan, `ask_user` writes a row here (source of
-- truth), delivers the question to the user's channel (web SSE emit, or a
-- proactive Telegram/Slack push), then waits for `status` to leave 'pending'.
-- The wait is a short DB poll, NOT an in-process asyncio.Future — so it works
-- identically whether the task runs inline in the web request or in a separate
-- arq worker process, and whichever process receives the answer (web /answer,
-- or the next inbound Telegram message) just flips the row to 'answered'.
--
-- This is what decouples "where the task runs" from "how the human answers":
-- both sides meet at this table instead of at a live stream + shared memory.

CREATE TYPE pending_question_status AS ENUM (
    'pending', 'answered', 'timeout', 'cancelled'
);

CREATE TABLE IF NOT EXISTS pending_questions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    thread_id    UUID,                                      -- for channel-side reply routing
    channel      TEXT,                                      -- originating channel ('web' | 'telegram' | ...)
    question     TEXT  NOT NULL,
    options      JSONB NOT NULL DEFAULT '[]'::jsonb,        -- optional multiple-choice
    urgency      TEXT  NOT NULL DEFAULT 'normal',
    status       pending_question_status NOT NULL DEFAULT 'pending',
    answer       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    answered_at  TIMESTAMPTZ
);

-- Hot path #1: `ask_user`'s poll — "is this specific question answered yet?"
-- (covered by the PK). Hot path #2: Telegram inbound routing — "does this
-- user have an open question to treat the next message as an answer to?"
-- Partial index keeps it tight; answered/timed-out rows never match.
CREATE INDEX IF NOT EXISTS pending_questions_open_idx
    ON pending_questions (user_id, created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS pending_questions_task_idx
    ON pending_questions (task_id);
