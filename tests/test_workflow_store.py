"""Long-term memory must outlive a conversation.

Short-term state lives in the checkpointer and is scoped to a ``thread_id``. Durable
facts about a student live in the Store, keyed by the student rather than the thread, so
a preference stated in one conversation is still known in the next one.
"""

from pathlib import Path

from langgraph.store.memory import InMemoryStore

from opportunity_sentinel.agents import DiscoveryAgent, VerificationAgent
from opportunity_sentinel.supervisor import RouteDecision
from opportunity_sentinel.tools import InMemoryResearchTools, SourcePage
from opportunity_sentinel.workflow import (
    MEMORY_KEY,
    MEMORY_NAMESPACE,
    build_store,
    build_workflow,
    thread_config,
)


class ScriptedSupervisor:
    """Returns a queued decision per message, mimicking the LLM supervisor."""

    def __init__(self, decisions: list[RouteDecision]) -> None:
        self.decisions = list(decisions)

    def classify(self, message: str) -> RouteDecision:  # noqa: ARG002
        return self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]


def _build(tmp_path: Path, store, supervisor, page: SourcePage):
    return build_workflow(
        DiscoveryAgent(InMemoryResearchTools([page])),
        VerificationAgent(),
        tmp_path / "checkpoints.sqlite",
        store=store,
        supervisor=supervisor,
    )


def test_a_fact_written_in_one_thread_is_readable_from_another(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    store = InMemoryStore()
    supervisor = ScriptedSupervisor(
        [
            RouteDecision(route="update_profile", extracted_facts=["prefers_remote=true"]),
            RouteDecision(route="find_jobs_internships", search_query="coop"),
        ]
    )
    workflow = _build(tmp_path, store, supervisor, verified_page)

    # Thread A: the student states a durable preference.
    first = workflow.invoke(
        {"thread_id": "mem-a", "telegram_id": 99, "message": "أفضل الفرص عن بعد فقط"},
        config=thread_config("mem-a"),
    )
    assert first["final_status"] == "profile_updated"
    assert first["stored_facts"] == ["prefers_remote=true"]

    # Thread B: a different conversation entirely, same student.
    second = workflow.invoke(
        {"thread_id": "mem-b", "telegram_id": 99, "message": "ابحث لي عن تدريب"},
        config=thread_config("mem-b"),
    )

    assert second["thread_id"] == "mem-b"
    assert "prefers_remote=true" in second["remembered_facts"]


def test_long_term_memory_is_scoped_per_student(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    store = InMemoryStore()
    supervisor = ScriptedSupervisor(
        [
            RouteDecision(route="update_profile", extracted_facts=["prefers_remote=true"]),
            RouteDecision(route="find_jobs_internships", search_query="coop"),
        ]
    )
    workflow = _build(tmp_path, store, supervisor, verified_page)
    workflow.invoke(
        {"thread_id": "mem-a", "telegram_id": 99, "message": "أفضل عن بعد"},
        config=thread_config("mem-a"),
    )

    other_student = workflow.invoke(
        {"thread_id": "mem-c", "telegram_id": 1234, "message": "ابحث لي عن تدريب"},
        config=thread_config("mem-c"),
    )

    assert other_student["remembered_facts"] == []


def test_facts_accumulate_instead_of_overwriting(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    store = InMemoryStore()
    supervisor = ScriptedSupervisor(
        [
            RouteDecision(route="update_profile", extracted_facts=["prefers_remote=true"]),
            RouteDecision(route="update_profile", extracted_facts=["graduation_year=2027"]),
        ]
    )
    workflow = _build(tmp_path, store, supervisor, verified_page)
    workflow.invoke(
        {"thread_id": "acc-a", "telegram_id": 7, "message": "عن بعد"},
        config=thread_config("acc-a"),
    )

    second = workflow.invoke(
        {"thread_id": "acc-b", "telegram_id": 7, "message": "أتخرج 2027"},
        config=thread_config("acc-b"),
    )

    assert second["remembered_facts"] == ["graduation_year=2027", "prefers_remote=true"]


def test_short_term_state_stays_bound_to_its_own_thread(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    """The checkpointer keeps threads separate even while the Store is shared."""
    store = InMemoryStore()
    supervisor = ScriptedSupervisor(
        [RouteDecision(route="find_jobs_internships", search_query="coop")]
    )
    workflow = _build(tmp_path, store, supervisor, verified_page)
    workflow.invoke({"thread_id": "t-1", "search_query": "coop"}, config=thread_config("t-1"))

    state_one = workflow.get_state(thread_config("t-1"))
    state_two = workflow.get_state(thread_config("t-2"))

    assert state_one.values
    assert not state_two.values


def test_sqlite_store_persists_facts_across_processes(tmp_path: Path) -> None:
    """The durable Store keeps facts after the process that wrote them is gone."""
    path = tmp_path / "memory.sqlite"
    namespace = (MEMORY_NAMESPACE, "99")

    store = build_store(path)
    store.put(namespace, MEMORY_KEY, {"facts": ["prefers_remote=true"]})

    reopened = build_store(path)
    item = reopened.get(namespace, MEMORY_KEY)

    assert item is not None
    assert item.value["facts"] == ["prefers_remote=true"]
