from contextlib import asynccontextmanager

from fastapi import FastAPI

from opportunity_sentinel.config import get_settings
from opportunity_sentinel.logging import configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(get_settings().log_level)
    yield


app = FastAPI(title="Opportunity Sentinel", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "opportunity-sentinel"}


@app.get("/readiness")
def readiness() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ready",
        "telegram_configured": bool(settings.telegram_bot_token),
        "llm_providers_configured": [
            name
            for name, configured in (
                ("groq", settings.groq_api_key),
                ("openrouter", settings.openrouter_api_key),
            )
            if configured
        ],
        "checkpoint_path": str(settings.checkpoint_db_path),
    }
