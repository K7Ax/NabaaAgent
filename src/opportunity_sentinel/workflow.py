"""The Nabaa agent pipeline, built with the LangGraph Functional API.

Every step is a ``@task`` and the orchestration is an ``@entrypoint``, so control flow is
ordinary Python (loops, conditionals) while durability, retries and human-in-the-loop
pauses stay with the framework.

Shape of a run:

1. ``route_request`` — the LLM supervisor classifies the message (structured output).
2. ``ask_knowledge`` requests go to ``answer_from_knowledge`` (RAG).
3. ``update_profile`` requests write durable facts to the long-term Store.
4. Discovery routes enter an **Evaluator-Optimizer** loop: discover → sanitize → extract
   → verify; when verification reports missing evidence, the query is refined with the
   gaps and discovery runs again, bounded by ``max_research_attempts``.
5. Anything still unverified pauses at ``interrupt()`` for a human decision.

Error handling uses two named strategies. Transient failures are retried by real
``RetryPolicy`` objects declared per task, and provider outages are absorbed by the
``.with_fallbacks()`` chain in :mod:`opportunity_sentinel.chat_models`. Long searches are
additionally bounded by a task ``timeout``.

Because the entrypoint body replays from the top when a run resumes, every side effect
lives inside a ``@task``: completed tasks are served from the checkpoint instead of being
re-executed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.func import entrypoint, task
from langgraph.types import RetryPolicy, interrupt

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
from opportunity_sentinel.supervisor import DISCOVERY_ROUTES, RouteDecision, refine_query

MEMORY_NAMESPACE = "student_facts"
MEMORY_KEY = "preferences"


def _is_transient_network_error(exc: Exception) -> bool:
    """Retry connection blips and 5xx/429 responses; never retry a 4xx we caused."""
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.ReadError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return False


NETWORK_RETRY = RetryPolicy(
    max_attempts=3,
    initial_interval=0.5,
    backoff_factor=2.0,
    jitter=True,
    retry_on=_is_transient_network_error,
)

LLM_RETRY = RetryPolicy(
    max_attempts=2,
    initial_interval=1.0,
    backoff_factor=3.0,
    jitter=True,
    retry_on=_is_transient_network_error,
)


def build_workflow(
    discovery_agent: DiscoveryAgent,
    verification_agent: VerificationAgent,
    checkpoint_path: Path,
    *,
    store,
    max_research_attempts: int = 2,
    supervisor=None,
    chat_model=None,
    knowledge_answerer=None,
    max_results: int = 5,
):
    """Compile the pipeline.

    ``supervisor``, ``chat_model`` and ``knowledge_answerer`` are optional so tests and
    the offline demo can run the whole graph without any API key. When ``supervisor`` is
    absent the caller's ``search_query`` is used directly; when ``chat_model`` is absent
    discovery uses the deterministic collector instead of the tool-calling agent.
    """
    connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    eligibility_matcher = EligibilityMatcher()

    @task(retry_policy=LLM_RETRY)
    def route_request(message: str) -> dict[str, Any]:
        """Ask the supervisor which specialist should handle this message."""
        if supervisor is None:
            return RouteDecision(
                route="find_courses_bootcamps",
                search_query=message,
                reason="supervisor_unavailable_default_discovery",
            ).model_dump(mode="json")
        return supervisor.classify(message).model_dump(mode="json")

    @task(retry_policy=NETWORK_RETRY, timeout=120)
    def discover(query: str, attempt: int, refining: bool) -> dict[str, Any]:
        """Gather candidate pages, letting the model drive when a chat model exists."""
        pages: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        if chat_model is not None:
            from opportunity_sentinel.agent_tools import run_discovery_agent

            pages, observations, _final = run_discovery_agent(
                chat_model, discovery_agent.tools, query
            )
        if not pages:
            # Free-tier models sometimes end a turn without calling a tool. The
            # deterministic collector guarantees the pipeline still produces evidence.
            pages, observations = discovery_agent.discover(query)
        successful = [item["tool"] for item in observations if item["success"]]
        trace = ReasoningStep(
            decision=(
                "Search again for the missing evidence using first-party sources"
                if refining
                else "Discover current opportunities using first-party tools before web fallback"
            ),
            action=" -> ".join(successful) or "search_web",
            observation=f"Retrieved {len(pages)} candidate pages",
            attempt=attempt,
        )
        logger.info("workflow_task", task="discover", attempt=attempt, pages=len(pages))
        return {
            "pages": pages,
            "observations": observations,
            "trace": trace.model_dump(mode="json"),
        }

    @task
    def sanitize(pages: list[dict[str, Any]]) -> dict[str, Any]:
        """Drop any page carrying an indirect prompt-injection payload."""
        safe = []
        for page in pages:
            if scan_untrusted_content(page["content"]).safe:
                safe.append(page)
            else:
                logger.warning(
                    "security_guardrail",
                    attack="indirect_prompt_injection",
                    blocked=True,
                    source_url=page.get("url"),
                )
        return {"pages": safe, "blocked": len(pages) - len(safe)}

    @task(retry_policy=LLM_RETRY)
    def extract(pages: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the first page that yields a structured candidate."""
        for index, page in enumerate(pages):
            candidate = discovery_agent.extract(page)
            if candidate:
                message = AgentMessage(
                    sender="DiscoveryAgent",
                    recipient="VerificationAgent",
                    message_type="candidate_for_independent_review",
                    payload={
                        "source_url": str(candidate.source_url),
                        "evidence_count": len(candidate.evidence),
                    },
                )
                return {
                    "candidate": candidate.model_dump(mode="json"),
                    "remaining": pages[index + 1 :],
                    "message": message.model_dump(mode="json"),
                }
        message = AgentMessage(
            sender="DiscoveryAgent",
            recipient="Coordinator",
            message_type="candidate_extraction_failed",
            payload={"remaining_pages": 0},
        )
        return {"candidate": None, "remaining": [], "message": message.model_dump(mode="json")}

    @task
    def verify(candidate: dict[str, Any] | None) -> dict[str, Any]:
        """Evaluate the candidate. This is the evaluator half of the loop."""
        if not candidate:
            report = VerificationReport(
                status=VerificationStatus.NEEDS_RESEARCH,
                score=0,
                missing_fields=["extractable_candidate"],
                reasons=["candidate_extraction_failed"],
            )
        else:
            report = verification_agent.verify(OpportunityCandidate.model_validate(candidate))
        message = AgentMessage(
            sender="VerificationAgent",
            recipient="WorkflowCoordinator",
            message_type="verification_report",
            payload={
                "status": report.status.value,
                "score": report.score,
                "missing_fields": report.missing_fields,
            },
        )
        logger.info("workflow_task", task="verify", status=report.status.value)
        return {
            "verification": report.model_dump(mode="json"),
            "message": message.model_dump(mode="json"),
        }

    @task
    def check_eligibility(
        candidate: dict[str, Any], profile: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not profile:
            return EligibilityDecision(eligible=True).model_dump(mode="json")
        decision = eligibility_matcher.match(
            OpportunityCandidate.model_validate(candidate),
            StudentProfile.model_validate(profile),
        )
        return decision.model_dump(mode="json")

    @task(retry_policy=LLM_RETRY)
    def answer_from_knowledge(question: str) -> dict[str, Any]:
        """Answer from the retrieval corpus instead of a live search."""
        if knowledge_answerer is None:
            return {
                "answer": "قاعدة المعرفة غير متاحة حاليًا.",
                "citations": [],
                "supported": False,
            }
        return knowledge_answerer.answer(question).model_dump(mode="json")

    @entrypoint(checkpointer=checkpointer, store=store)
    def opportunity_workflow(request: dict[str, Any], *, runtime) -> dict[str, Any]:
        message = request.get("message") or request.get("search_query", "")
        telegram_id = request.get("telegram_id") or (
            (request.get("student_profile") or {}).get("telegram_id")
        )
        memory_ns = (MEMORY_NAMESPACE, str(telegram_id)) if telegram_id else None

        decision = RouteDecision.model_validate(route_request(message).result())

        # Long-term memory is read on every run and is deliberately keyed by student,
        # not by thread, so a fact learned in one conversation applies to the next.
        remembered: list[str] = []
        if memory_ns is not None and runtime.store is not None:
            existing = runtime.store.get(memory_ns, MEMORY_KEY)
            if existing:
                remembered = list(existing.value.get("facts", []))

        result: dict[str, Any] = {
            "thread_id": request.get("thread_id"),
            "route": decision.route,
            "remembered_facts": remembered,
            "observations": [],
            "reasoning_trace": [],
            "agent_messages": [],
            "errors": [],
            "verified_candidates": [],
            "search_attempts": 0,
            "final_status": None,
            "human_decision": None,
        }

        if decision.route == "update_profile":
            merged = sorted(set(remembered) | set(decision.extracted_facts))
            if memory_ns is not None and runtime.store is not None:
                runtime.store.put(memory_ns, MEMORY_KEY, {"facts": merged})
            logger.info("long_term_memory_write", facts=len(merged))
            result.update(
                {
                    "final_status": "profile_updated",
                    "remembered_facts": merged,
                    "stored_facts": decision.extracted_facts,
                }
            )
            return result

        if decision.route == "ask_knowledge":
            answer = answer_from_knowledge(message).result()
            result.update({"final_status": "answered", "answer": answer})
            return result

        if decision.route not in DISCOVERY_ROUTES:  # defensive: unknown route
            result["errors"].append(f"unknown_route:{decision.route}")
            result["final_status"] = VerificationStatus.REJECTED.value
            return result

        profile = request.get("student_profile")
        pages: list[dict[str, Any]] = []
        candidate: dict[str, Any] | None = None
        report: dict[str, Any] | None = None
        attempts = 0
        ineligible_seen = False

        # --- Evaluator-Optimizer loop -------------------------------------------------
        while attempts < max_research_attempts:
            attempts += 1
            missing = (report or {}).get("missing_fields") if report else None
            found = discover(refine_query(decision, missing), attempts, bool(report)).result()
            result["observations"].extend(found["observations"])
            result["reasoning_trace"].append(found["trace"])

            cleaned = sanitize(found["pages"]).result()
            if cleaned["blocked"] and not cleaned["pages"]:
                result["errors"].append("prompt_injection_blocked")
                result["final_status"] = VerificationStatus.REJECTED.value
                result["search_attempts"] = attempts
                return result
            pages = cleaned["pages"]
            if not pages:
                result["errors"].append("no_candidate_pages")
                continue

            # Walk the ranked pages, keeping every candidate that verifies and is
            # eligible, until the batch is full or the pages run out.
            while pages and len(result["verified_candidates"]) < max_results:
                extracted = extract(pages).result()
                result["agent_messages"].append(extracted["message"])
                candidate = extracted["candidate"]
                pages = extracted["remaining"]

                checked = verify(candidate).result()
                report = checked["verification"]
                result["agent_messages"].append(checked["message"])
                if report["status"] != VerificationStatus.VERIFIED.value:
                    break

                eligibility = check_eligibility(candidate, profile).result()
                result["eligibility"] = eligibility
                if eligibility["eligible"]:
                    result["verified_candidates"].append(
                        {
                            "candidate": candidate,
                            "verification": report,
                            "eligibility": eligibility,
                        }
                    )
                else:
                    ineligible_seen = True

            if result["verified_candidates"]:
                break
            if report and report["status"] == VerificationStatus.REJECTED.value:
                break

        result["search_attempts"] = attempts
        result["candidate"] = candidate
        result["verification"] = report

        if result["verified_candidates"]:
            latest = result["verified_candidates"][-1]
            result.update(latest)
            result["final_status"] = VerificationStatus.VERIFIED.value
            return result

        if ineligible_seen:
            # Eligibility is deterministic, so a human has nothing to weigh in on: the
            # opportunity verified but this student does not qualify.
            result["final_status"] = VerificationStatus.REJECTED.value
            return result

        # --- Human-in-the-loop --------------------------------------------------------
        # Nothing cleared the bar automatically, so a person decides before anything is
        # published or discarded. The run pauses here and survives a process restart.
        response = interrupt(
            {
                "kind": "opportunity_review",
                "candidate": candidate,
                "verification": report,
                "allowed_decisions": ["approve", "reject", "research_again"],
            }
        )
        answer = response.get("decision") if isinstance(response, dict) else response
        decision_text = str(answer)
        logger.info("human_review_resumed", decision=decision_text)
        result["human_decision"] = decision_text

        if decision_text == "approve" and candidate:
            eligibility = check_eligibility(candidate, profile).result()
            result["eligibility"] = eligibility
            if eligibility["eligible"]:
                result["verified_candidates"].append(
                    {
                        "candidate": candidate,
                        "verification": report,
                        "eligibility": eligibility,
                    }
                )
                result["final_status"] = VerificationStatus.VERIFIED.value
                return result
        result["final_status"] = VerificationStatus.REJECTED.value
        return result

    return opportunity_workflow


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def build_store(path: Path):
    """Open the durable long-term memory store used outside tests.

    Takes a connection directly rather than the ``from_conn_string`` context manager,
    because the bot process keeps one store open for its whole lifetime.
    """
    from langgraph.store.sqlite import SqliteStore

    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,  # the store manages its own transactions
    )
    store = SqliteStore(connection)
    store.setup()
    return store
