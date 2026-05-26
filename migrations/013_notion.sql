-- Wolfpaw v2 — step 30 (Notion integration).
--
-- Notion OAuth tokens don't expire and don't carry refresh tokens —
-- the schema is simpler than Dropbox's. The integration claims a
-- workspace-scoped bot identity on first install; ``workspace_id`` +
-- ``workspace_name`` are surfaced in the prompt so the agent knows
-- which workspace it's operating against (a user can connect Notion
-- once per workspace they're a member of, but we only persist the
-- most recent install per Wolfpaw user — re-installing replaces the
-- prior row).

CREATE TABLE IF NOT EXISTS notion_links (
    user_id          UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token     TEXT NOT NULL,
    workspace_id     TEXT,
    workspace_name   TEXT,
    workspace_icon   TEXT,
    bot_id           TEXT,
    owner            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
