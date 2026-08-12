from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from opportunity_sentinel.agents import DiscoveryAgent, EligibilityMatcher, VerificationAgent
from opportunity_sentinel.logging import logger
from opportunity_sentinel.models import (
    AgentMessage,
    EligibilityDecision,
    OpportunityCandidate,
    ReasoningStep,
    StudentProfile,
    VerificationReport,
    VerificationStatus,
)
from opportunity_sentinel.security import scan_untrusted_content
from opportunity_sentinel.state import OpportunityState


def build_graph(
    discovery_agent: DiscoveryAgent,
    verification_agent: VerificationAgent,
    checkpoint_path: Path,
    max_research_attempts: int = 2,
):
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    eligibility_matcher = EligibilityMatcher()

    def discover(state: OpportunityState) -> dict:
        attempt = state.get("search_attempts", 0) + 1
        query = state["search_query"]
        if state.get("verification"):
            missing = state["verification"].get("missing_fields", [])
            query = f"{query} official {' '.join(missing)}"
        pages, observations = discovery_agent.discover(query)
        successful_tools = [item["tool"] for item in observations if item["success"]]
        trace = ReasoningStep(
            decision=(
                "Search again for missing evidence using first-party sources"
                if state.get("verification")
                else "Discover current opportunities using first-party tools before web fallback"
            ),
            action=" -> ".join(successful_tools) or "search_web",
            observation=f"Retrieved {len(pages)} candidate pages",
            attempt=attempt,
        )
        logger.info("graph_node", node="discover", attempt=attempt, pages=len(pages))
        return {
            "search_attempts": attempt,
            "candidate_pages": pages,
            "observations": observations,
            "reasoning_trace": [trace.model_dump(mode="json")],
        }

    def sanitize(state: OpportunityState) -> dict:
        pages = state.get("candidate_pages", [])
        if not pages:
            return {"current_page": None, "errors": ["no_candidate_pages"]}
        safe_pages = []
        for page in pages:
            scan = scan_untrusted_content(page["content"])
            if scan.safe:
                safe_pages.append(page)
            else:
                logger.warning(
                    "security_guardrail",
                    attack="indirect_prompt_injection",
                    blocked=True,
                    source_url=page.get("url"),
                )
        if safe_pages:
            logger.info(
                "graph_node",
                node="sanitize",
                result="safe",
                safe_pages=len(safe_pages),
            )
            return {"candidate_pages": safe_pages, "current_page": safe_pages[0]}
        logger.warning("security_guardrail", attack="indirect_prompt_injection", blocked=True)
        return {
            "current_page": None,
            "errors": ["prompt_injection_blocked"],
            "final_status": VerificationStatus.REJECTED.value,
        }

    def extract(state: OpportunityState) -> dict:
        pages = state.get("candidate_pages", [])
        if not pages:
            return {"candidate": None}
        candidate = None
        selected_page = None
        remaining_pages = []
        for index, page in enumerate(pages):
            candidate = discovery_agent.extract(page)
            if candidate:
                selected_page = page
                remaining_pages = pages[index + 1 :]
                break
        logger.info("graph_node", node="extract", success=candidate is not None)
        message = (
            AgentMessage(
                sender="DiscoveryAgent",
                recipient="VerificationAgent",
                message_type="candidate_for_independent_review",
                payload={
                    "source_url": str(candidate.source_url),
                    "evidence_count": len(candidate.evidence),
                },
            )
            if candidate
            else AgentMessage(
                sender="DiscoveryAgent",
                recipient="Coordinator",
                message_type="candidate_extraction_failed",
                payload={"remaining_pages": len(remaining_pages)},
            )
        )
        return {
            "candidate": candidate.model_dump(mode="json") if candidate else None,
            "current_page": selected_page,
            "candidate_pages": remaining_pages,
            "agent_messages": [message.model_dump(mode="json")],
        }

    def verify(state: OpportunityState) -> dict:
        candidate_data = state.get("candidate")
        if not candidate_data:
            report = VerificationReport(
                status=VerificationStatus.NEEDS_RESEARCH,
                score=0,
                missing_fields=["extractable_candidate"],
                reasons=["candidate_extraction_failed"],
            )
        else:
            report = verification_agent.verify(OpportunityCandidate.model_validate(candidate_data))
        logger.info(
            "graph_node", node="verify", status=report.status.value, score=report.score
        )
        message = AgentMessage(
            sender="VerificationAgent",
            recipient="LangGraphCoordinator",
            message_type="verification_report",
            payload={
                "status": report.status.value,
                "score": report.score,
                "missing_fields": report.missing_fields,
            },
        )
        return {
            "verification": report.model_dump(mode="json"),
            "agent_messages": [message.model_dump(mode="json")],
        }

    def approval(state: OpportunityState) -> Command[Literal["eligibility", "reject", "discover"]]:
        response = interrupt(
            {
                "kind": "opportunity_review",
                "candidate": state.get("candidate"),
                "verification": state.get("verification"),
                "allowed_decisions": ["approve", "reject", "research_again"],
            }
        )
        decision = response.get("decision") if isinstance(response, dict) else response
        logger.info("human_review_resumed", decision=decision)
        routes = {"approve": "eligibility", "reject": "reject", "research_again": "discover"}
        return Command(
            update={"human_decision": str(decision)},
            goto=routes.get(decision, "reject"),
        )

    def eligibility(state: OpportunityState) -> dict:
        profile_data = state.get("student_profile")
        if not profile_data:
            return {"eligibility": EligibilityDecision(eligible=True).model_dump(mode="json")}
        decision = eligibility_matcher.match(
            OpportunityCandidate.model_validate(state["candidate"]),
            StudentProfile.model_validate(profile_data),
        )
        logger.info("graph_node", node="eligibility", eligible=decision.eligible)
        return {"eligibility": decision.model_dump(mode="json")}

    def publish(_: OpportunityState) -> dict:
        logger.info("graph_node", node="publish", status="verified")
        return {"final_status": VerificationStatus.VERIFIED.value}

    def reject(state: OpportunityState) -> dict:
        logger.info("graph_node", node="reject", status="rejected")
        return {"final_status": state.get("final_status") or VerificationStatus.REJECTED.value}

    def after_sanitize(state: OpportunityState) -> str:
        return "extract" if state.get("current_page") else "reject"

    def after_verify(state: OpportunityState) -> str:
        report = VerificationReport.model_validate(state["verification"])
        if report.status != VerificationStatus.VERIFIED and state.get("candidate_pages"):
            return "extract"
        if report.status == VerificationStatus.VERIFIED:
            return "eligibility"
        if report.status == VerificationStatus.REJECTED:
            return "reject"
        if (
            report.status == VerificationStatus.NEEDS_RESEARCH
            and state.get("search_attempts", 0) < max_research_attempts
        ):
            return "discover"
        return "approval"

    def after_eligibility(state: OpportunityState) -> str:
        decision = EligibilityDecision.model_validate(state["eligibility"])
        return "publish" if decision.eligible else "reject"

    builder = StateGraph(OpportunityState)
    builder.add_node("discover", discover)
    builder.add_node("sanitize", sanitize)
    builder.add_node("extract", extract)
    builder.add_node("verify", verify)
    builder.add_node("approval", approval)
    builder.add_node("eligibility", eligibility)
    builder.add_node("publish", publish)
    builder.add_node("reject", reject)
    builder.add_edge(START, "discover")
    builder.add_edge("discover", "sanitize")
    builder.add_conditional_edges("sanitize", after_sanitize, ["extract", "reject"])
    builder.add_edge("extract", "verify")
    builder.add_conditional_edges(
        "verify",
        after_verify,
        ["eligibility", "reject", "discover", "approval", "extract"],
    )
    builder.add_conditional_edges("eligibility", after_eligibility, ["publish", "reject"])
    builder.add_edge("publish", END)
    builder.add_edge("reject", END)
    return builder.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}
