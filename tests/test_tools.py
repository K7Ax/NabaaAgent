import json
from datetime import date

import httpx
from bs4 import BeautifulSoup

import opportunity_sentinel.tools as tools_module
from opportunity_sentinel.agents import DiscoveryAgent, VerificationAgent
from opportunity_sentinel.models import OpportunityType, VerificationStatus
from opportunity_sentinel.tools import (
    InMemoryResearchTools,
    SourcePage,
    WebResearchTools,
    _is_public_address,
    _looks_official,
    _tuwaiq_channel_page,
    _tuwaiq_index_page,
)


def test_blank_structured_major_does_not_capture_next_line() -> None:
    page = SourcePage(
        url="https://tuwaiq.edu.sa/bootcamp/example/view",
        title="Official program without a stated major",
        official=True,
        content=(
            "OPPORTUNITY_SENTINEL_STRUCTURED_SOURCE\n"
            "organization: أكاديمية طويق\n"
            "type: course\n"
            "city: عن بعد\n"
            "mode: online\n"
            "majors: \n"
            "deadline: 2099-01-01\n"
            "registration_status: open\n"
            "apply: https://tuwaiq.edu.sa/bootcamp/example/view"
        ),
    )
    candidate = DiscoveryAgent(InMemoryResearchTools([page])).extract(page.__dict__)
    assert candidate is not None
    assert candidate.accepted_majors == []
    assert not candidate.evidence_for("accepted_majors")


def test_structured_technical_evidence_can_replace_unspecified_majors() -> None:
    page = SourcePage(
        url="https://tuwaiq.edu.sa/bootcamp/example/view",
        title="برنامج Python",
        official=True,
        content=(
            "OPPORTUNITY_SENTINEL_STRUCTURED_SOURCE\n"
            "organization: أكاديمية طويق\n"
            "type: bootcamp\n"
            "city: الرياض\n"
            "mode: in_person\n"
            "majors: \n"
            "deadline: 2099-01-01\n"
            "registration_status: open\n"
            "cost: free\n"
            "technical_focus: true\n"
            "technical_evidence: برنامج تطوير البرمجيات باستخدام Python\n"
            "apply: https://tuwaiq.edu.sa/bootcamp/example/view"
        ),
    )

    candidate = DiscoveryAgent(InMemoryResearchTools([page])).extract(page.__dict__)

    assert candidate is not None
    assert candidate.opportunity_type == OpportunityType.BOOTCAMP
    assert candidate.technical_focus is True
    assert candidate.evidence_for("technical_focus")
    assert VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED


def test_non_public_address_classes_are_blocked() -> None:
    assert _is_public_address("93.184.216.34") is True
    assert _is_public_address("127.0.0.1") is False
    assert _is_public_address("10.0.0.1") is False
    assert _is_public_address("169.254.1.1") is False
    assert _is_public_address("::1") is False


def test_trusted_saudi_opportunity_sources_are_recognized() -> None:
    assert _looks_official("https://tuwaiq.edu.sa/bootcamp/example") is True
    assert _looks_official("https://hub.misk.org.sa/ar/programs/skills/example") is True
    assert _looks_official("https://riyadh.sa/ar/article/example") is True
    assert _looks_official("https://jobs.sabic.com/opportunity") is True
    assert _looks_official("https://careers.stc.com.sa/internship") is True
    assert _looks_official("https://untrusted.example/opportunity") is False


