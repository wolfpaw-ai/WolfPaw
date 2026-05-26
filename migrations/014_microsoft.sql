-- Wolfpaw v2 — step 32 (Microsoft Calendar integration).
--
-- Microsoft Graph OAuth tokens expire ~1h; refresh tokens are valid
-- for ~90 days. Schema mirrors `dropbox_links` (refresh-required) but
-- adds the optional ``tenant_id`` that Microsoft Graph returns on
-- token exchange and uses to scope subsequent calls. ``scope`` carries
-- the granted-scopes string Microsoft returns so we can detect when
-- the user authorized a subset of what we asked for.

CREATE TABLE IF NOT EXISTS microsoft_links (
    user_id        UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    tenant_id      TEXT,
    account_id     TEXT,
    scope          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
