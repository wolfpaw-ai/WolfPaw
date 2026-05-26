-- Wolfpaw v2 — step 26 (Sleep Cycle).
--
-- Skill consolidation needs a way to retire near-duplicate skills
-- without destroying their row history (procedural memory still
-- references emitted skills via `source_plan_id` traversal, and a
-- human or future Sleep Cycle pass may want to inspect the merge).
--
-- Pattern: soft-delete via ``superseded_by_skill_id`` pointing at the
-- survivor of a merge pair. `search_by_task` filters out rows where
-- this is set, so the Planner only ever retrieves the survivor.
-- ``ON DELETE SET NULL`` on the FK means deleting the survivor
-- gracefully restores the superseded skill to active rather than
-- chain-deleting it.

ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS superseded_by_skill_id UUID
        REFERENCES skills(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS skills_superseded_idx
    ON skills (superseded_by_skill_id)
    WHERE superseded_by_skill_id IS NOT NULL;
