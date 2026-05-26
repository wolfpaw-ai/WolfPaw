-- Wolfpaw v2 — step 28 (Tool Creator).
--
-- Turns the previously-unused `tools` table into a real surface for
-- agent-proposed, human-approved user-tools. The Planner detects gaps
-- in its toolkit, emits a ``tool_creator`` step, the Tool Creator
-- drafts (name, description, input_schema, Python implementation),
-- asks the user for approval via ``ask_user``, and on approval the
-- row lands here with ``status='approved'`` + ``user_id`` set.
--
-- Schema changes (additive — existing builtin-tool rows are untouched):
--   * `id` UUID PK replaces the global `name` PK. Different users can
--     own tools with the same name; the (user_id, name) tuple is what
--     stays unique. Builtin tools (user_id IS NULL) get a partial
--     unique index keeping the global namespace clean.
--   * `user_id` — NULL for builtins, set for user-owned proposals.
--   * `implementation` — Python source code the sandbox executes when
--     the tool is invoked. NULL for builtins.
--   * `status` — 'proposed' | 'approved' | 'rejected'. NULL for
--     builtins. Only 'approved' rows are dispatchable.
--   * `source_plan_id` / `source_task_id` — provenance: which plan
--     and which task surfaced the gap that produced this proposal.
--   * `approved_at` — stamped on the approval transition.
--
-- The dispatch path in the Executor falls through to the DB lookup
-- only on Registry miss, so builtins always win when both exist.

ALTER TABLE tools
    DROP CONSTRAINT IF EXISTS tools_pkey;

ALTER TABLE tools
    ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid();

ALTER TABLE tools
    ADD CONSTRAINT tools_pkey PRIMARY KEY (id);

ALTER TABLE tools
    ADD COLUMN IF NOT EXISTS user_id UUID
        REFERENCES users(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS implementation TEXT,
    ADD COLUMN IF NOT EXISTS status TEXT
        CHECK (status IS NULL OR status IN ('proposed', 'approved', 'rejected')),
    ADD COLUMN IF NOT EXISTS source_plan_id UUID
        REFERENCES plans(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_task_id UUID
        REFERENCES tasks(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

-- Each user can only own one tool of a given name. NULL user_id rows
-- (builtin / global) get a separate partial index so the global
-- namespace stays unique too.
CREATE UNIQUE INDEX IF NOT EXISTS tools_user_name_idx
    ON tools (user_id, name)
    WHERE user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS tools_global_name_idx
    ON tools (name)
    WHERE user_id IS NULL;

-- Hot path: "does this user have an approved tool named X?"
CREATE INDEX IF NOT EXISTS tools_user_status_idx
    ON tools (user_id, status)
    WHERE status = 'approved';
