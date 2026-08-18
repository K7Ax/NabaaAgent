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
    """Reopen the application page and report what it proves.

    Three outcomes, and the difference between the last two matters:

    * ``closed`` — the page itself says registration has ended. Positive evidence.
    * ``open`` — the page itself says applications are being accepted. Positive evidence.
    * ``unverified`` — no evidence either way, because the host refused the request
      (tuwaiq.edu.sa answers 403 to anything that is not a browser) or because the page
      loaded but its wording matches none of the markers. This is the absence of a
      signal, **not** proof that the opportunity is gone, and callers must not treat it
      as such: every delivery used to be blocked on it, so the bot verified 148
      opportunities and sent students none of them.
    """
    tools = WebResearchTools(max_results=1, timeout=timeout)
    page, observation = tools.open_page(str(candidate.application_url))
    if not page:
        return RevalidationResult("unverified", observation.detail)
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
    open_markers = {
        "registration open",
        "applications open",
        "apply now",
        "apply for this job",
        "submit application",
        "التسجيل مفتوح",
        "التقديم مفتوح",
        "التقديم متاح",
        "سجل الآن",
        "قدّم الآن",
        "قدم الآن",
    }
    if any(marker in content for marker in open_markers):
        return RevalidationResult("open", "official application page proves applications are open")
    return RevalidationResult(
        "unverified",
        "application page is reachable but no current open-registration evidence was found",
    )
