"""LangChain chat models for the agentic pipeline.

The batch collector in ``scripts/scheduled_job.py`` keeps using the dependency-free
:class:`~opportunity_sentinel.llm.ModelRouter`; it runs in spawned subprocesses where a
small import surface matters. The agentic pipeline instead uses LangChain chat models so
it can bind tools the model chooses to call and parse results with
``with_structured_output``.

Provider failover is preserved: Groq is primary, OpenRouter is the fallback, composed
with ``.with_fallbacks()`` so the fallback applies to plain calls, tool-bound calls, and
structured calls alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from opportunity_sentinel.config import Settings

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.runnables import Runnable

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/SDAIAAcademy",
    "X-Title": "Opportunity Sentinel",
}


class NoChatProviderError(RuntimeError):
    """Raised when neither Groq nor OpenRouter is configured."""


def _groq_model(settings: Settings) -> BaseChatModel | None:
    if not settings.groq_api_key:
        return None
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.agent_groq_model,
        api_key=settings.groq_api_key,
        temperature=0,
        timeout=settings.request_timeout_seconds,
        max_retries=0,  # retries belong to the workflow's RetryPolicy, not the client
    )


def _openrouter_model(settings: Settings) -> BaseChatModel | None:
    if not settings.openrouter_api_key:
        return None
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.agent_openrouter_model,
        api_key=settings.openrouter_api_key,
        base_url=OPENROUTER_BASE_URL,
        default_headers=OPENROUTER_HEADERS,
        temperature=0,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
    )


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Return Groq with an OpenRouter fallback, or whichever one is configured."""
    primary = _groq_model(settings)
    fallback = _openrouter_model(settings)
    if primary is None and fallback is None:
        raise NoChatProviderError(
            "Set GROQ_API_KEY or OPENROUTER_API_KEY to use the agentic pipeline"
        )
    if primary is None:
        return fallback  # type: ignore[return-value]
    if fallback is None:
        return primary
    return primary.with_fallbacks([fallback])  # type: ignore[return-value]


def build_structured(settings: Settings, schema: type[BaseModel]) -> Runnable:
    """Return a runnable that answers with a validated instance of ``schema``.

    Structured output is applied to each provider *before* they are chained, because a
    fallback has to produce the same parsed type as the primary.
    """
    primary = _groq_model(settings)
    fallback = _openrouter_model(settings)
    if primary is None and fallback is None:
        raise NoChatProviderError(
            "Set GROQ_API_KEY or OPENROUTER_API_KEY to use the agentic pipeline"
        )
    structured = [
        model.with_structured_output(schema, method="json_schema")
        for model in (primary, fallback)
        if model is not None
    ]
    first, *rest = structured
    return first.with_fallbacks(rest) if rest else first
