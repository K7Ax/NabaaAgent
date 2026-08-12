from __future__ import annotations

import re
from datetime import date
from typing import Any

from opportunity_sentinel.models import (
    DeliveryMode,
    Evidence,
    OpportunityCandidate,
    OpportunityType,
    VerificationReport,
    VerificationStatus,
)
from opportunity_sentinel.security import scan_untrusted_content
from opportunity_sentinel.tools import ResearchTools, SourcePage


class DiscoveryAgent:
    """ReAct-style agent: chooses search/open actions and records observations."""

    def __init__(self, tools: ResearchTools) -> None:
        self.tools = tools

    def discover(self, query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pages, search_observation = self.tools.search_web(query)
        observations = [search_observation.model_dump(mode="json")]
        safe_pages: list[dict[str, Any]] = []
        for page in pages:
            opened, open_observation = self.tools.open_page(page.url)
            observations.append(open_observation.model_dump(mode="json"))
            if opened:
                safe_pages.append(_page_to_dict(opened))
        return safe_pages, observations

    def extract(self, page_data: dict[str, Any]) -> OpportunityCandidate | None:
        page = SourcePage(**page_data)
        if not scan_untrusted_content(page.content).safe:
            return None

        deadline_match = re.search(r"deadline:\s*(\d{4}-\d{2}-\d{2})", page.content, re.I)
        city_match = re.search(r"city:\s*([^\n]+)", page.content, re.I)
        org_match = re.search(r"organization:\s*([^\n]+)", page.content, re.I)
        type_match = re.search(r"type:\s*(internship|coop|course)", page.content, re.I)
        mode_match = re.search(r"mode:\s*(in_person|online|hybrid)", page.content, re.I)
        majors_match = re.search(r"majors:\s*([^\n]+)", page.content, re.I)
        apply_match = re.search(r"apply:\s*(https?://\S+)", page.content, re.I)

        required = [org_match, type_match, mode_match, apply_match]
        if not all(required):
            return None

        evidence: list[Evidence] = []
        facts = {
            "organization": org_match.group(1).strip(),
            "city": city_match.group(1).strip() if city_match else None,
            "deadline": deadline_match.group(1) if deadline_match else None,
        }
        for field_name, value in facts.items():
            if value:
                evidence.append(
                    Evidence(
                        field_name=field_name,
                        value=value,
                        quote=f"{field_name}: {value}",
                        source_url=page.url,
                        official_source=page.official,
                    )
                )

        return OpportunityCandidate(
            title=page.title,
            organization=org_match.group(1).strip(),
            opportunity_type=OpportunityType(type_match.group(1).lower()),
            city=city_match.group(1).strip() if city_match else None,
            delivery_mode=DeliveryMode(mode_match.group(1).lower()),
            accepted_majors=(
                [item.strip() for item in majors_match.group(1).split(",")]
                if majors_match
                else []
            ),
            deadline=date.fromisoformat(deadline_match.group(1)) if deadline_match else None,
            application_url=apply_match.group(1),
            source_url=page.url,
            evidence=evidence,
        )


class VerificationAgent:
    """Independent evidence reviewer; deterministic policy remains outside the LLM."""

    def verify(
        self, candidate: OpportunityCandidate, today: date | None = None
    ) -> VerificationReport:
        today = today or date.today()
        missing: list[str] = []
        reasons: list[str] = []

        if not candidate.evidence_for("organization"):
            missing.append("organization_evidence")
        if candidate.delivery_mode != DeliveryMode.ONLINE and not candidate.evidence_for("city"):
            missing.append("city_evidence")
        if candidate.deadline is None:
            missing.append("deadline")
        elif candidate.deadline < today:
            return VerificationReport(
                status=VerificationStatus.REJECTED,
                score=0,
                reasons=["application_deadline_expired"],
            )
        elif not candidate.evidence_for("deadline"):
            missing.append("deadline_evidence")

        if (
            candidate.delivery_mode != DeliveryMode.ONLINE
            and candidate.city
            and candidate.city.casefold() not in {"riyadh", "الرياض"}
        ):
            return VerificationReport(
                status=VerificationStatus.REJECTED,
                score=0,
                reasons=["outside_riyadh_scope"],
            )

        official_evidence = sum(item.official_source for item in candidate.evidence)
        score = min(1.0, 0.25 + 0.2 * len(candidate.evidence) + 0.15 * official_evidence)
        if missing:
            reasons.append("required_evidence_is_missing")
            return VerificationReport(
                status=VerificationStatus.NEEDS_RESEARCH,
                score=min(score, 0.69),
                missing_fields=missing,
                reasons=reasons,
            )

        if score < 0.8:
            return VerificationReport(
                status=VerificationStatus.NEEDS_HUMAN_REVIEW,
                score=score,
                reasons=["evidence_confidence_below_publish_threshold"],
                requires_human_review=True,
            )

        return VerificationReport(
            status=VerificationStatus.VERIFIED,
            score=score,
            reasons=["required_fields_have_evidence"],
        )


def _page_to_dict(page: SourcePage) -> dict[str, Any]:
    return {
        "url": page.url,
        "title": page.title,
        "content": page.content,
        "official": page.official,
    }
