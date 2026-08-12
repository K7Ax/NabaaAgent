from datetime import date, timedelta

from opportunity_sentinel.agents import DiscoveryAgent, VerificationAgent
from opportunity_sentinel.models import VerificationStatus
from opportunity_sentinel.tools import InMemoryResearchTools, SourcePage


def test_verified_official_opportunity(verified_page: SourcePage) -> None:
    agent = DiscoveryAgent(InMemoryResearchTools([verified_page]))
    candidate = agent.extract(verified_page.__dict__)
    assert candidate is not None

    report = VerificationAgent().verify(candidate)
    assert report.status == VerificationStatus.VERIFIED
    assert report.score >= 0.8


def test_expired_opportunity_is_rejected() -> None:
    expired = (date.today() - timedelta(days=1)).isoformat()
    page = SourcePage(
        url="https://official.example/expired",
        title="Expired Technical Internship",
        official=True,
        content=(
            "organization: Example Company\n"
            "type: internship\ncity: Riyadh\nmode: in_person\n"
            f"deadline: {expired}\napply: https://official.example/apply"
        ),
    )
    candidate = DiscoveryAgent(InMemoryResearchTools([page])).extract(page.__dict__)
    assert candidate is not None
    report = VerificationAgent().verify(candidate)
    assert report.status == VerificationStatus.REJECTED
    assert "application_deadline_expired" in report.reasons

