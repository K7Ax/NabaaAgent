# NabaaAgent production architecture

## Runtime boundary

When the production service is configured, it owns the durable database, Telegram webhook,
matching, lifecycle, and delivery queue. GitHub Actions is stateless and only performs
collection. The repository workflow is currently manual-only until that durable API and its
signed-ingestion secrets exist; local polling remains the development runtime.
All mutations sent by Actions use `HMAC-SHA256(timestamp + "." + body)` and are rejected
after five minutes. Telegram uses its independent webhook secret header.

## Opportunity lifecycle

`signal → needs_evidence → verified_open → closing_soon → expired`

Rejected and stale are terminal/withheld states. `opportunity_versions` retains every
material payload change. Evidence is additionally normalized into `evidence_records`.
Canonical application URL is the primary identity, with content hashing for versioning.

## Matching policy

Hard gates run before ranking: current status, category, major breadth, graduation year,
location/mode, and structured mandatory requirements. A missing mandatory profile fact
creates `needs_confirmation`. It never creates an eligible match.

Ranking weights are major 35, category 20, location 15, academic timing 10, skills 10,
and freshness/urgency 10. Scores of 80+ are immediate; 60–79 join the 18:00 Riyadh
digest. Closing opportunities can become immediate from 60.

## Cost controls

The half-hour collector calls first-party Tuwaiq, Future Skills, Misk, KSU News,
KSU Alumni Gate, and Financial Academy sources, plus public employer ATS APIs from Ashby,
Lever, and Greenhouse. Each employer board has an independent source-health record; the
curated set currently includes Sarj.ai, Lean, Trendyol, Infinite PL, Tamara, HALA, and TSMG.
If Tuwaiq's API is blocked, the collector uses recent registration posts from the academy's
official public channel. It uses no paid search or LLM provider.
Deep discovery runs seven queries per cycle
from a rotating matrix that covers all opportunity categories during the day. Before every
Tavily basic call, the collector asks the persistent API to reserve one credit.
At three cycles per day this is approximately 630 credits per 30-day month, leaving a
safety reserve under the 900-credit cap. The persistent counter refuses reservations
beyond the configured limit. LLM extraction results remain associated with source
content; deterministic structured adapters do not invoke an LLM.

## Operations

- `/health`: process liveness.
- `/readiness`: database, Telegram, webhook, provider configuration, and counts.
- `/internal/ingest/batch`: verified candidate ingestion boundary.
- `/internal/quota/reserve`: persistent provider budget gate.
- `/internal/revalidate`: deadline and official-link revalidation.
- `/internal/deliver`: throttled immediate and digest delivery.
- `/internal/metrics`: protected platform counters.

If an official application page is unreachable, delivery remains pending for retry. If
the page is closed, the opportunity expires and all pending deliveries are cancelled.
