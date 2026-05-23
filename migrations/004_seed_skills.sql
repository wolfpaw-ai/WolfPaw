-- Wolfpaw v1 — step 12 (planner + memory retrieval).
--
-- The v1 seeded skill set is loaded by `memory/skills.seed_starter_skills`
-- rather than SQL because each row needs a `voyage-3` embedding computed
-- via the Voyage API (or the stub embedder in dev). SQL can't make that
-- call. Keeping this migration as a marker for the seeded-skills step
-- so apply-all migration runs stay correlated with the build-order
-- step numbers.
--
-- The Python seeder is idempotent on the seeded `name` set (it inserts
-- only rows that aren't already present). Call it once at app boot, or
-- via a one-off task.

-- Belt-and-braces: ensure the partial index used by the seeded set
-- exists. The base index from 001_init.sql covers all rows including
-- seeded ones, so this is a no-op upgrade path if we ever decide to
-- split user-owned vs seeded indexes.
DO $$ BEGIN
    -- No schema change needed today. This file is a build-order marker.
    PERFORM 1;
END $$;
