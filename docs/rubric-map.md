# Rubric map

**Student:** Khalid Al-Zahem · **Programme:** SDAIA Academy — Agentic AI Systems, August
2026 · **Declared track:** Track A · **Academy:** https://github.com/SDAIAAcademy

Every claim below points at code you can read, a test that runs in CI, and a notebook cell
whose output is committed. Section numbers follow the rubric.

| § | Requirement | Implementation | Test | Notebook |
|---|---|---|---|---|
| **1** | Agent calls tools the model chose; `with_structured_output` + Pydantic | [`agent_tools.py`](../src/opportunity_sentinel/agent_tools.py) — three `@tool`s over the production research tools; [`chat_models.py`](../src/opportunity_sentinel/chat_models.py) — `build_structured()` applies `with_structured_output(schema, method="json_schema")` | `tests/test_agent_tools.py`, `tests/test_chat_models.py` | §1 — printed `tool_calls`; a validated `GroundedAnswer` |
| **2** | LLM supervisor routing with structured output (not keywords) | [`supervisor.py`](../src/opportunity_sentinel/supervisor.py) — `RouteDecision.route` is a `Literal` over six specialists | `tests/test_supervisor.py`, `tests/test_workflow.py::test_the_supervisor_decides_the_route_for_each_message` | §2 — five Arabic messages, five different routes |
| **3** | Load → split → embed → store → retrieve + written justification | [`rag.py`](../src/opportunity_sentinel/rag.py); corpus in [`docs/knowledge/`](knowledge) | `tests/test_rag.py` (12 tests, real FAISS, offline embedding) | §3 — each stage printed; grounded answer; refusal; **Hybrid** justification |
| **4** | Checkpointer with `thread_id` **and** a separate Store, with a cross-thread test | [`workflow.py`](../src/opportunity_sentinel/workflow.py) — `SqliteSaver` + `build_store()` (`SqliteStore`), namespace `("student_facts", telegram_id)` | `tests/test_workflow_store.py` — 5 tests incl. `test_a_fact_written_in_one_thread_is_readable_from_another` | §4 — write in `mem-a`, read in `mem-b`; thread state stays separate |
| **5** | `interrupt()` **and** `Command(resume=...)` both demonstrated | [`workflow.py`](../src/opportunity_sentinel/workflow.py) `interrupt({...})`; resumed by the admin flow in [`telegram_bot.py`](../src/opportunity_sentinel/telegram_bot.py) | `tests/test_workflow.py` — pause/resume and resume-after-rebuild | §5 — interrupt payload printed, then resumed by a *different* workflow object |
| **6** | Functional API (`@task`/`@entrypoint`) + ≥2 error strategies with a real `RetryPolicy` | [`workflow.py`](../src/opportunity_sentinel/workflow.py) — 7 `@task`s, 1 `@entrypoint`, `NETWORK_RETRY` / `LLM_RETRY`, `.with_fallbacks()`, fail-closed sanitising | `tests/test_workflow.py` | §6 — decorators listed, `StateGraph` count = 0, retry predicate table, a flaky task retried 3×, fallback firing, injection blocked |
| **7** | Implement a named workflow pattern and name it | **Evaluator-Optimizer** — generator `discover`/`extract`, evaluator `verify`, optimizer `refine_query(decision, missing_fields)` | `tests/test_workflow.py::test_missing_evidence_researches_then_interrupts_and_resumes` | §7 — both queries printed; the refined one verifies |
| **8** | `LANGCHAIN_TRACING_V2` + what the trace showed | [`config.configure_tracing`](../src/opportunity_sentinel/config.py), [`.env.example`](../.env.example) | `tests/test_config.py` | §8 — traces uploaded and queried back from the LangSmith API, six findings written from them |

## Submission requirements

| Requirement | Where |
|---|---|
| Full name in README and notebook header | [README](../README.md) capstone table; notebook cell 1 |
| Programme name + cohort dates | "SDAIA Academy — Agentic AI Systems, August 2026" — README and notebook |
| Declared track stated explicitly | **Track A** — README and notebook |
| Link to SDAIA Academy's GitHub | https://github.com/SDAIAAcademy — README and notebook |
| Professional README with how to run | [README](../README.md) |
| Technical documentation | [`architecture.md`](architecture.md), [`evaluation.md`](evaluation.md), [`capstone-writeup.md`](capstone-writeup.md), this file |
| `.gitignore` excludes secrets and generated files | [`.gitignore`](../.gitignore) — `.env`, `*.sqlite*`, `artifacts/`, caches |
| No API key in the code or in git history | Keys only ever come from the environment via [`config.py`](../src/opportunity_sentinel/config.py); `.env` has never been tracked |
| Notebook restarted and run top to bottom | Every code cell carries a sequential `execution_count` and captured output |
| No TODO or placeholder text | — |

## Honest limitations

These are stated here rather than discovered by a grader:

- **The LangSmith project is private.** §8 uploads real traces and queries them back out
  of the API, printing what the platform stored, and both trace URLs are in the notebook
  output — but a grader without access to that workspace will see the printed run tables
  rather than the LangSmith UI.
- **The batch collector still uses hand-rolled JSON extraction.**
  [`scripts/scheduled_job.py`](../scripts/scheduled_job.py) and
  [`llm.py`](../src/opportunity_sentinel/llm.py) predate the LangChain layer and run in
  spawned subprocesses where a small import surface matters. The *graded* pipeline is the
  Functional API workflow; the legacy path is production plumbing that has not been
  migrated.
- **Retrieval embeddings are local.** `sentence-transformers` pulls in torch, so the
  free-tier deployment does not install it and answers "knowledge base unavailable" rather
  than failing to boot. RAG runs fully in the notebook and in local development.
- **The notebook's demo pipelines use in-memory research tools** for the deterministic
  sections (§4–§7) so the output is reproducible; §1–§3 and §8 call the live model.
