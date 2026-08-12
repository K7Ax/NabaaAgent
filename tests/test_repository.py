from pathlib import Path

from opportunity_sentinel.agents import DiscoveryAgent
from opportunity_sentinel.models import OpportunityType, StudentProfile
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.tools import InMemoryResearchTools, SourcePage


def test_profile_opportunity_deduplication_and_delivery(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    repository = Repository(tmp_path / "data.sqlite")
    profile = StudentProfile(
        telegram_id=123,
        major="Software Engineering",
        graduation_year=2027,
        preferred_types={OpportunityType.COOP},
    )
    repository.upsert_profile(profile)
    candidate = DiscoveryAgent(InMemoryResearchTools([verified_page])).extract(
        verified_page.__dict__
    )
    assert candidate is not None
    first_id = repository.save_opportunity(candidate, 0.95)
    second_id = repository.save_opportunity(candidate, 0.99)
    assert first_id == second_id
    assert repository.get_profile(123) == profile
    assert repository.list_matches(profile)[0][0] == first_id
    assert repository.was_delivered(123, first_id) is False
    repository.mark_delivered(123, first_id)
    assert repository.was_delivered(123, first_id) is True
    repository.save_for_student(123, first_id)
    assert repository.list_saved(123)[0][0] == first_id

