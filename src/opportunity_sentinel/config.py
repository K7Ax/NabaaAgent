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
    max_research_attempts: int = Field(default=2, ge=1, le=5)
    min_verification_score: float = Field(default=0.80, ge=0, le=1)
    telegram_bot_token: str | None = None
    telegram_admin_chat_id: int | None = None
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None
    tavily_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-20b"
    openrouter_model: str = "openrouter/free"
    search_max_results: int = Field(default=5, ge=1, le=20)
    request_timeout_seconds: float = Field(default=20, ge=3, le=60)
    notification_interval_minutes: int = Field(default=360, ge=15, le=1440)


@lru_cache
def get_settings() -> Settings:
    return Settings()
