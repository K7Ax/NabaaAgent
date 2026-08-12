from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from ipaddress import ip_address
from socket import getaddrinfo
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from opportunity_sentinel.logging import logger
from opportunity_sentinel.models import ToolObservation


@dataclass(frozen=True)
class SourcePage:
    url: str
    title: str
    content: str
    official: bool = False


class ResearchTools(Protocol):
    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]: ...

    def open_page(self, url: str) -> tuple[SourcePage | None, ToolObservation]: ...


class InMemoryResearchTools:
    """Deterministic tool implementation for development, tests, and rubric evidence."""

    def __init__(self, pages: list[SourcePage]) -> None:
        self.pages = {page.url: page for page in pages}
        self.search_calls = 0

    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        started = time.perf_counter()
        self.search_calls += 1
        terms = {term.casefold() for term in query.split() if len(term) > 2}
        ranked = sorted(
            self.pages.values(),
            key=lambda page: len(terms.intersection(page.content.casefold().split())),
            reverse=True,
        )
        elapsed = (time.perf_counter() - started) * 1000
        observation = ToolObservation(
            tool="search_web",
            success=True,
            detail=f"Found {len(ranked)} candidate pages",
            latency_ms=elapsed,
            metadata={"query": query, "attempt": self.search_calls},
        )
        return ranked, observation

    def open_page(self, url: str) -> tuple[SourcePage | None, ToolObservation]:
        started = time.perf_counter()
        page = self.pages.get(url)
        elapsed = (time.perf_counter() - started) * 1000
        return page, ToolObservation(
            tool="open_page",
            success=page is not None,
            detail="Page opened" if page else "Page not found",
            latency_ms=elapsed,
            metadata={"url": url},
        )


class WebResearchTools:
    """Live web search and safe page retrieval tools used by the Discovery Agent."""

    def __init__(
        self,
        max_results: int = 5,
        timeout: float = 20,
        tavily_api_key: str | None = None,
    ) -> None:
        self.max_results = max_results
        self.tavily_api_key = tavily_api_key
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )

    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        folded_query = query.casefold()
        if (
            "site:tuwaiq.edu.sa" in folded_query
            and "-site:tuwaiq.edu.sa" not in folded_query
        ):
            return self._search_tuwaiq(query)
        if self.tavily_api_key:
            pages, observation = self._search_tavily(query)
            if observation.success and pages:
                return pages, observation
        started = time.perf_counter()
        try:
            results = list(
                DDGS(timeout=min(8, int(self.client.timeout.read))).text(
                    query, max_results=self.max_results
                )
            )
            pages = [
                SourcePage(
                    url=item["href"],
                    title=item.get("title") or "Untitled opportunity page",
                    content=item.get("body") or "",
                    official=_looks_official(item["href"]),
                )
                for item in results
                if item.get("href") and _public_http_url(item["href"])
            ]
            success = True
            detail = f"Found {len(pages)} live search results"
        except Exception as exc:  # the search backend exposes heterogeneous failures
            pages = []
            success = False
            detail = f"Search failed: {type(exc).__name__}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("tool_call", tool="search_web", success=success, latency_ms=elapsed)
        return pages, ToolObservation(
            tool="search_web",
            success=success,
            detail=detail,
            latency_ms=elapsed,
            metadata={"query": query, "result_count": len(pages)},
        )

    def _search_tavily(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        started = time.perf_counter()
        pages: list[SourcePage] = []
        credits = 0
        request_id = None
        try:
            payload = {
                "query": query[:400],
                "search_depth": "advanced",
                "chunks_per_source": 3,
                "max_results": self.max_results,
                "include_raw_content": "markdown",
                "include_answer": False,
                "time_range": "year",
            }
            if "-site:tuwaiq.edu.sa" in query.casefold():
                payload["include_domains"] = [
                    "athkax.sdaia.gov.sa",
                    "futurex.sa",
                    "hub.misk.org.sa",
                    "kacst.gov.sa",
                    "mcit.gov.sa",
                    "misk.org.sa",
                    "sdaia.gov.sa",
                    "spa.gov.sa",
                ]
            response = self.client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self.tavily_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            credits = body.get("usage", {}).get("credits", 0)
            request_id = body.get("request_id")
            for item in body.get("results", []):
                url = item.get("url")
                if not url or not _public_http_url(url):
                    continue
                content = item.get("raw_content") or item.get("content") or ""
                pages.append(
                    SourcePage(
                        url=url,
                        title=item.get("title") or "Untitled opportunity page",
                        content="TAVILY_EXTRACTED_SOURCE\n" + content[:60_000],
                        official=_looks_official(url),
                    )
                )
            success = True
            detail = f"Tavily returned {len(pages)} ranked and extracted results"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            success = False
            detail = f"Tavily failed: {type(exc).__name__}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_call",
            tool="tavily_search",
            success=success,
            latency_ms=elapsed,
            credits=credits,
            request_id=request_id,
        )
        return pages, ToolObservation(
            tool="tavily_search",
            success=success,
            detail=detail,
            latency_ms=elapsed,
            metadata={
                "query": query,
                "result_count": len(pages),
                "credits": credits,
                "request_id": request_id,
                "search_depth": "advanced",
            },
        )

    def _search_tuwaiq(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        """Use Tuwaiq's public first-party API instead of unreliable search snippets."""
        started = time.perf_counter()
        pages: list[SourcePage] = []
        try:
            response = self.client.get(
                "https://tuwaiq.edu.sa/api/GetInitiativePublishesShorten/100/1?type=NORMAL"
            )
            response.raise_for_status()
            rows = response.json().get("data", [])
            eligible_rows = [
                row
                for row in rows
                if row.get("isOpen")
                and row.get("isRegistrationOpen")
                and not row.get("isRegistrationClosed")
                and not row.get("isPaid")
                and _future_or_today(row.get("registrationEndDate"))
            ]
            eligible_rows.sort(
                key=lambda row: _tuwaiq_relevance(row, query),
                reverse=True,
            )
            # Preserve enough first-party candidates for batch verification. The final
            # delivery layer still caps what the student receives in one search.
            for row in eligible_rows[: max(self.max_results, 10)]:
                slug = row.get("slug")
                if not slug:
                    continue
                detail_response = self.client.get(
                    f"https://tuwaiq.edu.sa/api/GetInitiativePublishBySlug/{slug}"
                )
                detail_response.raise_for_status()
                detail = detail_response.json()
                majors = _majors_from_requirements(detail.get("requirements") or [])
                city, mode = _location_from_tuwaiq(detail)
                deadline = (detail.get("registrationEndDate") or "")[:10]
                application_url = f"https://tuwaiq.edu.sa/bootcamp/{slug}/view"
                content = (
                    "OPPORTUNITY_SENTINEL_STRUCTURED_SOURCE\n"
                    "organization: أكاديمية طويق\n"
                    "type: course\n"
                    f"city: {city}\n"
                    f"mode: {mode}\n"
                    f"majors: {majors}\n"
                    f"deadline: {deadline}\n"
                    "registration_status: open\n"
                    f"apply: {application_url}\n"
                    "requirements: "
                    + " | ".join(detail.get("requirements") or [])
                )
                pages.append(
                    SourcePage(
                        url=application_url,
                        title=detail.get("title") or row.get("title") or "برنامج تقني",
                        content=content,
                        official=True,
                    )
                )
            success = True
            detail_text = f"Found {len(pages)} open free programs from Tuwaiq official API"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            success = False
            detail_text = f"Tuwaiq API failed: {type(exc).__name__}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_call",
            tool="tuwaiq_official_api",
            success=success,
            latency_ms=elapsed,
        )
        return pages, ToolObservation(
            tool="tuwaiq_official_api",
            success=success,
            detail=detail_text,
            latency_ms=elapsed,
            metadata={"result_count": len(pages), "source": "tuwaiq.edu.sa"},
        )

    def open_page(self, url: str) -> tuple[SourcePage | None, ToolObservation]:
        started = time.perf_counter()
        page: SourcePage | None = None
        detail = "Page retrieval failed"
        try:
            current_url = url
            response: httpx.Response | None = None
            for _ in range(4):
                if not _public_http_url(current_url):
                    raise ValueError("Blocked non-public URL")
                response = self.client.get(current_url)
                if not response.is_redirect:
                    break
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect is missing a location")
                current_url = urljoin(current_url, location)
            else:
                raise ValueError("Too many redirects")
            if response is None or response.is_redirect:
                raise ValueError("Redirect could not be resolved safely")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise ValueError(f"Unsupported content type: {content_type}")
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = "\n".join(
                line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
            )
            page = SourcePage(
                url=str(response.url),
                title=(soup.title.string.strip() if soup.title and soup.title.string else url),
                content=text[:60_000],
                official=_looks_official(str(response.url)),
            )
            detail = "Page opened and sanitized"
        except (httpx.HTTPError, ValueError) as exc:
            detail = f"Page failed: {type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_call",
            tool="open_page",
            success=page is not None,
            latency_ms=elapsed,
            url=_ascii_safe(url),
        )
        return page, ToolObservation(
            tool="open_page",
            success=page is not None,
            detail=detail,
            latency_ms=elapsed,
            metadata={"url": url},
        )


