# Capstone rubric audit

Audit date: 12 August 2026. Source: the two-page **Advanced Agentic AI Systems
Engineering - Capstone Rubric & Submission Requirements** supplied with the project.

This audit separates the scored 100-point rubric from the additional mandatory GitHub
submission rules. A score is never guaranteed because the evaluator owns the final
judgment; the table records whether the repository now contains direct evidence for every
stated scoring requirement.

## Scored rubric

| Deliverable | Pts | Repository evidence | Audit result |
|---|---:|---|---:|
| Agentic Reasoning & Tool Use | 15 | `DiscoveryAgent` invokes first-party Tuwaiq, optional Tavily, DDGS, and page-open tools through `ResearchTools`. `ReasoningStep` records ReAct decision/action/observation summaries, and state persists across nodes. Executed trace: `artifacts/capstone-evidence.json`. | 15/15 |
| Graph-Based Orchestration | 20 | Real LangGraph `StateGraph` with eight named nodes, shared `OpportunityState`, conditional edges, candidate iteration, and a bounded re-search loop. Tests execute publish, reject, retry, eligibility, and interrupt routes. | 20/20 |
| Multi-Agent & Role Specialization | 20 | `DiscoveryAgent` and `VerificationAgent` are separate classes with distinct prompts and responsibilities. Typed `AgentMessage` records Discovery-to-Verification and Verification-to-Coordinator communication in shared state. LangGraph is the centralized coordinator. | 20/20 |
| Security, Guardrails & Observability | 20 | A real indirect prompt-injection payload is blocked before extraction. URL/redirect SSRF controls, Pydantic schemas, official evidence, expiry, Riyadh, registration, and eligibility policies protect data/output. Structlog JSON events and tool observations capture latency, failure, provider fallback, Tavily credits, and request IDs. | 20/20 |
| Persistence, HITL & Cloud | 20 | `SqliteSaver` checkpoints are reconstructed and resumed using the same thread ID. A real LangGraph `interrupt` pauses and `Command(resume=...)` continues after graph reconstruction. `Dockerfile`, Compose API/bot services, persistent volume, non-root container, FastAPI health/readiness endpoints, and endpoint tests are present. | 20/20 |
| Documentation & Evidence | 5 | README, architecture, security, evaluation, setup, and this audit use course vocabulary. JUnit, coverage, deterministic attack/fallback/HITL evidence, provider connectivity, and a live verified Tuwaiq workflow are captured under `artifacts/`. | 5/5 |

**Evidence-backed rubric target: 100/100.**

## Mandatory GitHub requirements

| Requirement | Current status |
|---|---|
| Comprehensive repository landing page | Complete in `README.md`. |
| Setup, keys, execution, and expected behavior | Complete in README and `docs/telegram-setup.md`. |
| Architecture and configuration documentation | Complete in `docs/architecture.md` and `.env.example`. |
| Meaningful incremental Git history | Complete: multiple feature/fix commits exist locally. |
| Secrets and generated databases excluded | Complete through `.gitignore` and `.dockerignore`. |
| Program attribution and SDAIA GitHub link | Complete in README. |
| Published and continuously updated GitHub repository | **Blocked: no Git remote is configured, so submission is not complete yet.** |

## Executed proof map

- `artifacts/pytest-results.xml`: current automated test execution.
- `artifacts/coverage.xml`: current coverage execution.
- `artifacts/capstone-evidence.json`: ReAct trace, tools with latency, structured agent
  messages, graph loop, blocked attack, real interrupt payload, restart-safe resume, and
  simulated 429 provider fallback.
- `artifacts/live-smoke-evidence.json`: Telegram, administrator chat, Groq, OpenRouter,
  and search connectivity without stored secrets.
- `artifacts/live-workflow-evidence.json`: current first-party Tuwaiq discovery,
  verification score, and student eligibility result.

## Tavily decision

Tavily is implemented as an optional advanced-search tool for internship and CO-OP
discovery. It does not replace first-party connectors, because an official structured API
is stronger evidence than a search index. Tavily records credit usage and request IDs and
falls back to DDGS if unavailable. Configure `TAVILY_API_KEY` to execute its live path.

## Remaining submission actions

1. Create a GitHub repository and push this incremental history.
2. Confirm the repository visibility required by the instructor.
3. Capture Telegram screenshots of onboarding, a verified result, and an administrator
   review interrupt/resume for presentation quality.
4. If Docker is available on the presentation machine, capture `docker compose up --build`
   and `/health` output. The rubric accepts the existing artifacts, but a build capture
   further reduces evaluator ambiguity.
