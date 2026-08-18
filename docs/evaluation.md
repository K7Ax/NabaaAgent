# Evaluation plan and captured evidence

## Capstone acceptance criteria

Each rubric section maps to a file, a test and a notebook cell in
[`rubric-map.md`](rubric-map.md). This table records how the claim is *proved*.

| Rubric area | Automated or captured proof |
|---|---|
| Agentic reasoning and tools | The model binds three `@tool`s and chooses which to call; the executed ReAct trace records decision, action and observation. `tests/test_agent_tools.py` replays a scripted tool call offline. |
| Orchestration | The pipeline is built with the LangGraph Functional API — `@task` steps under one `@entrypoint`, with the re-search loop written as ordinary Python. No `StateGraph` remains in `src/`. |
| Multi-agent routing | An LLM supervisor classifies each message into one of six routes with `with_structured_output`; Discovery and Verification stay separate agents exchanging structured `AgentMessage` records. |
| Retrieval | `rag.py` loads the shipped guides plus verified opportunities, splits, embeds, indexes in FAISS and answers with citations; `tests/test_rag.py` runs the whole path on a deterministic offline embedding. |
| Security and observability | The injection test is blocked; JSON logs capture routing, tool latency, provider fallback, retries and human decisions; LangSmith traces every run when `LANGCHAIN_TRACING_V2` is set. |
| Persistence and HITL | The demo pauses at `interrupt()`, rebuilds the workflow from the SQLite checkpoint, then resumes with `Command(resume=...)` on the same thread. A separate Store proves a fact written in one thread is readable in another. |
| Cloud artifact | Dockerfile, Compose API service, persistent volume, health/readiness endpoints, and bot service. |

## Product quality metrics

The evaluation dataset will label opportunities as open/expired, Riyadh/remote/outside
scope, official/untrusted, duplicate/unique, and eligible/ineligible. We measure:

- Publication precision: verified opportunities that are truly valid.
- Discovery recall against a fixed monitored-source list.
- Field extraction accuracy for organization, city, deadline, majors, and URLs.
- Evidence coverage: published fields backed by a quote and source URL.
- Eligibility precision by major and graduation year.
- Duplicate rate after URL normalization.
- Time from source publication to student notification.
- Tool success rate, end-to-end latency, LLM calls, and provider fallback rate.

## Reproduce evidence

```powershell
.venv\Scripts\python -m pytest `
  --junitxml=artifacts\pytest-results.xml `
  --cov=opportunity_sentinel `
  --cov-report=xml:artifacts\coverage.xml
.venv\Scripts\python scripts\capstone_demo.py
.venv\Scripts\python scripts\live_smoke_test.py
.venv\Scripts\python scripts\live_workflow_test.py
```

The repository keeps the XML test report, coverage report, deterministic capstone
evidence, and secret-safe live connectivity evidence so the evaluator can inspect actual
execution rather than code claims.
