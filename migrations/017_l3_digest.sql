-- Wolfpaw — memory improvement step 2 (single size-capped L3 digest).
--
-- Extends the summary ladder with a third level so the summary layer
-- stops growing linearly in the prompt. L1 (window of ~20 raw messages)
-- and L2 (fold of ~10 L1s) are unchanged. L3 is special: there is at
-- most ONE L3 row per thread and it is REWRITTEN in place as new L2s
-- fold into it, so it never accumulates — a fixed-budget rolling digest
-- of the distant past. It is lossy by design (older detail compresses
-- away), which is the accepted tradeoff for an O(1) prompt.
--
-- Only change here: allow level = 3. The compactor's `_drain_l3`
-- enforces the single-row + rewrite-in-place invariant in code;
-- `fetch_summaries` already returns any row whose `folded_into_summary_id`
-- is NULL, so the L3 (never folded, being the top) surfaces automatically
-- while L2s folded into it drop out.

ALTER TABLE thread_summaries
    DROP CONSTRAINT IF EXISTS thread_summaries_level_check;
ALTER TABLE thread_summaries
    ADD CONSTRAINT thread_summaries_level_check CHECK (level BETWEEN 1 AND 3);
