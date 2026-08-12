from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

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

