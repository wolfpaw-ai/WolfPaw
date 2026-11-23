-- Voice channel — paired voice devices (a Raspberry Pi with a headset or
-- speaker) that talk to Wolfpaw over a WebSocket.
--
--   - `voice` joins the `channel` enum so threads, tasks, schedules and
--     channel_links can name it. (Postgres can't use a new enum value in the
--     same transaction that adds it; nothing in this file does.)
--   - `voice_devices` holds one row per paired device. Only the SHA-256 hash
--     of the long-lived device token is stored; the plaintext goes to the
--     device once, at pairing. A user may pair several devices (kitchen,
--     living room, a headset). Revoking sets `revoked_at` and drops the
--     device's channel_links row.
--
-- Pairing codes reuse `channel_link_tokens` with channel = 'voice'.

ALTER TYPE channel ADD VALUE IF NOT EXISTS 'voice';

CREATE TABLE IF NOT EXISTS voice_devices (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS voice_devices_user_idx
    ON voice_devices (user_id) WHERE revoked_at IS NULL;
