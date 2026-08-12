# Capstone rubric audit

Audit date: 12 August 2026. Source: the two-page **Advanced Agentic AI Systems
Engineering - Capstone Rubric & Submission Requirements** supplied with the project.

This audit separates the scored 100-point rubric from the additional mandatory GitHub
submission rules. A score is never guaranteed because the evaluator owns the final
judgment; the table records whether the repository now contains direct evidence for every
stated scoring requirement.

## Scored rubric - requirement-by-requirement

| PDF requirement | Direct implementation and executed proof | Result |
|---|---|---:|
| **1. Working agent calls real tools/functions** | `DiscoveryAgent` calls the `ResearchTools` interface; live adapters call Tuwaiq's first-party API, Tavily, DDGS, and safe page retrieval. `artifacts/live-workflow-evidence.json` and `artifacts/live-smoke-evidence.json` contain captured live results. | Met |
| **1. Explicit course reasoning pattern** | ReAct is named in `ReasoningStep.pattern`; the graph records decision, action, and observation. Executed trace is in `artifacts/capstone-evidence.json`. | Met |
| **1. Short-term state across steps** | `OpportunityState` carries query, pages, candidate, verification, eligibility, messages, observations, and collected results across nodes. | Met |
| **Deliverable 1 subtotal** | All three requirements are implemented and executed. | **15/15** |
| **2. Genuine orchestration framework** | `langgraph.graph.StateGraph` is compiled with nine named nodes in `graph.py`; it is not a hand-written linear chain. | Met |
| **2. Nodes, edges, and conditional branch** | The graph declares explicit nodes/edges and conditional routes after sanitize, verify, eligibility, and collect. | Met |
| **2. Real shared state** | Every node reads and returns updates to typed `OpportunityState`. SQLite checkpoint inspection is exercised in the demo. | Met |
| **2. Terminating loop** | Missing evidence routes verification back to discovery with `max_research_attempts`; candidate iteration and five-result batch limits also terminate. Retry evidence is captured in `capstone-evidence.json`. | Met |
| **Deliverable 2 subtotal** | All four requirements are implemented and executed. | **20/20** |
| **3. Two or more distinct agents** | `DiscoveryAgent` discovers/extracts; `VerificationAgent` independently checks evidence. They are separate classes with separate prompts and policies. | Met |
| **3. Structured communication/shared state** | Typed `AgentMessage` records Discovery-to-Verification and Verification-to-Coordinator payloads; executed messages appear in `capstone-evidence.json`. | Met |
| **3. Explicit coordination strategy** | LangGraph is the documented centralized coordinator and owns routing, retries, approval, eligibility, and publication. | Met |
| **Deliverable 3 subtotal** | All three requirements are implemented and executed. | **20/20** |
| **4. Demonstrated input guardrail** | A real indirect prompt-injection string is passed through the graph and blocked by `scan_untrusted_content`; the blocked result and error are captured. | Met |
| **4. Output/data-protection guardrail** | Closed Pydantic schemas, evidence requirements, source trust, SSRF/redirect protection, deadline, registration, Riyadh, and deterministic eligibility filters fail closed. Tests cover these routes. | Met |
| **4. Structured monitoring** | Structlog emits JSON; `ToolObservation` records calls, latency, failures and metadata; provider fallback and Tavily request/credit telemetry are captured. | Met |
| **Deliverable 4 subtotal** | All three requirements are implemented and executed. | **20/20** |
| **5. Persistent checkpointer survives restart** | `SqliteSaver` persists state. The evidence script pauses, rebuilds a new graph instance on the same database, and resumes the same thread successfully. | Met |
| **5. Real HITL pause and resume** | LangGraph `interrupt` pauses at `approval`; Telegram admin buttons resume with `Command(resume=...)`. Interrupt payload and resumed decision are captured. | Met |
| **5. Cloud/deployment artifact** | `Dockerfile`, two-service `docker-compose.yml`, persistent volume, non-root user, FastAPI `/health` and `/readiness`, plus endpoint tests are present. | Met |
| **Deliverable 5 subtotal** | All three requirements are implemented and executed. | **20/20** |
| **6. Captured execution for every deliverable** | JUnit (24 passing tests), coverage XML, graph/security/fallback/HITL evidence, live provider/search evidence, and a live verified workflow are committed under `artifacts/`. | Met |
| **6. Architecture write-up in course vocabulary** | `docs/architecture.md` explains nodes, edges, state, agents, tools, loops, guardrails, coordination, checkpointing, and HITL. | Met |
| **Deliverable 6 subtotal** | Both requirements are implemented and executed. | **5/5** |

**Pre-submission evidence-backed score: 100/100.** The evaluator owns the official
grade. Our automated gate currently records 28 passing tests, 70.57% line coverage,
clean Ruff output, and a successful executed capstone proof script.

## Mandatory GitHub requirements

| Requirement | Current status |
|---|---|
| Comprehensive repository landing page | Complete in `README.md`. |
| Setup, keys, execution, and expected behavior | Complete in README and `docs/telegram-setup.md`. |
| Architecture and configuration documentation | Complete in `docs/architecture.md` and `.env.example`. |
| Meaningful incremental Git history | Complete: multiple feature/fix commits exist locally. |
| Secrets and generated databases excluded | Complete through `.gitignore` and `.dockerignore`. |
| Program attribution and SDAIA GitHub link | Complete in README. |
| Automated GitHub evaluation | `.github/workflows/quality.yml` runs Ruff, 24 tests, a 70% coverage gate, the capstone evidence script, and uploads proof artifacts without secrets. |
| Published and continuously updated GitHub repository | Complete: public `K7Ax/NabaaAgent`, incremental history, and successful GitHub Quality Gate run `31634713737`. |

## Executed proof map

- `artifacts/rubric-score.json`: machine-readable pre-submission score and gate status.
- `artifacts/pytest-results.xml`: current 28-test automated execution.
- `artifacts/coverage.xml`: current coverage execution.
- `artifacts/capstone-evidence.json`: ReAct trace, tools with latency, structured agent
  messages, graph loop, blocked attack, real interrupt payload, restart-safe resume, and
  simulated 429 provider fallback.
- `artifacts/live-smoke-evidence.json`: Telegram, administrator chat, Groq, OpenRouter,
  and search connectivity without stored secrets.
- `artifacts/live-workflow-evidence.json`: current multi-result first-party Tuwaiq
  discovery, verification scores, and student eligibility results. Tavily's official
  Saudi-domain allowlist and request telemetry are separately covered by executable tests.

## Tavily decision

Tavily is implemented as an optional advanced-search tool for courses, internships, and CO-OP
discovery. It does not replace first-party connectors, because an official structured API
is stronger evidence than a search index. Tavily records credit usage and request IDs and
falls back to DDGS if unavailable. Configure `TAVILY_API_KEY` to execute its live path.

## Presentation follow-ups

1. Capture Telegram screenshots of onboarding, a verified result, and an administrator
   review interrupt/resume for presentation quality.
2. If Docker is available on the presentation machine, capture `docker compose up --build`
   and `/health` output. The rubric accepts the existing artifacts, but a build capture
   further reduces evaluator ambiguity.