def test_redirect_to_private_network_is_not_followed(monkeypatch) -> None:
    calls: list[str] = []

    def resolve(host: str, port: int):
        address = "93.184.216.34" if host == "public.test" else host
        return [(0, 0, 0, "", (address, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(tools_module, "getaddrinfo", resolve)
    research = WebResearchTools()
    research.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )

    page, observation = research.open_page("https://public.test/opportunity")

    assert page is None
    assert observation.success is False
    assert calls == ["https://public.test/opportunity"]


def test_tavily_is_a_structured_observable_search_tool(monkeypatch) -> None:
    def resolve(host: str, port: int):
        return [(0, 0, 0, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer redacted-test-key"
        payload = json.loads(request.content)
        assert "spa.gov.sa" in payload["include_domains"]
        assert "athkax.sdaia.gov.sa" in payload["include_domains"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Current Riyadh CO-OP",
                        "url": "https://careers.example/current",
                        "content": "registration open",
                        "raw_content": "Official application deadline and requirements",
                    }
                ],
                "usage": {"credits": 2},
                "request_id": "request-123",
            },
        )

    monkeypatch.setattr(tools_module, "getaddrinfo", resolve)
    research = WebResearchTools(tavily_api_key="redacted-test-key")
    research.client = httpx.Client(transport=httpx.MockTransport(handler))

    pages, observation = research.search_web("technical courses Riyadh -site:tuwaiq.edu.sa")

    assert len(pages) == 1
    assert pages[0].content.startswith("TAVILY_EXTRACTED_SOURCE")
    assert observation.tool == "tavily_search"
    assert observation.metadata["credits"] == 2
    assert observation.metadata["request_id"] == "request-123"


def test_tuwaiq_connector_paginates_and_preserves_official_category() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetInitiativePublishesShorten/20/1"):
            return httpx.Response(
                200,
                json={
                    "pagination": {"totalPages": 2},
                    "data": [
                        {
                            "id": "current",
                            "slug": "technical-camp",
                            "title": "معسكر تطوير البرمجيات",
                            "initiativeCategoryName": "معسكر",
                            "isOpen": True,
                            "isRegistrationOpen": True,
                            "isRegistrationClosed": False,
                            "isPaid": True,
                            "registrationEndDate": "2099-01-01T12:00:00+03:00",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/GetInitiativePublishesShorten/20/2"):
            return httpx.Response(
                200,
                json={
                    "pagination": {"totalPages": 2},
                    "data": [
                        {
                            "id": "past",
                            "slug": "past",
                            "registrationEndDate": "2020-01-01T12:00:00+03:00",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/GetInitiativePublishBySlug/technical-camp"):
            return httpx.Response(
                200,
                json={
                    "title": "معسكر تطوير البرمجيات",
                    "description": "تطوير تطبيقات الويب باستخدام Python",
                    "initiativeCategoryName": "معسكر",
                    "locationName": "الرياض - المقر الرئيسي",
                    "registrationEndDate": "2099-01-01T12:00:00+03:00",
                    "requirements": [],
                },
            )
        return httpx.Response(404)

    research = WebResearchTools()
    research.client = httpx.Client(transport=httpx.MockTransport(handler))

    pages, observation = research.search_web("برامج مفتوحة site:tuwaiq.edu.sa")

    assert observation.success is True
    assert len(pages) == 1
    assert "type: bootcamp" in pages[0].content
    assert "technical_focus: true" in pages[0].content


def test_tuwaiq_index_fallback_requires_official_open_technical_location_evidence() -> None:
    page = _tuwaiq_index_page(
        {
            "href": "https://tuwaiq.edu.sa/bootcamp/current-ai/view",
            "title": "معسكر تطوير حلول الذكاء الاصطناعي",
            "body": "متاح التسجيل حضوريًا في الرياض - المقر الرئيسي",
        }
    )
    assert page is not None
    assert page.official is True
    assert "registration_status: open" in page.content
    assert "technical_focus: true" in page.content
    assert "mode: in_person" in page.content

    assert _tuwaiq_index_page(
        {
            "href": "https://tuwaiq.edu.sa/bootcamp/closed/view",
            "title": "معسكر Python",
            "body": "انتهى التسجيل في الرياض",
        }
    ) is None


def test_tuwaiq_official_channel_supports_fresh_unknown_location_without_guessing() -> None:
    today = date.today().isoformat()
    soup = BeautifulSoup(
        f"""
        <div class="tgme_widget_message" data-post="TuwaiqAcademy/999">
          <time datetime="{today}T09:00:00+03:00"></time>
          <div class="tgme_widget_message_text">
            طوّر جاهزيتك المهنية مع دبلوم الأمن السيبراني المتقدم.
            سجّل الآن:
            <a href="https://tuwaiq.edu.sa/bootcamp/current-cyber/view">الرابط</a>
          </div>
        </div>
        """,
        "html.parser",
    )

    page = _tuwaiq_channel_page(soup.select_one(".tgme_widget_message"))

    assert page is not None
    assert "mode: unknown" in page.content
    assert "city:" not in page.content
    candidate = DiscoveryAgent(InMemoryResearchTools([page])).extract(page.__dict__)
    assert candidate is not None
    assert candidate.delivery_mode.value == "unknown"
    assert candidate.city is None
    assert VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED


def test_tavily_converts_positive_site_operators_to_domain_filters(monkeypatch) -> None:
    def resolve(host: str, port: int):
        return [(0, 0, 0, "", ("93.184.216.34", port))]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["include_domains"] == ["linkedin.com", "x.com"]
        return httpx.Response(200, json={"results": [], "usage": {"credits": 2}})

    monkeypatch.setattr(tools_module, "getaddrinfo", resolve)
    research = WebResearchTools(tavily_api_key="redacted-test-key")
    research.client = httpx.Client(transport=httpx.MockTransport(handler))

    research.search_web("فرص تقنية site:linkedin.com site:x.com")


def test_tavily_basic_reserves_one_credit_and_requests_twenty_results(monkeypatch) -> None:
    reserved: list[int] = []

    monkeypatch.setattr(
        tools_module,
        "getaddrinfo",
        lambda host, port: [(0, 0, 0, "", ("93.184.216.34", port))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["search_depth"] == "basic"
        assert payload["max_results"] == 20
        assert "chunks_per_source" not in payload
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Technical internship",
                        "url": "https://careers.example/internship",
                        "content": "Apply",
                    }
                ],
                "usage": {"credits": 1},
            },
        )

    research = WebResearchTools(
        max_results=20,
        tavily_api_key="redacted-test-key",
        tavily_quota_guard=lambda units: reserved.append(units) is None,
        tavily_search_depth="basic",
    )
    research.client = httpx.Client(transport=httpx.MockTransport(handler))

    _, observation = research.search_web("technical internships Riyadh")

    assert reserved == [1]
    assert observation.metadata["search_depth"] == "basic"
