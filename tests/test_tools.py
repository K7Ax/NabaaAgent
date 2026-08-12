import httpx

import opportunity_sentinel.tools as tools_module
from opportunity_sentinel.tools import WebResearchTools, _is_public_address, _looks_official


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
