# Evaluation plan and captured evidence

## Capstone acceptance criteria

| Rubric area | Automated or captured proof |
|---|---|
| Agentic reasoning and tools | Discovery Agent invokes `search_web` and `open_page`; observations record latency and results. |
| Graph orchestration | StateGraph has eight named nodes, conditional edges, and a bounded re-search loop. |
| Multi-agent | Discovery and Verification are separate classes, prompts, responsibilities, and structured outputs connected by shared state. |
| Security and observability | Injection test is blocked; JSON logs capture node routing, tool latency, provider fallback, failures, and human decisions. |
| Persistence and HITL | Demo pauses at `interrupt`, rebuilds the graph from the SQLite checkpoint, then resumes with the same thread ID. |
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
```

The repository keeps the XML test report, coverage report, deterministic capstone
evidence, and secret-safe live connectivity evidence so the evaluator can inspect actual
execution rather than code claims.
