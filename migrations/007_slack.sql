-- Wolfpaw v1 — step 21 (Slack channel).
--
-- Slack differs from Telegram in two ways:
--   1. Each workspace installs the app separately and gets its own bot
--      token (`xoxb-...`). We need a per-workspace record to know which
--      token to use when sending a reply.
--   2. A Slack user identity is workspace-scoped: the same person in two
--      different Workspaces is two different (team_id, user_id) pairs.
--      We encode that in `channel_links.external_id` as `"<team_id>:<user_id>"`.
--      The (channel, external_id) UNIQUE constraint already handles the
--      "one Slack identity → one Wolfpaw user" invariant.
--
-- `channel_link_tokens` (006_telegram.sql) is reused: the OAuth `state`
-- parameter is one of these tokens. When the user clicks "Connect Slack"
-- in the web UI, we mint a token bound to their Wolfpaw user_id, embed
-- it as `state=<token>` in the Slack OAuth URL, and consume it in the
-- callback to know who installed the app.

CREATE TABLE IF NOT EXISTS slack_workspaces (
    team_id              TEXT PRIMARY KEY,                -- Slack team / workspace id
    team_name            TEXT,
    bot_user_id          TEXT NOT NULL,                   -- the bot's Slack user id (for self-mention filtering)
    bot_token            TEXT NOT NULL,                   -- xoxb-... ; rotate by re-installing
    installed_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    installed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS slack_workspaces_installer_idx
    ON slack_workspaces (installed_by_user_id);
