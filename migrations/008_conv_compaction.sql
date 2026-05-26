-- Wolfpaw v2 — step 22 (tiered conversational memory).
--
-- Two additions in support of the compaction worker:
--
--   1. `'compactor'` enum value on `agent_kind` so the summarization
--      model calls that fold older messages into L1 / L2 summaries can
--      attribute their `token_usage` rows to a sensible agent slot.
--
--   2. `folded_into_summary_id` column on `thread_summaries` so we can
--      tell which L1 rows have already been rolled up into an L2 (and
--      should therefore drop out of `fetch_summaries` in favor of their
--      L2 parent). The pointer is `ON DELETE SET NULL` so deleting an L2
--      gracefully demotes its children back to "unfolded".
--
-- `ALTER TYPE ... ADD VALUE` is a PG 12+ feature that's allowed inside
-- a transaction; the new value just can't be used in the same tx that
-- added it. We don't use it here, so it's safe.

ALTER TYPE agent_kind ADD VALUE IF NOT EXISTS 'compactor';

ALTER TABLE thread_summaries
    ADD COLUMN IF NOT EXISTS folded_into_summary_id UUID
        REFERENCES thread_summaries(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS thread_summaries_folded_idx
    ON thread_summaries (thread_id, folded_into_summary_id);
