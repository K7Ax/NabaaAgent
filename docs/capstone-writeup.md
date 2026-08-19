# Capstone write-up

**Student:** Khalid Al-Zahem
**Programme:** SDAIA Academy — Agentic AI Systems (cohort: August 2026)
**Declared track:** Track A
**Academy:** https://github.com/SDAIAAcademy
**Notebook with executed output:** [`capstone.ipynb`](../capstone.ipynb)
**Section → file → test → cell map:** [`rubric-map.md`](rubric-map.md)

---

## The problem

Saudi students miss opportunities they qualify for, not because the opportunities are
hidden but because they are scattered — Tuwaiq, Misk, SDAIA, university career pages, ATS
boards, ministry announcements — and because the aggregators that do collect them are
frequently wrong: closed programmes still listed as open, dead application links, invented
deadlines. A student who acts on a wrong listing loses more than time.

Nabaa (نبأ) is an Arabic Telegram platform that searches once for everyone, *verifies*
each opportunity against first-party evidence before publishing it, and then matches it to
each student deterministically. The verification step is the product. Everything in this
write-up exists to make it trustworthy.

---

## Section 1 — Agent fundamentals

The discovery agent is given three tools — `search_web`, `tuwaiq_catalog` and `open_page`
— as bound schemas, and the model decides which to call and with what arguments. The
notebook shows the raw `tool_calls` the model returned: given "ابحث لي عن معسكرات برمجة
مجانية في الرياض" it emitted `search_web({"query": "معسكر برمجة مجاني الرياض"})` — a query
it composed itself, not a template we filled in.

The tools are deliberately thin adapters over the research tools the production bot
already uses ([`agent_tools.py`](../src/opportunity_sentinel/agent_tools.py)), so the
Tavily monthly credit guard and the SSRF checks inside `open_page` still apply when the
*model* is the one calling them. An agent that can reach the network is an agent that can
be talked into reaching the wrong part of it; reusing the guarded implementation was
cheaper and safer than writing new tools for the demo.

Structured output is used wherever a model result feeds control flow.
`build_structured(settings, schema)` applies `with_structured_output(schema,
method="json_schema")` and returns validated Pydantic instances — the notebook prints
`type: GroundedAnswer`, `is pydantic: True`. Nothing downstream parses model text.

## Section 2 — Multi-agent system and routing

Routing was the clearest anti-pattern in the original codebase: a keyword branch that
checked whether a message contained "دورة" or "معسكر". It failed on exactly the messages
students actually send.

The supervisor ([`supervisor.py`](../src/opportunity_sentinel/supervisor.py)) is now an
LLM that returns a `RouteDecision` through structured output. Its `route` field is a
`Literal` over six specialists, so an invalid route cannot be constructed at all — the
schema, not a downstream `if`, is what makes routing total.

The notebook runs five deliberately awkward Arabic messages, none containing its route's
obvious keyword, and the model routes all five correctly:

| Message | Route |
|---|---|
| ودّي أطوّر نفسي في البرمجة هالصيف | `find_courses_bootcamps` |
| أبغى شي أشتغل فيه بعد التخرج | `find_jobs_internships` |
| فيه شي أشارك فيه مع فريق وأربح جوائز؟ | `find_hackathons_events` |
| هل شهادة طويق معترف فيها من وزارة التعليم؟ | `ask_knowledge` |
| أدرس أمن سيبراني وأتخرج ٢٠٢٧ | `update_profile` |

The last row is the one keyword matching could never do. "أدرس أمن سيبراني وأتخرج ٢٠٢٧"
contains no request at all; the supervisor recognised it as a durable fact and returned
`extracted_facts: ['major=cybersecurity', 'graduation_year=2027']`, which the workflow
wrote to long-term memory.

Discovery and Verification remain separate agents with separate prompts and
responsibilities, exchanging structured `AgentMessage` records. Verification is not
allowed to be the same agent that found the opportunity — an extractor asked to grade its
own extraction agrees with itself.

## Section 3 — RAG, and why Hybrid

**The choice: Hybrid.** The supervisor decides *whether* to retrieve — that is the agentic
half — and only `ask_knowledge` messages reach the retriever at all. Once inside,
retrieval is a fixed 2-Step retrieve-then-generate.

**Why not pure 2-Step.** It would retrieve on every message. "ابحث لي عن معسكر" has to be
answered from a live search of what is open *today*; a stored document would be both
wasted work and actively misleading, because a stale chunk would outrank a fresh page.

**Why not full Agentic RAG.** A model issuing retrieval calls in a loop buys almost no
recall over a single top-k search on a corpus this size — six policy documents plus the
verified rows — while spending tokens the free tier has to ration and making latency
unpredictable for someone waiting on a Telegram reply.

The pipeline is the standard five stages, and the notebook prints each:

1. **Load** — 6 Arabic guides from [`docs/knowledge/`](knowledge) plus every verified
   opportunity in the database. Shipping the guides means the demo never depends on the
   database already having rows.