def _public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {info[4][0] for info in getaddrinfo(parsed.hostname, parsed.port or 443)}
        return all(_is_public_address(address) for address in addresses)
    except OSError:
        return False


def _is_public_address(address: str) -> bool:
    parsed = ip_address(address)
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _looks_official(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    trusted_roots = {
        "aramco.com",
        "futurex.sa",
        "misk.org.sa",
        "neom.com",
        "riyadh.sa",
        "sabic.com",
        "spa.gov.sa",
        "stc.com.sa",
    }
    return (
        any(host == root or host.endswith(f".{root}") for root in trusted_roots)
        or host.endswith(".gov.sa")
        or host.endswith(".edu.sa")
        or host.endswith(".org.sa")
    )


def _ascii_safe(value: str) -> str:
    return value.encode("ascii", "backslashreplace").decode("ascii")


def _future_or_today(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value).date() >= date.today()
    except ValueError:
        return False


def _tuwaiq_relevance(row: dict, query: str) -> int:
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "initiativeScopeName", "initiativeCategoryName")
    ).casefold()
    terms = {term.casefold() for term in query.split() if len(term) > 3}
    technical = (
        "البرمجيات",
        "الذكاء الاصطناعي",
        "الحوسبة",
        "الأمن السيبراني",
        "تقني",
        "بيانات",
        "برمجة",
    )
    return 10 * len(terms.intersection(text.split())) + 20 * sum(x in text for x in technical)


def _majors_from_requirements(requirements: list[str]) -> str:
    joined = " ".join(requirements).casefold()
    if "تخصص تقني" in joined or "التخصصات التقنية" in joined:
        return "التخصصات التقنية"
    return ""


def _location_from_tuwaiq(detail: dict) -> tuple[str, str]:
    location = str(detail.get("locationName") or detail.get("locationText") or "")
    if not location:
        return "عن بعد", "online"
    if "عن بعد" in location and "الرياض" in location:
        return "الرياض", "hybrid"
    if "عن بعد" in location:
        return "عن بعد", "online"
    return "الرياض" if "الرياض" in location else location, "in_person"
