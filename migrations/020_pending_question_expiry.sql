-- Pending-question expiry — stop stale `ask_user` rows from hijacking chat.
--
-- `get_open_for_user` treats a user's next inbound message as the answer to
-- their oldest still-`pending` question. But a question is only really "open"
-- while a task is actively waiting on it. If the waiting process dies (worker
-- restart mid-wait, a crash before `ask_user`'s timeout handler runs), the row
-- is stranded in 'pending' forever — and then every future message the user
-- sends is silently swallowed as an answer to a question nobody is listening
-- for (the task never even gets routed). That's the "it just says 'Got it —
-- continuing' and no task is created" failure.
--
-- `expires_at` bounds the window: `ask_user` sets it to now + the wait timeout,
-- and the inbound-answer lookup ignores anything past its expiry. Abandoned
-- waits now self-heal instead of poisoning the channel.

ALTER TABLE pending_questions
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Backfill existing rows to a past instant so any currently-stranded 'pending'
-- question stops matching immediately (its 5-minute default window has long
-- since elapsed for anything already in the table).
UPDATE pending_questions
   SET expires_at = created_at + INTERVAL '5 minutes'
 WHERE expires_at IS NULL;

-- Refine the inbound-routing index to cover the expiry filter. Rows that are
-- answered/timed-out/expired never need to match, so keep it partial on
-- 'pending' and order by expiry for the "oldest still-open" lookup.
DROP INDEX IF EXISTS pending_questions_open_idx;
CREATE INDEX IF NOT EXISTS pending_questions_open_idx
    ON pending_questions (user_id, expires_at)
    WHERE status = 'pending';
