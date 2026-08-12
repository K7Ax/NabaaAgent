from __future__ import annotations

import time
from dataclasses import dataclass
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

    def __init__(self, max_results: int = 8, timeout: float = 20) -> None:
        self.max_results = max_results
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )

    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        started = time.perf_counter()
        try:
            results = list(
                DDGS(timeout=int(self.client.timeout.read)).text(
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
            "tool_call", tool="open_page", success=page is not None, latency_ms=elapsed, url=url
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
    return host.endswith(".gov.sa") or host.endswith(".edu.sa") or host.endswith(".org.sa")
