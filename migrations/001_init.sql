-- Wolfpaw v1 — initial schema.
--
-- Tables here correspond 1:1 with the "Postgres schema (v1)" section of
-- implementation_plan.md. Apply with `psql -f migrations/001_init.sql` or via
-- the wolfpaw.memory.db.apply_sql_file helper in tests/dev.

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector for embeddings

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE channel AS ENUM ('web', 'telegram', 'email', 'slack');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system', 'tool');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE auth_method AS ENUM ('magic_link', 'google_oauth', 'api_key');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE subscription_tier AS ENUM (
        'dev', 'self_host', 'starter', 'pro', 'enterprise'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE subscription_status AS ENUM (
        'active', 'past_due', 'canceled', 'trialing', 'incomplete'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE task_status AS ENUM (
        'pending', 'running', 'blocked', 'awaiting_user',
        'completed', 'failed', 'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE agent_kind AS ENUM (
        'triage', 'quick', 'planner', 'executor', 'post_evaluator',
        'pre_evaluator', 'tool_creator'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE file_source AS ENUM ('user_upload', 'agent_output');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE sandbox_status AS ENUM ('starting', 'running', 'stopped', 'errored');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL UNIQUE,
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id      UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL DEFAULT 1,
    persona_md   TEXT NOT NULL DEFAULT '',
    preferences  JSONB NOT NULL DEFAULT '{}'::jsonb,
    timezone     TEXT NOT NULL DEFAULT 'UTC',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_auth_methods (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    method       auth_method NOT NULL,
    identifier   TEXT NOT NULL,                -- email for magic_link, sub for google
    secret_hash  TEXT,                          -- nullable for oauth
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (method, identifier)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prefix       TEXT NOT NULL,                 -- first ~8 chars, shown in UI
    hash         TEXT NOT NULL,                 -- argon2/sha256 of full key
    name         TEXT NOT NULL DEFAULT '',
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (prefix, hash)
);

-- ---------------------------------------------------------------------------
-- Billing
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tier_limits (
    tier                       subscription_tier PRIMARY KEY,
    allowance_cents            INTEGER NOT NULL,
    channels_allowed           TEXT[] NOT NULL,
    opus_allowed               BOOLEAN NOT NULL DEFAULT FALSE,
    scheduled_tasks_allowed    BOOLEAN NOT NULL DEFAULT FALSE,
    storage_quota_bytes        BIGINT NOT NULL DEFAULT 1073741824,  -- 1 GiB
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id                 UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    stripe_customer_id      TEXT,
    stripe_subscription_id  TEXT,
    tier                    subscription_tier NOT NULL DEFAULT 'dev',
    status                  subscription_status NOT NULL DEFAULT 'active',
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    allowance_cents         INTEGER NOT NULL DEFAULT 0,
    overage_authorized      BOOLEAN NOT NULL DEFAULT FALSE,
    overage_cap_cents       INTEGER,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS model_prices (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id                        TEXT NOT NULL,
    input_per_mtok_cents            INTEGER NOT NULL,
    output_per_mtok_cents           INTEGER NOT NULL,
    cache_read_per_mtok_cents       INTEGER NOT NULL DEFAULT 0,
    cache_write_per_mtok_cents      INTEGER NOT NULL DEFAULT 0,
    effective_from                  TIMESTAMPTZ NOT NULL,
    effective_to                    TIMESTAMPTZ,
    UNIQUE (model_id, effective_from)
);
CREATE INDEX IF NOT EXISTS model_prices_lookup_idx
    ON model_prices (model_id, effective_from DESC);

-- ---------------------------------------------------------------------------
-- Memory: threads, messages, summaries, plans, skills, tools
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS threads (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel               channel NOT NULL,
    soul_version          TEXT,
    user_profile_version  INTEGER,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS threads_user_idx ON threads (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id   UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role        message_role NOT NULL,
    content     TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS messages_thread_idx
    ON messages (thread_id, created_at);

CREATE TABLE IF NOT EXISTS message_embeddings (
    message_id  UUID PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    embedding   vector(1024) NOT NULL
);
-- IVFFlat index — populate then `VACUUM ANALYZE` to make it useful.
-- Scoped per-thread via WHERE on the join to messages.thread_id.
CREATE INDEX IF NOT EXISTS message_embeddings_ivf
    ON message_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE TABLE IF NOT EXISTS thread_summaries (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id                 UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    level                     SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 2),
    summary_md                TEXT NOT NULL,
    range_start_message_id    UUID REFERENCES messages(id) ON DELETE SET NULL,
    range_end_message_id      UUID REFERENCES messages(id) ON DELETE SET NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS thread_summaries_lookup_idx
    ON thread_summaries (thread_id, level, created_at);

CREATE TABLE IF NOT EXISTS plans (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    thread_id         UUID REFERENCES threads(id) ON DELETE SET NULL,
    task_id           UUID,  -- FK added below after tasks table exists
    query             TEXT NOT NULL,
    query_embedding   vector(1024),
    steps             JSONB NOT NULL DEFAULT '[]'::jsonb,
    final_answer      TEXT,
    success           BOOLEAN,
    score             INTEGER,
    error             TEXT,
    trace_id          TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS plans_user_idx ON plans (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS plans_query_embedding_ivf
    ON plans USING ivfflat (query_embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE TABLE IF NOT EXISTS skills (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,  -- NULL = seeded
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,
    embedding       vector(1024),
    ingredients     JSONB NOT NULL DEFAULT '{}'::jsonb,
    steps           JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_plan_id  UUID REFERENCES plans(id) ON DELETE SET NULL,
    score           INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS skills_owner_idx ON skills (user_id NULLS FIRST);
CREATE INDEX IF NOT EXISTS skills_embedding_ivf
    ON skills USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);

CREATE TABLE IF NOT EXISTS tools (
    name         TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    signature    JSONB NOT NULL,
    embedding    vector(1024),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS tools_embedding_ivf
    ON tools USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 20);

-- ---------------------------------------------------------------------------
-- Tasks & workspace
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tasks (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_task_id           UUID REFERENCES tasks(id) ON DELETE SET NULL,
    title                    TEXT NOT NULL,
    description              TEXT,
    status                   task_status NOT NULL DEFAULT 'pending',
    current_plan_id          UUID REFERENCES plans(id) ON DELETE SET NULL,
    budget_cents             INTEGER,
    spent_cents              INTEGER NOT NULL DEFAULT 0,
    blocking_reason          TEXT,
    channel_for_completion   channel,
    schedule_pattern         TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at               TIMESTAMPTZ,
    completed_at             TIMESTAMPTZ,
    last_active_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tasks_user_status_idx ON tasks (user_id, status);
CREATE INDEX IF NOT EXISTS tasks_parent_idx ON tasks (parent_task_id);

-- Now that tasks exists, wire plans.task_id → tasks(id).
ALTER TABLE plans
    ADD CONSTRAINT plans_task_id_fkey
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS task_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    content     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS task_events_task_idx
    ON task_events (task_id, created_at);

CREATE TABLE IF NOT EXISTS workspace_files (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id        UUID REFERENCES tasks(id) ON DELETE SET NULL,
    source         file_source NOT NULL,
    filename       TEXT NOT NULL,
    mime_type      TEXT,
    storage_url    TEXT NOT NULL,
    size_bytes     BIGINT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    supersedes_id  UUID REFERENCES workspace_files(id) ON DELETE SET NULL,
    sha256         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, filename, version)
);
CREATE INDEX IF NOT EXISTS workspace_files_user_idx
    ON workspace_files (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sandboxes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL,
    external_id       TEXT,
    status            sandbox_status NOT NULL DEFAULT 'starting',
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminated_at     TIMESTAMPTZ,
    compute_seconds   INTEGER NOT NULL DEFAULT 0,
    cost_cents        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sandboxes_task_idx ON sandboxes (task_id);

-- ---------------------------------------------------------------------------
-- Metering & observability
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prompt_versions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent             agent_kind NOT NULL,
    version_label     TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    content_template  JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent, version_label)
);
CREATE INDEX IF NOT EXISTS prompt_versions_agent_idx
    ON prompt_versions (agent, created_at DESC);

-- TODO: partition by month (RANGE on created_at) once volume justifies it.
-- For v1 pre-launch we keep it flat with the lookup indexes below.
CREATE TABLE IF NOT EXISTS token_usage (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id             UUID REFERENCES tasks(id) ON DELETE SET NULL,
    trace_id            TEXT,
    request_id          TEXT,
    agent               agent_kind NOT NULL,
    model               TEXT NOT NULL,
    prompt_version_id   UUID REFERENCES prompt_versions(id) ON DELETE SET NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_cents          INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS token_usage_user_time_idx
    ON token_usage (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS token_usage_trace_idx ON token_usage (trace_id);

CREATE TABLE IF NOT EXISTS compute_usage (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id            UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    sandbox_id         UUID REFERENCES sandboxes(id) ON DELETE SET NULL,
    compute_seconds    INTEGER NOT NULL DEFAULT 0,
    memory_gb_seconds  INTEGER NOT NULL DEFAULT 0,
    cost_cents         INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS compute_usage_user_time_idx
    ON compute_usage (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS usage_summaries (
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period_start         TIMESTAMPTZ NOT NULL,
    period_end           TIMESTAMPTZ NOT NULL,
    total_cost_cents     INTEGER NOT NULL DEFAULT 0,
    by_agent             JSONB NOT NULL DEFAULT '{}'::jsonb,
    by_model             JSONB NOT NULL DEFAULT '{}'::jsonb,
    compute_cost_cents   INTEGER NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, period_start)
);

CREATE TABLE IF NOT EXISTS cost_notifications (
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period_start  TIMESTAMPTZ NOT NULL,
    threshold_pct INTEGER NOT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, period_start, threshold_pct)
);

-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------

-- Tier limits — v1 placeholders. Real numbers locked in step 24 (billing).
-- "dev" runs every authenticated user with metering on but no enforcement.
INSERT INTO tier_limits (tier, allowance_cents, channels_allowed, opus_allowed, scheduled_tasks_allowed)
VALUES
    ('dev',        10000000, ARRAY['web','telegram','email','slack'], TRUE,  TRUE),
    ('self_host',  10000000, ARRAY['web','telegram','email','slack'], TRUE,  TRUE),
    ('starter',     2000,    ARRAY['web','telegram'],                  FALSE, FALSE),
    ('pro',        10000,    ARRAY['web','telegram','email'],          TRUE,  TRUE),
    ('enterprise', 50000,    ARRAY['web','telegram','email','slack'], TRUE,  TRUE)
ON CONFLICT (tier) DO NOTHING;

-- Model prices — placeholder values reflecting Anthropic & Voyage retail at
-- v1 build time (denominated in cents per million tokens). Update via a new
-- row with effective_from = NOW() and set the prior row's effective_to.
-- TODO: replace with live numbers from each provider's pricing page on deploy.
INSERT INTO model_prices
    (model_id, input_per_mtok_cents, output_per_mtok_cents,
     cache_read_per_mtok_cents, cache_write_per_mtok_cents, effective_from)
VALUES
    ('claude-haiku-4-5',   100,  500,  10,  125, '2026-01-01'),
    ('claude-sonnet-4-6',  300, 1500,  30,  375, '2026-01-01'),
    ('claude-opus-4-7',   1500, 7500, 150, 1875, '2026-01-01'),
    ('voyage-3',             6,    0,   0,    0, '2026-01-01')
ON CONFLICT (model_id, effective_from) DO NOTHING;
