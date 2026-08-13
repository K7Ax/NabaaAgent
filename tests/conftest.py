from datetime import date, timedelta

import pytest

from opportunity_sentinel.tools import SourcePage


@pytest.fixture
def future_deadline() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


@pytest.fixture
def verified_page(future_deadline: str) -> SourcePage:
    return SourcePage(
        url="https://official.example/coop",
        title="Software Engineering CO-OP Program",
        official=True,
        content=(
            "organization: Example Technology Company\n"
            "type: coop\n"
            "city: Riyadh\n"
            "mode: in_person\n"
            "majors: Software Engineering, Computer Science\n"
            f"deadline: {future_deadline}\n"
            "apply: https://official.example/apply"
        ),
    )
