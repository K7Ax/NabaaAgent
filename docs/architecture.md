# Opportunity Sentinel architecture

## Decision principle

LLMs and agents may discover, interpret, and propose facts. Publication is controlled by
validated evidence and deterministic policy rules. Web content is always treated as
untrusted data, never as instructions.

## Graph

```mermaid
flowchart TD
    S([Start]) --> D[Discovery Agent]
    D --> G[Input guardrail]
    G -->|safe| E[Structured extraction]
    G -->|blocked| X[Reject]
    E --> V[Verification Agent]
    V -->|complete evidence| M[Eligibility Matcher]
    M -->|student matches| P[Publish]
    M -->|student does not match| X
    V -->|missing evidence and attempts remain| D
    V -->|uncertain or attempts exhausted| H[Human review interrupt]
    V -->|expired or out of scope| X
    H -->|approve| M
    H -->|research again| D
    H -->|reject| X
```

## Agent responsibilities

- **Discovery Agent:** Runs the bounded ReAct loop, invokes search/open tools, and extracts
  a typed candidate from untrusted sources.
- **Verification Agent:** Independently examines field-level evidence and returns a
  structured report with status, score, missing fields, conflicts, and reasons.
- **LangGraph coordinator:** Owns routing, bounded retries, shared state, checkpointing,
  and the human approval interrupt.

## Tool adapters

`WebResearchTools` performs live search, checks URLs against private-network/SSRF risks,
downloads source pages, and returns structured observations with latency and metadata.
For Tuwaiq programs it uses the academy's public first-party API and applies deterministic
validation to explicit structured fields, avoiding search-index staleness and unnecessary
LLM interpretation.
For sources without a first-party connector, optional Tavily advanced search supplies
ranked extracted content and records credit usage/request IDs. DDGS and safe page opening
remain the fallback, so Tavily is not a single point of failure.

The ReAct trace stores auditable `decision`, `action`, and `observation` summaries without
persisting private chain-of-thought. Discovery sends a structured candidate message to
Verification, which returns a structured verification message to the LangGraph coordinator.
`InMemoryResearchTools` implements the same interface for repeatable security and
evaluation runs. The discovery agent uses Groq first and falls back to OpenRouter when a
provider is unavailable or rate limited.

## Persistence and human review

The compiled graph uses a SQLite checkpointer. Every run has a stable `thread_id`. When an
uncertain opportunity reaches the approval node, LangGraph persists the state and pauses.
The process can later resume with `Command(resume=...)` using the same `thread_id`.

## Security baseline

- Detect indirect prompt-injection patterns in retrieved pages.
- Do not extract or publish blocked pages.
- Validate candidates and verification reports with Pydantic.
- Bound research attempts to prevent uncontrolled loops and token consumption.
- Keep provider and Telegram secrets in environment variables excluded by `.gitignore`.

## Delivery and application state

The Telegram adapter identifies users by their Telegram account ID. Onboarding, search,
profile editing, saving, applying, and human-review decisions use inline buttons. A
separate SQLite repository stores student profiles, verified opportunities, saved items,
and delivery records. URL-derived identifiers suppress duplicate notifications. A
scheduled loop repeats discovery for registered students and sends only unseen matches.
