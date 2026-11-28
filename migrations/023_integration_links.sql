-- Generic per-user link table for integrations built on the integration
-- framework (wolfpaw.integrations.base). One row per (user, provider).
--
-- New providers store their credentials here instead of adding a
-- `<provider>_links` table + migration of their own. The original three
-- (Dropbox, Notion, Microsoft) keep their dedicated tables for now.
--
-- `credentials` is provider-defined JSON (for an API-key integration:
-- {"api_key": "..."}). Like the existing link tables it is stored
-- verbatim in v1; operators who need it encrypted at rest can encrypt the
-- column or the volume. Credentials never enter model context or logs.

CREATE TABLE IF NOT EXISTS integration_links (
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    auth_kind     TEXT NOT NULL CHECK (auth_kind IN ('api_key', 'oauth')),
    credentials   JSONB NOT NULL DEFAULT '{}'::jsonb,
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    display       JSONB NOT NULL DEFAULT '{}'::jsonb,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, provider)
);
