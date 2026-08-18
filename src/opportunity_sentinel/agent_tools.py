"""Tools the discovery model chooses to call, plus the ReAct agent that calls them.

Each tool is a thin adapter over the existing :class:`~opportunity_sentinel.tools.ResearchTools`
implementation, so the Tavily credit guard, the SSRF checks in ``open_page``, and the
Tuwaiq fallback chain all still apply. The model decides *which* tool to call and *when*
to stop; the collected pages then flow into the unchanged extract/verify chain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opportunity_sentinel.logging import logger
from opportunity_sentinel.models import ToolObservation
from opportunity_sentinel.tools import ResearchTools, SourcePage

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from langchain_core.language_models.chat_models import BaseChatModel

# Long pages blow the context window and the free-tier token budget. The agent only needs
# enough text to decide whether a page is worth keeping; extraction re-reads the full page.
SNIPPET_CHARS = 600

DISCOVERY_SYSTEM_PROMPT = """\
You find real student opportunities in Saudi Arabia (bootcamps, jobs, internships, \
scholarships, hackathons).

Work in this order:
1. Call search_web with a focused Arabic query, or tuwaiq_catalog when the request is \
about training courses or bootcamps.
2. Read the results. Call open_page on any promising URL that is not already extracted, \
to confirm it is a real, currently open opportunity with an application link.
3. Stop as soon as you have gathered enough evidence, and reply with a one-line summary \
of what you found.

Rules: call at most one tool per step, never invent URLs, and prefer first-party official \
sources (tuwaiq.edu.sa, careers pages, university sites) over aggregators.\
"""


@dataclass
class PageCollector:
    """Records every page the tools return so the workflow can use them after the run."""

    pages: dict[str, SourcePage] = field(default_factory=dict)
    observations: list[dict] = field(default_factory=list)

    def add(self, page: SourcePage) -> None:
        self.pages.setdefault(page.url, page)

    def record(self, observation: ToolObservation) -> None:
        self.observations.append(observation.model_dump(mode="json"))

    def collected(self) -> list[dict]:
        return [
            {
                "url": page.url,
                "title": page.title,
                "content": page.content,
                "official": page.official,
            }
            for page in self.pages.values()
        ]


def _summarise(pages: list[SourcePage]) -> str:
    return json.dumps(
        [
            {
                "url": page.url,
                "title": page.title,
                "official": page.official,
                "snippet": page.content[:SNIPPET_CHARS],
            }
            for page in pages
        ],
        ensure_ascii=False,
    )


def build_tools(tools: ResearchTools, collector: PageCollector) -> list:
    """Wrap the research tools as LangChain tools bound to one collector."""
    from langchain_core.tools import tool

    @tool
    def search_web(query: str) -> str:
        """Search the web for Saudi student opportunities. Input: an Arabic search query."""
        pages, observation = tools.search_web(query)
        collector.record(observation)
        for page in pages:
            collector.add(page)
        return _summarise(pages) if pages else "[]"

    @tool
    def tuwaiq_catalog(query: str) -> str:
        """Search the Tuwaiq Academy catalogue specifically, for courses and bootcamps."""
        pages, observation = tools.search_web(f"{query} site:tuwaiq.edu.sa")
        collector.record(observation)
        for page in pages:
            collector.add(page)
        return _summarise(pages) if pages else "[]"

    @tool
    def open_page(url: str) -> str:
        """Open one URL and read its text, to confirm an opportunity is real and open."""
        page, observation = tools.open_page(url)
        collector.record(observation)
        if page is None:
            return json.dumps({"url": url, "error": "unreachable"}, ensure_ascii=False)
        collector.add(page)
        return json.dumps(
            {
                "url": page.url,
                "title": page.title,
                "official": page.official,
                "content": page.content[:SNIPPET_CHARS * 2],
            },
            ensure_ascii=False,
        )

    return [search_web, tuwaiq_catalog, open_page]


def run_discovery_agent(
    chat_model: BaseChatModel,
    tools: ResearchTools,
    query: str,
    *,
    recursion_limit: int = 12,
) -> tuple[list[dict], list[dict], str]:
    """Let the model drive discovery. Returns (pages, observations, final message).

    The caller is responsible for falling back to the deterministic collector when this
    returns no pages: free-tier models occasionally end a turn without calling any tool.
    """
    from langchain.agents import create_agent

    collector = PageCollector()
    agent = create_agent(
        chat_model,
        tools=build_tools(tools, collector),
        system_prompt=DISCOVERY_SYSTEM_PROMPT,
    )
    result = agent.invoke(
        {"messages": [("human", query)]},
        config={"recursion_limit": recursion_limit},
    )
    messages = result.get("messages", [])
    tool_calls = sum(len(getattr(message, "tool_calls", []) or []) for message in messages)
    final = messages[-1].content if messages else ""
    logger.info(
        "agentic_discovery",
        query=query,
        tool_calls=tool_calls,
        pages=len(collector.pages),
    )
    return collector.collected(), collector.observations, str(final)
