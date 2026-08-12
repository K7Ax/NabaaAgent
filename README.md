# Opportunity Sentinel

Opportunity Sentinel is a capstone project for the **Advanced Agentic AI Systems
Engineering** training program. It discovers technical student opportunities in Riyadh,
verifies claims against evidence, checks student eligibility, and delivers only reviewed
results through a button-driven Telegram bot.

The system is intentionally evidence-first: an LLM may propose or extract facts, but
deterministic Python rules and cited source evidence decide whether an opportunity can be
published.

## Current milestone

The first milestone provides:

- Pydantic domain models for opportunities and field-level evidence.
- Separate discovery and verification agents communicating through shared graph state.
- A real LangGraph `StateGraph` with conditional routing and a bounded re-search loop.
- Prompt-injection scanning and structured output validation.
- A human approval interrupt for uncertain opportunities.
- SQLite checkpoint persistence and a FastAPI health endpoint.
- Automated tests for success, rejection, retry, security, and HITL paths.

Live web search, LLM providers, scheduled collection, and Telegram delivery are added in
later milestones after the safety and evaluation baseline is stable.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\python -m pytest
.venv\Scripts\python -m uvicorn opportunity_sentinel.api:app --reload
```

Never commit `.env` or API keys.

## Architecture vocabulary

- **Agents:** Discovery Agent and Verification Agent have distinct responsibilities.
- **Tools:** Source search, page retrieval, URL checks, and persistence are explicit tools.
- **State:** `OpportunityState` is shared and updated by graph nodes.
- **Nodes and edges:** Discovery, sanitization, extraction, verification, human review,
  publish, and reject are graph nodes connected by conditional edges.
- **Reasoning pattern:** The discovery flow uses bounded ReAct-style
  Thought -> Action -> Observation cycles.

## Training attribution

Built for the Advanced Agentic AI Systems Engineering program, August 2026 cohort.
See [SDAIA Academy on GitHub](https://github.com/SDAIAAcademy).

