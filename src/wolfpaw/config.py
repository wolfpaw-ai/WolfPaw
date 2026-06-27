"""Application config. Read from env vars (and `.env` for local dev).

Step 1 carries only what the foundation needs (env, log level, model IDs,
service URLs, feature flags). Later steps add credentials, channel tokens,
provider keys.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="WOLFPAW_",
    )

    env: str = "dev"
    log_level: str = "INFO"

    # Per-agent model IDs (implementation_plan.md §11)
    model_triage: str = "claude-haiku-4-5"
    model_quick: str = "claude-haiku-4-5"
    model_planner: str = "claude-sonnet-4-6"
    model_planner_opus: str = "claude-opus-4-7"
    model_executor: str = "claude-sonnet-4-6"
    model_post_evaluator: str = "claude-haiku-4-5"

    langsmith_enabled: bool = False

    database_url: str = "postgresql://localhost/wolfpaw"
    redis_url: str = "redis://localhost:6379/0"

    # Background workers (step 23). When enabled, fire-and-forget work
    # (thread compaction, Tasks, Telegram + Slack inbound dispatch) goes
    # to a Redis-backed arq queue and is run by the worker process from
    # `wolfpaw.workers.arq_app`. When disabled (the dev default), the
    # same code paths fall back to `asyncio.create_task` inside the
    # caller's event loop — so a fresh checkout works without Redis.
    # The web channel always runs in-process (SSE streams require it).
    workers_enabled: bool = False
    workers_redis_max_connections: int = 20

    # Sleep Cycle (step 26). Periodic maintenance: re-score old plans
    # against current Post-Evaluator prompts, consolidate near-duplicate
    # emitted skills, garbage-collect threads that never accumulated any
    # messages. Opt-in (default off) because each operation costs tokens
    # (re-scoring) or makes user-visible changes (skill supersession).
    # The cron fires only when the worker is running AND this flag is
    # true, on Sunday 03:00 UTC (tune the cron schedule in arq_app.py).
    # Manual / one-shot runs go through `enqueue_sleep_cycle()`.
    sleep_cycle_enabled: bool = False
    # How many of the oldest scored plans to re-evaluate per run.
    # Bounded so a single cycle's Haiku spend is predictable.
    sleep_cycle_rescore_batch: int = 20
    # Cosine threshold for merging two user-emitted skills. Higher than
    # the emit-time dedup threshold (0.85) because post-hoc consolidation
    # is destructive (soft-delete via superseded_by_skill_id); we want
    # high confidence the two skills really cover the same task.
    sleep_cycle_dedup_threshold: float = 0.92
    # Threads with zero messages older than this many days get
    # garbage-collected. Empty threads come from `/reset` followed by
    # the user never returning, or a channel-link mishap.
    sleep_cycle_orphan_thread_age_days: int = 30

    # Auth
    secret_key: str = "dev-only-secret-CHANGE-ME-in-non-dev-envs"
    magic_link_ttl_minutes: int = 15
    session_ttl_days: int = 30
    session_cookie_name: str = "wp_session"
    email_backend: str = "console"            # "console" | "ses" (later)
    web_base_url: str = "http://localhost:3000"

    # Model client
    anthropic_api_key: str = ""

    # Storage / workspace (step 7)
    storage_backend: str = "local"            # "local" | "s3"
    local_storage_root: str = ".wolfpaw_workspace"
    s3_bucket: str = ""                       # set when storage_backend == "s3"
    s3_region: str = "us-east-1"
    workspace_signed_url_ttl_seconds: int = 300
    workspace_upload_max_bytes: int = 50 * 1024 * 1024  # 50 MB

    # Tools (step 7)
    tavily_api_key: str = ""
    http_get_timeout_seconds: float = 15.0
    http_get_max_bytes: int = 2 * 1024 * 1024  # 2 MB

    # Embeddings (step 12)
    embedding_backend: str = "voyage"     # "voyage" | "stub" (tests/dev)
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3"
    voyage_dimensions: int = 1024

    # Tiered conversational memory (step 22). Together: messages newer
    # than `recent_window_size` stay verbatim; older messages get folded
    # into L1 summaries of `compaction_window_size` each once a thread
    # crosses `compaction_trigger_threshold`; once `l2_fold_threshold`
    # un-folded L1s accumulate, the oldest fold into an L2. The Planner
    # additionally pulls `vector_recall_k` semantically-matching older
    # messages from `message_embeddings` on every plan request.
    recent_window_size: int = 20
    compaction_window_size: int = 20
    compaction_trigger_threshold: int = 40  # recent_window + compaction_window
    l2_fold_threshold: int = 10
    vector_recall_k: int = 5

    # Skills auto-emission (step 25). When the Post-Evaluator scores a
    # plan at or above `skill_emit_min_score` AND the plan looks
    # reusable (multi-step + uses tools), the Skill Distiller runs to
    # generalize it into a named skill. Dedup against existing skills
    # is by cosine similarity on the plan's query embedding — anything
    # with a hit above `skill_dedup_similarity_threshold` is dropped.
    skill_emit_min_score: int = 90
    skill_dedup_similarity_threshold: float = 0.85

    # Procedural-memory plan retrieval (step 14). The Planner only adapts
    # past plans that scored at or above this — failed attempts must not be
    # offered back as templates. (NULL-score in-flight plans still pass.)
    plan_retrieval_min_score: int = 50

    # Tool Creator (step 28). When the Planner emits a `tool_creator`
    # step the Executor invokes the Tool Creator agent, which drafts a
    # spec, asks the user to approve via `ask_user`, persists on
    # approval. The approved tool then dispatches via the sandbox
    # whenever the Planner names it on subsequent plans.
    # Default on — the spec calls out "high risk for a first pass" but
    # also says "gate behind a feature flag," and we've gated the
    # actual execution behind ``ask_user`` approval, so the flag is
    # mainly for operators who want to disable the capability entirely.
    tool_creator_enabled: bool = True
    # Cosine threshold for skipping a Tool Creator proposal that
    # duplicates an existing approved user-tool (same shape as the
    # Skill Distiller's dedup, similar threshold).
    tool_dedup_similarity_threshold: float = 0.85

    # OAuth integrations (Phase C). Each provider gets a client_id +
    # client_secret pair the operator registers with the provider and
    # pastes into `.env`. Blank values mean the integration is
    # disabled on this deployment — the install-url endpoint returns
    # 503 and the tools surface "not connected" errors.
    integration_state_ttl_minutes: int = 15

    # Dropbox (step 29). App-folder scope. Register an app at
    # https://www.dropbox.com/developers/apps with permission_type =
    # "App folder". Callback URL must match
    # ${WOLFPAW_WEB_BASE_URL}/integrations/dropbox/oauth/callback.
    dropbox_client_id: str = ""
    dropbox_client_secret: str = ""

    # Notion (step 30). Public OAuth integration. Create one at
    # https://www.notion.so/my-integrations. Callback URL must match
    # ${WOLFPAW_WEB_BASE_URL}/integrations/notion/oauth/callback.
    notion_client_id: str = ""
    notion_client_secret: str = ""

    # Microsoft Calendar (step 32). Register an "App registration" in
    # the Azure portal under Entra ID; configure a Web platform with
    # redirect ${WOLFPAW_WEB_BASE_URL}/integrations/microsoft/oauth/callback;
    # API permissions: Microsoft Graph delegated Calendars.ReadWrite +
    # offline_access + User.Read. `microsoft_tenant` is the OAuth
    # tenant id — "common" works for any user account; pin to a
    # specific tenant id when the deployment is single-tenant.
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = "common"

    # Persona (step 17). Soul file path; empty → fall back to the
    # repo-root `soul.md` discovered via `persona.soul.default_soul_path()`.
    soul_path: str = ""

    # Telegram (step 18). Hosted uses one shared `@WolfpawBot`; OSS users
    # provision their own bot via @BotFather and set these env vars.
    telegram_bot_token: str = ""
    telegram_bot_username: str = "WolfpawBot"     # for deep-link URLs
    telegram_webhook_secret: str = ""             # X-Telegram-Bot-Api-Secret-Token
    telegram_link_token_ttl_minutes: int = 15

    # Slack (step 21). One Slack app per deployment; each workspace
    # installs the app via the OAuth flow which stores a per-workspace
    # bot token in `slack_workspaces`. All three of these come from the
    # Slack app's "Basic Information" + "OAuth & Permissions" pages.
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""                # for HMAC verify on events + commands
    slack_install_token_ttl_minutes: int = 15     # OAuth state token TTL

    # Sandbox (step 8). Provider selection + per-execution limits.
    sandbox_backend: str = "subprocess"        # "subprocess" | "docker" | "e2b"
    sandbox_idle_timeout_seconds: int = 15 * 60
    sandbox_default_cpu_seconds: int = 60
    sandbox_max_cpu_seconds: int = 5 * 60
    sandbox_default_memory_mb: int = 1024
    sandbox_max_memory_mb: int = 4096
    sandbox_compute_per_second_micros: int = 100   # 0.01¢/s placeholder
    docker_image: str = "python:3.12-slim"
    e2b_api_key: str = ""
    e2b_template: str = "base"


@lru_cache
def get_settings() -> Settings:
    return Settings()