2. **Split** — `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120)`, with
   the Markdown heading separator first in the list, so an FAQ question and its answer
   tend to stay in one chunk. 9 chunks, longest 780 characters.
3. **Embed** — `intfloat/multilingual-e5-small`. The corpus is Arabic, so
   `all-MiniLM-L6-v2` and every other English-only model was disqualified before quality
   entered into it. e5-small runs locally and costs nothing per query, which matters on
   free tiers.
4. **Store** — FAISS, 9 vectors.
5. **Retrieve** — top-4, then a grounded answer.

Asked "هل شهادة أكاديمية طويق معادلة من وزارة التعليم؟" the system retrieved the Tuwaiq
FAQ chunk and answered correctly that the certificate is *not* a Ministry-equivalent
academic qualification, citing `tuwaiq-faq`. Asked the price of a flight to Tokyo it
returned `supported: False` and said the knowledge base does not cover it.

That refusal is worth being precise about. A vector search always returns its top-k, so
retrieval alone cannot detect an off-corpus question — the model is instructed to set
`supported=false` when the context does not contain the answer, and `KnowledgeAnswerer`
additionally refuses outright when retrieval returns nothing at all. Fail closed, twice.

## Section 4 — Context and state

Two persistence layers with two different scopes, because they answer different questions.

**Short-term** is a `SqliteSaver` checkpointer keyed by `thread_id`. It holds one
conversation, including a run paused mid-review.

**Long-term** is a separate `Store`, keyed `("student_facts", telegram_id)` — by *student*,
not by thread. The notebook proves the distinction in both directions: a preference stated
in thread `mem-a` (`prefers_remote=true`) is read back in thread `mem-b`, a different
conversation entirely; while checkpoint state for `mem-a` is invisible to any other
thread. The same assertion runs in CI as
`tests/test_workflow_store.py::test_a_fact_written_in_one_thread_is_readable_from_another`.

Facts accumulate rather than overwrite, and are scoped per student — a fact written by one
Telegram user is never visible to another, which is a privacy property and not just a
correctness one.

## Section 5 — Human-in-the-loop

When nothing clears verification automatically, the run calls `interrupt()` with the
candidate, the verification report and the decisions a reviewer is allowed to make. It
does not guess, and it does not publish.

The notebook shows the interrupt payload — status `needs_research`, score 0.69, missing
`application_open_evidence` and `accepted_majors_or_technical_focus` — and then resumes
with `Command(resume={"decision": "approve"})`, after which the opportunity publishes.

The resume is deliberately performed by a **different workflow object** built over the
same checkpoint file, standing in for a restarted process. That is not a demo
convenience: on Render's free tier the service suspends after fifteen idle minutes, so a
review that could not survive a restart could not survive being sent to a human at all.
The Telegram admin flow resumes this same interrupt hours later.

Eligibility is handled the other way round. A verified opportunity that a specific student
does not qualify for is rejected deterministically without asking anyone — a human has
nothing to weigh in on when the rule is "your major is not in the accepted list", and
paging a person for it would train them to approve without reading.

## Section 6 — Functional API and error handling

The pipeline was a nine-node `StateGraph`. It is now `@task` + `@entrypoint` throughout,
and the conversion was complete rather than cosmetic: `graph.py` and its state schema were
deleted, and the notebook prints `StateGraph references anywhere in src/: 0`.

What used to be conditional edges is ordinary Python. The re-research loop is a `while`,
batch collection is a nested `while`, and the early rejections are `return` statements. The
one thing that genuinely changed shape is side effects: because the entrypoint body
replays from the top when a run resumes, every effect must live inside a `@task`, where
completed work is served from the checkpoint instead of re-executed. Counters live in the
entrypoint body, not in reducers.

Three error strategies:

1. **Retry with exponential backoff.** `NETWORK_RETRY` (3 attempts, 0.5 s, ×2, jitter) and
   `LLM_RETRY` (2 attempts, 1 s, ×3, jitter) are real `RetryPolicy` objects with a shared
   predicate: retry connection blips and 408/425/429/5xx, never retry a 4xx we caused, and
   never retry a `ValueError` — that is a bug, and retrying a bug just finds it three
   times. The notebook shows a task failing twice and succeeding on the third attempt.
2. **Provider fallback.** `build_chat_model()` returns
   `ChatGroq.with_fallbacks([ChatOpenAI(OpenRouter)])`, and `build_structured()` applies
   `with_structured_output` to each provider *before* chaining, so the fallback returns the
   same validated type as the primary. Separately, a free model that ends its turn without
   calling any tool falls back to the deterministic collector, so discovery still produces
   evidence.
3. **Fail closed.** A page carrying an indirect prompt-injection payload is dropped, not
   trusted; the notebook shows a run rejecting one and publishing nothing.

