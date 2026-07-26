# 14 — Run the local worker safely on macOS

> GitHub issue: [#15](https://github.com/example-org/job-agent/issues/15)
> Status: `ready-for-agent`
> Parent: [#1](https://github.com/example-org/job-agent/issues/1)

## Parent

#1

## What to build

Operate interactive and sensitive workflow capabilities on the always-on Mac with automatic login startup, durable recovery, explicit health and stop controls, redacted logs, and a clear boundary from optional remote discovery.

## Acceptance criteria

- [ ] The local worker starts automatically at macOS login and recovers persisted work without repeating external actions.
- [ ] Telegram Pausa durably stops scheduled fetches, queued work, browser actions, and pending automation.
- [ ] Riprendi resumes only work that remains valid, while Stato reports health and current applications.
- [ ] The worker exposes an explicit local stop mechanism and useful redacted logs.
- [ ] Credentials, sensitive answers, browser state, and protected reports never move to the remote discovery runtime.
- [ ] Remote discovery can hand off versioned, non-sensitive shortlist and grading records to the local worker.
- [ ] The local worker can retrieve the official vacancy for `needs_local_fetch` records and resume the same deep-grading contract without grading an email snippet.
- [ ] After `Prepara candidatura`, GitHub Actions may generate professional CV and cover-letter artifacts from the exact authoritative grading snapshot, but uploads only an authenticated encrypted package.
- [ ] The Mac records prior matching artifact IDs, downloads only a newly produced package, authenticates and decrypts it from a Keychain-held key, verifies identity and hashes, and enables `Compila` only after successful installation.
- [ ] Keychain, browser work, ATS answers, sensitive persistence, and submission remain local and gated by Telegram approvals.
- [ ] Crashes and restarts preserve application state, approval scope, and submission idempotency.
- [ ] Operational errors do not expose tokens, health data, demographic answers, or identity documents.

## Blocked by

- #10 — Pause for intervention and recover uncertain outcomes
- #12 — Monitor career Gmail and classify application correspondence
- #13 — Schedule discovery, digests, and urgent alerts
- #14 — Expand watchlists and job alerts with approval
