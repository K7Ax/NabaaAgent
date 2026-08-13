from __future__ import annotations

from dataclasses import dataclass

from opportunity_sentinel.models import OpportunityCandidate
from opportunity_sentinel.tools import WebResearchTools


@dataclass(frozen=True)
class RevalidationResult:
    state: str
    reason: str


def revalidate_application(
    candidate: OpportunityCandidate, timeout: float = 20
) -> RevalidationResult:
    """Reopen the application page and fail closed on outages or closure text."""
    tools = WebResearchTools(max_results=1, timeout=timeout)
    page, observation = tools.open_page(str(candidate.application_url))
    if not page:
        return RevalidationResult("unavailable", observation.detail)
    content = page.content.casefold()
    closed_markers = {
        "registration closed",
        "applications closed",
        "no longer accepting applications",
        "التسجيل مغلق",
        "انتهى التسجيل",
        "انتهت فترة التقديم",
        "لا يمكن التقديم",
    }
    if any(marker in content for marker in closed_markers):
        return RevalidationResult("closed", "official application page is closed")
    return RevalidationResult("open", "official application page is reachable")
