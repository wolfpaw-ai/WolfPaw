-- Wolfpaw v1 — auth tables.
--
-- Ephemeral magic-link tokens live in their own table (separate from
-- `user_auth_methods`, which holds persistent identities like Google `sub`).
-- Tokens are short-lived (~15 min), single-use, and reference an email rather
-- than a user — the user row is created on first successful verify.

CREATE TABLE IF NOT EXISTS magic_link_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,    -- sha256(plain_token) hex
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS magic_link_tokens_email_idx
    ON magic_link_tokens (email, created_at DESC);
CREATE INDEX IF NOT EXISTS magic_link_tokens_expiry_idx
    ON magic_link_tokens (expires_at) WHERE used_at IS NULL;
