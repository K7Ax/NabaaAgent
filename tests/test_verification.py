from datetime import date, timedelta

from opportunity_sentinel.agents import DiscoveryAgent, VerificationAgent
from opportunity_sentinel.models import (
    DeliveryMode,
    Evidence,
    OpportunityCandidate,
    OpportunityType,
    VerificationStatus,
)
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


def test_explicit_open_registration_can_replace_unknown_deadline() -> None:
    source = "https://tuwaiq.edu.sa/bootcamp/current"
    candidate = OpportunityCandidate(
        title="معسكر تقني حالي",
        organization="أكاديمية طويق",
        opportunity_type=OpportunityType.COURSE,
        city="الرياض",
        delivery_mode=DeliveryMode.IN_PERSON,
        accepted_majors=["جميع التخصصات"],
        registration_open=True,
        application_url=source,
        source_url=source,
        evidence=[
            Evidence(
                field_name=field,
                value=value,
                quote=quote,
                source_url=source,
                official_source=True,
            )
            for field, value, quote in [
                ("organization", "أكاديمية طويق", "تنظم أكاديمية طويق المعسكر"),
                ("city", "الرياض", "حضوري في مقر الأكاديمية بمدينة الرياض"),
                ("accepted_majors", "جميع التخصصات", "البرنامج متاح لجميع التخصصات"),
                ("registration_status", "open", "حالة التسجيل: متاح"),
            ]
        ],
    )

    report = VerificationAgent().verify(candidate)

    assert report.status == VerificationStatus.VERIFIED


def test_first_party_structured_tuwaiq_data_is_not_downgraded_by_llm() -> None:
    source = "https://tuwaiq.edu.sa/bootcamp/current/view"
    candidate = OpportunityCandidate(
        title="برنامج تقني مفتوح",
        organization="أكاديمية طويق",
        opportunity_type=OpportunityType.COURSE,
        city="الرياض",
        delivery_mode=DeliveryMode.IN_PERSON,
        accepted_majors=["التخصصات التقنية"],
        deadline=date.today() + timedelta(days=5),
        registration_open=True,
        application_url=source,
        source_url=source,
        evidence=[
            Evidence(
                field_name=field,
                value=value,
                quote=f"official {field}: {value}",
                source_url=source,
                official_source=True,
            )
            for field, value in [
                ("organization", "أكاديمية طويق"),
                ("city", "الرياض"),
                ("deadline", str(date.today() + timedelta(days=5))),
                ("accepted_majors", "التخصصات التقنية"),
                ("registration_status", "open"),
            ]
        ],
    )

    class FailingLLM:
        def generate_json(self, **kwargs):
            raise AssertionError("LLM must not override trusted structured data")

    report = VerificationAgent(FailingLLM()).verify(candidate)

    assert report.status == VerificationStatus.VERIFIED
    assert "verified_against_first_party_structured_data" in report.reasons
