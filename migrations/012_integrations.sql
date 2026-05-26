-- Wolfpaw v2 — Phase C scaffolding (steps 29 / 30 / 32).
--
-- Two pieces of shared infrastructure used by every OAuth integration
-- (Dropbox, Notion, Microsoft Calendar today; Google + Gmail when
-- verification clears), plus the Dropbox-specific token table for
-- step 29.
--
--   * `integration_state_tokens` — single-use, TTL-bound state nonces
--     for OAuth authorization-code flows. Mirrors the shape of
--     `channel_link_tokens` but lives in its own table because
--     integrations aren't channels (no `channel` enum entry would fit).
--     The plaintext token goes into the OAuth `state` query parameter;
--     only its SHA-256 hash is stored. Verification is atomic (the
--     row is marked used inside the same transaction that reads it).
--
--   * `dropbox_links` — per-user Dropbox tokens. Step 29. Dropbox tokens
--     expire ~4h; the integration refreshes via `refresh_token` before
--     each API call when within a buffer of expiry. ``account_id`` is
--     the Dropbox-side identifier returned on first token exchange.
--
-- Notion + Microsoft Calendar each get their own migration (013, 014)
-- following the same shape but with provider-specific columns.

CREATE TABLE IF NOT EXISTS integration_state_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS integration_state_tokens_user_idx
    ON integration_state_tokens (user_id);

CREATE TABLE IF NOT EXISTS dropbox_links (
    user_id        UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    account_id     TEXT,
    scope          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
