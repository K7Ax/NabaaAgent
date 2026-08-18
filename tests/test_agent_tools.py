import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from opportunity_sentinel.agent_tools import PageCollector, build_tools, run_discovery_agent
from opportunity_sentinel.tools import InMemoryResearchTools, SourcePage


def _tools() -> InMemoryResearchTools:
    return InMemoryResearchTools(
        [
            SourcePage(
                url="https://tuwaiq.edu.sa/bootcamp",
                title="AI bootcamp",
                official=True,
                content="معسكر الذكاء الاصطناعي مجاني apply: https://tuwaiq.edu.sa/apply",
            )
        ]
    )


def test_search_tool_returns_json_and_collects_pages() -> None:
    collector = PageCollector()
    search_web, _tuwaiq, _open = build_tools(_tools(), collector)

    payload = json.loads(search_web.invoke({"query": "معسكر"}))

    assert payload[0]["url"] == "https://tuwaiq.edu.sa/bootcamp"
    assert collector.pages
    assert collector.observations[0]["tool"] == "search_web"


def test_open_page_tool_reports_unreachable_urls_without_raising() -> None:
    collector = PageCollector()
    _search, _tuwaiq, open_page = build_tools(_tools(), collector)

    payload = json.loads(open_page.invoke({"url": "https://missing.example/none"}))

    assert payload["error"] == "unreachable"


def test_tuwaiq_tool_scopes_the_query_to_the_academy() -> None:
    tools = _tools()
    collector = PageCollector()
    _search, tuwaiq_catalog, _open = build_tools(tools, collector)

    tuwaiq_catalog.invoke({"query": "معسكر"})

    assert "site:tuwaiq.edu.sa" in collector.observations[0]["metadata"]["query"]


def test_collector_deduplicates_pages_seen_through_two_tools() -> None:
    tools = _tools()
    collector = PageCollector()
    search_web, tuwaiq_catalog, _open = build_tools(tools, collector)

    search_web.invoke({"query": "معسكر"})
    tuwaiq_catalog.invoke({"query": "معسكر"})

    assert len(collector.collected()) == 1


class ScriptedToolCallingModel(BaseChatModel):
    """Minimal chat model that replays a fixed list of AI messages and accepts tools."""

    replies: list

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002 - tools are ignored on purpose
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.replies[min(self._sent, len(self.replies) - 1)]
        object.__setattr__(self, "_sent", self._sent + 1)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    _sent: int = 0


def test_react_agent_lets_the_model_choose_the_tool() -> None:
    """A scripted model emits one tool call, then answers. No network involved."""
    scripted = ScriptedToolCallingModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[{"name": "search_web", "args": {"query": "معسكر"}, "id": "call_1"}],
            ),
            AIMessage(content="وجدت معسكر طويق للذكاء الاصطناعي."),
        ]
    )

    pages, observations, final = run_discovery_agent(scripted, _tools(), "ابحث عن معسكر")

    assert [page["url"] for page in pages] == ["https://tuwaiq.edu.sa/bootcamp"]
    assert observations[0]["tool"] == "search_web"
    assert "طويق" in final
