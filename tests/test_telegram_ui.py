from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opportunity_sentinel.agents import DiscoveryAgent
from opportunity_sentinel.config import Settings
from opportunity_sentinel.models import OpportunityCandidate, OpportunityType, StudentProfile
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.telegram_bot import (
    MAJORS,
    BotRuntime,
    _deliver_graph_result,
    _initial_state,
    create_runtime,
    main_menu,
    major_keyboard,
    opportunity_text,
    type_keyboard,
    user_id_from_thread,
    year_keyboard,
)
from opportunity_sentinel.tools import InMemoryResearchTools, SourcePage


def test_all_student_navigation_is_button_driven() -> None:
    assert all(button.callback_data for row in main_menu().inline_keyboard for button in row)
    assert len(major_keyboard().inline_keyboard) == len(MAJORS)
    assert all(
        button.callback_data
        for row in type_keyboard("onboard").inline_keyboard
        for button in row
    )
    assert all(button.callback_data for row in year_keyboard().inline_keyboard for button in row)


def test_workflow_thread_is_bound_to_authenticated_telegram_user() -> None:
    assert user_id_from_thread("opp-12345-a1b2c3") == 12345
    assert user_id_from_thread("alert-67890-a1b2c3") == 67890
    assert user_id_from_thread("unknown-12345-a1b2c3") is None
    assert user_id_from_thread("opp-not-a-number-a1b2c3") is None


def test_search_state_and_opportunity_card_use_student_profile(
    verified_page: SourcePage,
) -> None:
    profile = StudentProfile(
        telegram_id=42,
        major="هندسة البرمجيات",
        graduation_year=2027,
        preferred_types={OpportunityType.COURSE},
    )
    state = _initial_state("opp-42-test", profile)
    candidate = DiscoveryAgent(InMemoryResearchTools([verified_page])).extract(
        verified_page.__dict__
    )

    assert "دورات مجانية" in state["search_query"]
    assert "هندسة البرمجيات" in state["search_query"]
    assert state["student_profile"]["telegram_id"] == 42
    assert candidate is not None
    assert "تم التحقق" in opportunity_text(candidate)
    assert str(candidate.source_url) in opportunity_text(candidate)


@pytest.mark.asyncio
async def test_batch_delivery_deduplicates_and_persists_results(
    tmp_path: Path,
    verified_page: SourcePage,
) -> None:
    first = DiscoveryAgent(InMemoryResearchTools([verified_page])).extract(
        verified_page.__dict__
    )
    assert first is not None
    second_data = first.model_dump(mode="json")
    second_data.update(
        {
            "title": "Second verified opportunity",
            "source_url": "https://second.official.example/coop",
            "application_url": "https://second.official.example/apply",
        }
    )
    second = OpportunityCandidate.model_validate(second_data)
    runtime = BotRuntime(
        Settings(), Repository(tmp_path / "telegram.sqlite"), graph=object()
    )
    message = SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=77))
    collected = [
        {"candidate": first.model_dump(mode="json"), "verification": {"score": 1.0}},
        {"candidate": first.model_dump(mode="json"), "verification": {"score": 1.0}},
        {"candidate": second.model_dump(mode="json"), "verification": {"score": 0.9}},
    ]

    await _deliver_graph_result(
        message,
        runtime,
        {"final_status": "verified", "verified_candidates": collected},
    )

    assert message.answer.await_count == 3
    summary = message.answer.await_args_list[-1].args[0]
    assert "تحققت من 2 فرص" in summary
    opportunity_count = runtime.repository.connection.execute(
        "SELECT COUNT(*) FROM opportunities"
    ).fetchone()[0]
    delivery_count = runtime.repository.connection.execute(
        "SELECT COUNT(*) FROM deliveries WHERE telegram_id = 77"
    ).fetchone()[0]
    assert opportunity_count == 2
    assert delivery_count == 2


@pytest.mark.asyncio
async def test_delivery_fails_closed_when_graph_has_no_verified_result(
    tmp_path: Path,
) -> None:
    runtime = BotRuntime(
        Settings(), Repository(tmp_path / "empty.sqlite"), graph=object()
    )
    message = SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=88))

    await _deliver_graph_result(
        message,
        runtime,
        {"final_status": "rejected", "verified_candidates": []},
    )

    assert message.answer.await_count == 1
    assert "لم أعثر" in message.answer.await_args.args[0]


def test_runtime_builds_production_graph_and_persistence_without_live_keys(
    tmp_path: Path,
) -> None:
    settings = Settings(
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
        data_db_path=tmp_path / "runtime.sqlite",
        telegram_bot_token=None,
        groq_api_key=None,
        openrouter_api_key=None,
        tavily_api_key=None,
    )

    runtime = create_runtime(settings)

    assert runtime.settings is settings
    assert runtime.graph is not None
    assert runtime.repository.get_profile(999999) is None
    assert (tmp_path / "runtime.sqlite").exists()
