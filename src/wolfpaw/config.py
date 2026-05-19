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

    # Auth
    secret_key: str = "dev-only-secret-CHANGE-ME-in-non-dev-envs"
    magic_link_ttl_minutes: int = 15
    session_ttl_days: int = 30
    session_cookie_name: str = "wp_session"
    email_backend: str = "console"            # "console" | "ses" (later)
    web_base_url: str = "http://localhost:3000"

    # Model client
    anthropic_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