Retry policies are attached only where transient failure is possible — network and LLM
tasks. `sanitize`, `verify` and `check_eligibility` are pure; a retry there would mask a
defect rather than absorb a blip.

## Section 7 — Workflow pattern: Evaluator-Optimizer

**The pattern implemented is Evaluator-Optimizer.**

| Role | Component |
|---|---|
| Generator | `discover` → `extract` |
| Evaluator | `verify` — an independent agent that reports status, score, and *which fields lack evidence* |
| Optimizer | `refine_query(decision, missing_fields)` — the named gaps are appended to the query |
| Exit | verified, or attempts exhausted → human review |

The feedback signal is what makes this Evaluator-Optimizer rather than a retry loop. The
second attempt is not the same query again; it is a query shaped by the evaluator's
specific complaint. The notebook shows both queries, and the refined one — carrying
`official application_open_evidence accepted_majors_or_technical_focus` — is the one that
finds the fully-evidenced page, which then verifies.

The loop is bounded at two attempts on purpose. An unbounded refine loop on a free tier is
a way to spend a month of search credits on one stubborn query; two attempts and then a
human is the trade.

## Section 8 — LangSmith observability

Tracing is configured with `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY` and
`LANGCHAIN_PROJECT` (see [`.env.example`](../.env.example)). `configure_tracing()` exports
them into `os.environ` at startup, because the tracing client reads the process
environment and would otherwise never see values loaded from `.env` — and it refuses to
enable tracing when the flag is set but the key is missing, which would turn every model
call into a failed upload.

The notebook uploads a run, waits for it, then queries the LangSmith API by `trace_id`
and prints what the platform stored, so the numbers below are read back out of LangSmith
rather than measured locally. Both trace URLs are in the notebook output. Six things the
traces showed:

1. **The model call is the entire cost; the schema is free.** One routing decision is four
   runs — `RunnableWithFallbacks → RunnableSequence → ChatGroq → PydanticOutputParser`.
   In the trace LangSmith returned, `ChatGroq` took 865.6 ms and 705 tokens while
   `PydanticOutputParser` took 1.1 ms and 0 tokens. So economise on the *number* of model
   calls, never on the strictness of the schema. That is why routing happens once per message rather than once per candidate,
   and why verification stayed deterministic instead of becoming a second LLM judge.
2. **The fallback wrapper appears on the happy path.** `RunnableWithFallbacks` is the root
   of a *successful* call, which is how you confirm the chain is wired rather than merely
   configured. A fallback you only see during an outage is one you find out about during
   an outage.
3. **Retries are declared where they are visible.** Both chat clients set `max_retries=0`.
   Retries inside the client collapse into one `ChatGroq` run — three attempts and one
   attempt look identical — and they would compound with the task's own `RetryPolicy`.
   Keeping retry policy at the task level makes every attempt a run you can count.
4. **The Evaluator-Optimizer loop is legible.** A full run shows `discover`, `sanitize`,
   `extract` and `verify` each appearing twice under one `opportunity_workflow` run. A loop
   written as plain Python inside an `@entrypoint` is as observable as explicit graph
   edges.
5. **Token counts roll up the tree; latencies do not.** All three chain runs report the
   same 705 tokens as the `ChatGroq` run beneath them — usage is summed over the subtree,
   so a parent's token count is not its own work. Latency is the opposite:
   `RunnableWithFallbacks` at 875.0 ms wraps `ChatGroq` at 865.6 ms, and the ~9 ms
   difference is the wrapper itself. Reading a trace means knowing which numbers are
   inclusive.
6. **Latency is network, not logic.** Every offline task completed in under a millisecond
   and the whole twelve-run workflow in tens of milliseconds. All real latency is network
   and model time — which is the evidence behind putting retry policies on exactly those
   tasks and nowhere else.

---

## What I would do next

- **Move retrieval off local embeddings.** Computing embeddings in CI and storing vectors
  in Turso would let the deployed service answer knowledge questions without shipping
  torch.
- **Unify the admin review onto workflow interrupts.** The Telegram admin path and the
  workflow interrupt do the same job through two code paths.
- **Act on the circuit-breaker signal.** Source health is already recorded; degraded
  sources should be skipped rather than retried into their own timeout.
- **Migrate the batch collector.** `scheduled_job.py` still uses the hand-rolled JSON
  extractor. It works, but it is a second way of doing something the LangChain layer now
  does better.

## Honest limitations

Stated plainly rather than left to be discovered:

- The §8 trace URLs point at a private LangSmith project, so a grader without access to
  that workspace sees the run tables printed in the notebook rather than the platform UI.
- The legacy batch collector still parses JSON by hand; the graded pipeline does not.
- The deployed free-tier service does not install `sentence-transformers`, so it answers
  "knowledge base unavailable" for retrieval questions rather than failing to boot. RAG
  runs fully in the notebook and locally.
- Sections 4–7 of the notebook use in-memory research tools so their output is
  reproducible; sections 1–3 and 8 call the live model.
