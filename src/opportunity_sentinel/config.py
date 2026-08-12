from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    checkpoint_db_path: Path = Path("opportunity_checkpoints.sqlite")
    max_research_attempts: int = Field(default=2, ge=1, le=5)
    min_verification_score: float = Field(default=0.80, ge=0, le=1)
    telegram_bot_token: str | None = None
    telegram_admin_chat_id: int | None = None
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()

