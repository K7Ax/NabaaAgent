import pytest

from opportunity_sentinel.chat_models import (
    NoChatProviderError,
    build_chat_model,
    build_structured,
)
from opportunity_sentinel.config import Settings
from opportunity_sentinel.supervisor import RouteDecision


def _settings(**overrides) -> Settings:
    base = {
        "groq_api_key": None,
        "openrouter_api_key": None,
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})


def test_missing_providers_raise_a_clear_error() -> None:
    with pytest.raises(NoChatProviderError):
        build_chat_model(_settings())
    with pytest.raises(NoChatProviderError):
        build_structured(_settings(), RouteDecision)


def test_groq_only_configuration_has_no_fallback_attached() -> None:
    model = build_chat_model(_settings(groq_api_key="test-key"))

    assert type(model).__name__ == "ChatGroq"


def test_openrouter_only_configuration_is_used_directly() -> None:
    model = build_chat_model(_settings(openrouter_api_key="test-key"))

    assert type(model).__name__ == "ChatOpenAI"
    assert "openrouter.ai" in str(model.openai_api_base)


def test_both_providers_compose_into_a_fallback_chain() -> None:
    model = build_chat_model(
        _settings(groq_api_key="test-key", openrouter_api_key="test-key")
    )

    assert type(model).__name__ == "RunnableWithFallbacks"
    assert type(model.runnable).__name__ == "ChatGroq"
    assert [type(each).__name__ for each in model.fallbacks] == ["ChatOpenAI"]


def test_structured_output_applies_to_the_fallback_too() -> None:
    """Both providers must return the same parsed type, so structure is applied first."""
    runnable = build_structured(
        _settings(groq_api_key="test-key", openrouter_api_key="test-key"),
        RouteDecision,
    )

    assert type(runnable).__name__ == "RunnableWithFallbacks"
    assert len(runnable.fallbacks) == 1


def test_the_agent_pins_tool_calling_models_not_the_batch_collector_models() -> None:
    settings = _settings(groq_api_key="test-key")

    assert settings.agent_groq_model != settings.groq_model
    assert settings.agent_openrouter_model != "openrouter/free"
