# Nabaa coverage benchmark

`gold_opportunities.json` is intentionally independent from Nabaa's database. During the
weekly source audit, a reviewer opens each declared official source and records every open
technical opportunity using this shape:

```json
{
  "application_url": "https://official.example/apply/123",
  "source_id": "official-source-id",
  "opportunity_type": "coop",
  "expected_open": true
}
```

Run `python scripts/coverage_report.py`. The report calculates recall only when the gold set
contains independently reviewed opportunities. An empty set reports `not_measured`; it never
pretends that inventory size is recall.
