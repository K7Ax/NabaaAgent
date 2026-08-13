# Security and guardrails

## Threat model

Opportunity Sentinel reads arbitrary public web pages. Retrieved text is therefore
untrusted and may contain indirect prompt injection, malicious links, false deadlines,
or instructions intended to trigger tools. Telegram callback data may also be forged by
a user, and API keys must never enter prompts, logs, or Git.

## Enforced controls

- Only public HTTP(S) URLs are fetched; loopback and private addresses are blocked to
  reduce SSRF risk.
- Script, style, SVG, and hidden markup are removed before content enters the graph.
- Known English and Arabic prompt-injection patterns are detected before extraction.
- Retrieved text is explicitly delimited as untrusted data in both agent prompts.
- The Discovery Agent can only search and read. It cannot publish, execute code, or send
  Telegram messages.
- Pydantic validates every candidate and verification report.
- A verified result requires field-level evidence and at least one official Saudi
  government, educational, or organizational source; otherwise it is re-searched or
  escalated to a human.
- Expired opportunities are rejected; city and remote eligibility are matched against each profile.
- Eligibility is checked after verification and again after human approval.
- Research loops and page sizes are bounded.
- Admin review callbacks verify the configured Telegram administrator ID.
- Secrets live only in `.env`, which is excluded from Git and Docker build context.

## Demonstrated attack

`scripts/capstone_demo.py` feeds a page containing instructions to ignore prior rules,
reveal API keys, and mark itself verified. The input guardrail blocks it before
extraction. The resulting `prompt_injection_blocked` evidence is captured in
`artifacts/capstone-evidence.json`.

## Remaining production hardening

For a public deployment, use PostgreSQL-backed encrypted checkpoints, a managed secret
store, domain allowlists per source, outbound egress policies, encrypted backups, and a
dedicated content-safety classifier. These do not replace the controls already enforced
in the application.
