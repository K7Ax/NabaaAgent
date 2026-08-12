import operator
from typing import Annotated, Any, TypedDict


class OpportunityState(TypedDict, total=False):
    thread_id: str
    search_query: str
    search_attempts: int
    candidate_pages: list[dict[str, Any]]
    current_page: dict[str, Any] | None
    candidate: dict[str, Any] | None
    verification: dict[str, Any] | None
    human_decision: str | None
    final_status: str | None
    observations: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]

