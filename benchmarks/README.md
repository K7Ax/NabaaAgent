# Nabaa coverage benchmark

`gold_opportunities.json` is intentionally independent from Nabaa's database. During the
weekly source audit, a reviewer opens each declared official source and records every open
technical opportunity. A source is marked `complete` only after its full official catalogue
and the application state of every in-scope item were reviewed. Blocked catalogues remain
explicitly `blocked`; they are never treated as a zero-opportunity source.

```json
{
  "application_url": "https://official.example/apply/123",
  "source_id": "official-source-id",
  "opportunity_type": "coop",
  "expected_open": true,
  "evidence_quote": "Applications are open until 30 November 2026",
  "reviewed_at": "2026-08-16"
}
```

Run `python scripts/coverage_report.py`. The report calculates recall only when the gold set
contains independently reviewed opportunities. For production, use
`python scripts/coverage_report.py --api-url $NABAA_API_URL`; the request is HMAC-signed with
`INTERNAL_API_SECRET`, and production returns only aggregate results plus missed public URLs.
The weekly GitHub workflow uploads this report without turning a product-quality gap into a
broken deployment. An empty set reports `not_measured`; inventory size is never presented as
recall.
