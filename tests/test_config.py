"""Settings that have to reach the process environment, not just the settings object."""

from __future__ import annotations

import pytest

from opportunity_sentinel.config import Settings, configure_tracing


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(name, raising=False)


def test_tracing_is_exported_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """LangSmith reads os.environ, so .env values have to be published there."""
    import os

    settings = Settings(
        langchain_tracing_v2=True,
        langchain_api_key="lsv2-test",
        langchain_project="nabaa-capstone",
    )

    assert configure_tracing(settings) is True
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "lsv2-test"
    assert os.environ["LANGCHAIN_PROJECT"] == "nabaa-capstone"


def test_tracing_stays_off_without_a_key() -> None:
    """A flag with no key would make every LLM call fail on an unauthorized export."""
    import os

    assert configure_tracing(Settings(langchain_tracing_v2=True)) is False
    assert "LANGCHAIN_TRACING_V2" not in os.environ


def test_tracing_stays_off_when_not_requested() -> None:
    assert configure_tracing(Settings(langchain_api_key="lsv2-test")) is False
