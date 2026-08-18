from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    checkpoint_db_path: Path = Path("opportunity_checkpoints.sqlite")
    data_db_path: Path = Path("opportunity_sentinel.db")
    database_url: str | None = None
    turso_auth_token: str | None = None
    max_research_attempts: int = Field(default=2, ge=1, le=5)
    min_verification_score: float = Field(default=0.80, ge=0, le=1)
    telegram_bot_token: str | None = None
    telegram_admin_chat_id: int | None = None
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None
    tavily_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-20b"
    openrouter_model: str = "openrouter/free"
    # The agentic pipeline needs models that reliably support native tool calling
    # and json_schema responses, so it pins its own models instead of reusing the
    # batch collector's. "openrouter/free" is a wildcard route and cannot make
    # either guarantee.
    agent_groq_model: str = "openai/gpt-oss-120b"
    agent_openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    memory_db_path: Path = Path("student_memory.sqlite")
    knowledge_dir: Path = Path("docs/knowledge")
    rag_top_k: int = Field(default=4, ge=1, le=20)
    search_max_results: int = Field(default=5, ge=1, le=20)
    request_timeout_seconds: float = Field(default=20, ge=3, le=60)
    notification_interval_minutes: int = Field(default=480, ge=15, le=1440)
    official_scan_interval_minutes: int = Field(default=30, ge=15, le=1440)
    revalidation_interval_hours: int = Field(default=24, ge=1, le=168)
    revalidation_batch_size: int = Field(default=30, ge=1, le=200)
    discovery_cycle_timeout_seconds: int = Field(default=150, ge=30, le=600)
    search_heartbeat_hours: int = Field(default=24, ge=1, le=168)
    public_base_url: str | None = None
    telegram_webhook_secret: str | None = None
    internal_api_secret: str | None = None
    tavily_monthly_credit_limit: int = Field(default=900, ge=0, le=1000)
    digest_hour_riyadh: int = Field(default=18, ge=0, le=23)
    delivery_batch_size: int = Field(default=10, ge=1, le=100)


@lru_cache
def get_settings() -> Settings:
    return Settings()
