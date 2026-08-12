from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel

from opportunity_sentinel.config import Settings
from opportunity_sentinel.llm import ModelRouter, Provider
from opportunity_sentinel.tools import WebResearchTools

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "artifacts" / "live-smoke-evidence.json"


class SmokeAnswer(BaseModel):
    status: Literal["ok"]


def test_telegram(settings: Settings) -> dict:
    result = {"configured": bool(settings.telegram_bot_token), "connected": False}
    if not settings.telegram_bot_token:
        return result
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe",
            timeout=settings.request_timeout_seconds,
        )
        body = response.json()
        result["connected"] = response.is_success and body.get("ok") is True
        result["bot_username"] = body.get("result", {}).get("username")
    except (httpx.HTTPError, ValueError):
        result["error_type"] = "connection_failed"
    return result


def test_admin_chat(settings: Settings) -> dict:
    result = {"configured": bool(settings.telegram_admin_chat_id), "reachable": False}
    if not settings.telegram_bot_token or not settings.telegram_admin_chat_id:
        return result
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getChat",
            params={"chat_id": settings.telegram_admin_chat_id},
            timeout=settings.request_timeout_seconds,
        )
        result["reachable"] = response.is_success and response.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        result["error_type"] = "connection_failed"
    return result


def test_provider(provider: Provider, timeout: float) -> dict:
    result = {"configured": True, "connected": False, "model": provider.model}
    try:
        answer = ModelRouter([provider], timeout).generate_json(
            system='Return exactly {"status":"ok"} using the required JSON schema.',
            user="This is a minimal connectivity test. Do not add any other content.",
            schema=SmokeAnswer,
            task=f"live_smoke_{provider.name}",
        )
        result["connected"] = answer.status == "ok"
    except RuntimeError:
        result["error_type"] = "provider_request_failed"
    return result


def test_search(settings: Settings) -> dict:
    pages, observation = WebResearchTools(
        max_results=min(settings.search_max_results, 5),
        timeout=settings.request_timeout_seconds,
    ).search_web("فرص تدريب طلاب تقنية الرياض التقديم مفتوح site:gov.sa OR site:edu.sa")
    return {
        "connected": observation.success,
        "result_count": len(pages),
    }


def main() -> None:
    settings = Settings()
    providers: dict[str, dict] = {}
    if settings.groq_api_key:
        providers["groq"] = test_provider(
            Provider(
                "groq",
                "https://api.groq.com/openai/v1",
                settings.groq_api_key,
                settings.groq_model,
            ),
            settings.request_timeout_seconds,
        )
    if settings.openrouter_api_key:
        providers["openrouter"] = test_provider(
            Provider(
                "openrouter",
                "https://openrouter.ai/api/v1",
                settings.openrouter_api_key,
                settings.openrouter_model,
                {
                    "HTTP-Referer": "https://github.com/SDAIAAcademy",
                    "X-Title": "Opportunity Sentinel",
                },
                {
                    "provider": {"require_parameters": True},
                    "plugins": [{"id": "response-healing"}],
                },
            ),
            settings.request_timeout_seconds,
        )
    report = {
        "telegram": test_telegram(settings),
        "admin_chat": test_admin_chat(settings),
        "providers": providers,
        "live_search": test_search(settings),
    }
    EVIDENCE_PATH.parent.mkdir(exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
