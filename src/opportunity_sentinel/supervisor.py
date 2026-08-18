"""LLM supervisor that classifies an incoming student request and routes it.

This replaces keyword matching as the routing mechanism. The model returns a
:class:`RouteDecision` through structured output, and the workflow branches on the
``route`` literal. The supervisor also normalises the search query and lifts any durable
facts out of the message so they can be written to long-term memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from opportunity_sentinel.chat_models import build_structured
from opportunity_sentinel.config import Settings
from opportunity_sentinel.logging import logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from langchain_core.runnables import Runnable

Route = Literal[
    "find_courses_bootcamps",
    "find_jobs_internships",
    "find_hackathons_events",
    "find_scholarships",
    "ask_knowledge",
    "update_profile",
]

SUPERVISOR_SYSTEM_PROMPT = """\
You route requests for Nabaa, an Arabic assistant that finds verified student \
opportunities in Saudi Arabia. Most messages are Arabic; some are English.

Choose exactly one route:
- find_courses_bootcamps: training courses, bootcamps, camps (دورة، دورات، معسكر، تدريب).
- find_jobs_internships: jobs, internships, co-op, graduate programmes (وظيفة، تدريب \
تعاوني، توظيف، خريجين).
- find_hackathons_events: hackathons, competitions, events, volunteering (هاكاثون، \
مسابقة، فعالية، تطوع).
- find_scholarships: scholarships and funded study (منحة، ابتعاث).
- ask_knowledge: a question ABOUT opportunities, eligibility rules, or how Nabaa works, \
answerable from the knowledge base rather than a fresh search (هل دورات طويق مجانية؟).
- update_profile: the student states a durable preference or fact about themselves \
(أفضل العمل عن بعد، تخرجي 2027).

Set search_query to a concise Arabic search query for the discovery routes; leave it \
empty for ask_knowledge and update_profile.

Set extracted_facts to durable, reusable facts about the student, written as short \
key=value strings such as "prefers_remote=true" or "graduation_year=2027". Leave the \
list empty when the message states no durable fact. Never treat a one-off search \
request as a durable fact.\
"""


class RouteDecision(BaseModel):
    """The supervisor's classification of a single student message."""

    route: Route = Field(description="Which specialist handles this request")
    search_query: str = Field(default="", description="Normalised query for discovery routes")
    extracted_facts: list[str] = Field(
        default_factory=list,
        description="Durable student facts as key=value strings, empty when none",
    )
    reason: str = Field(default="", description="One short sentence explaining the choice")


DISCOVERY_ROUTES: frozenset[str] = frozenset(
    {
        "find_courses_bootcamps",
        "find_jobs_internships",
        "find_hackathons_events",
        "find_scholarships",
    }
)

# Each discovery route searches different first-party sources. Tuwaiq dominates Saudi
# training, whereas jobs live on ATS boards, so the query strategy differs per route.
ROUTE_QUERY_HINTS: dict[str, str] = {
    "find_courses_bootcamps": "site:tuwaiq.edu.sa OR معسكر تدريبي معتمد السعودية",
    "find_jobs_internships": "تدريب تعاوني OR وظائف خريجين السعودية careers",
    "find_hackathons_events": "هاكاثون OR مسابقة تقنية السعودية التسجيل",
    "find_scholarships": "منحة دراسية OR ابتعاث السعودية التقديم",
}


class Supervisor:
    """Classifies student messages with structured output."""

    def __init__(self, runnable: Runnable) -> None:
        self._runnable = runnable

    @classmethod
    def from_settings(cls, settings: Settings) -> Supervisor:
        return cls(build_structured(settings, RouteDecision))

    def classify(self, message: str) -> RouteDecision:
        decision = self._runnable.invoke(
            [
                ("system", SUPERVISOR_SYSTEM_PROMPT),
                ("human", message),
            ]
        )
        if not isinstance(decision, RouteDecision):  # defensive: providers can return dicts
            decision = RouteDecision.model_validate(decision)
        logger.info(
            "supervisor_route",
            route=decision.route,
            facts=len(decision.extracted_facts),
        )
        return decision


def refine_query(decision: RouteDecision, missing_fields: list[str] | None = None) -> str:
    """Build the discovery query, appending evidence gaps on a re-research attempt."""
    query = decision.search_query.strip() or decision.reason.strip()
    hint = ROUTE_QUERY_HINTS.get(decision.route)
    if hint:
        query = f"{query} {hint}".strip()
    if missing_fields:
        query = f"{query} official {' '.join(missing_fields)}".strip()
    return query
