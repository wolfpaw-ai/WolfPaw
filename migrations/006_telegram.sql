-- Wolfpaw v1 — step 18 (Telegram channel).
--
-- Two tables:
--   - `channel_links` is the per-user mapping from Wolfpaw user_id to
--     a channel-side identity (Telegram user id today; Slack workspace
--     id later). Unique on (channel, external_id) so the same Telegram
--     account can't link to two Wolfpaw users.
--   - `channel_link_tokens` are short-lived single-use tokens minted by
--     the web app for onboarding via deep link. Same shape as
--     `magic_link_tokens` from 002_auth.sql — sha256 hash on disk, plain
--     token in the URL.

CREATE TABLE IF NOT EXISTS channel_links (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel           channel NOT NULL,
    external_id       TEXT NOT NULL,
    external_username TEXT,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (channel, external_id)
);
CREATE INDEX IF NOT EXISTS channel_links_user_idx
    ON channel_links (user_id, channel);


CREATE TABLE IF NOT EXISTS channel_link_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel     channel NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS channel_link_tokens_user_idx
    ON channel_link_tokens (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS channel_link_tokens_expiry_idx
    ON channel_link_tokens (expires_at) WHERE used_at IS NULL;
