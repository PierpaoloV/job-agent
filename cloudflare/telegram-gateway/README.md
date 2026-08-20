# Telegram callback gateway

This Cloudflare Worker is the always-on public boundary between Telegram
inline-button callbacks and the private GitHub Actions workflow.

It exposes:

- `POST /v1/authorizations` for GitHub Actions to mint short-lived,
  exact-role callback capabilities.
- `POST /v1/review-authorizations` to mint 24-hour, package-scoped
  `Approva`/`Rigenera` capabilities.
- `POST /v1/artifact-reviews/:id/messages` to bind the exact two protected
  PDF receipts immediately, then the review-control receipt before a decision
  is accepted.
- `POST /v1/artifact-reviews/:id/decision-ack` for Actions to confirm that the
  exact decision was persisted in the authoritative state artifact.
- `POST /v1/artifact-reviews/:id/dispatch-recovery` for an explicit operator
  retry only after the corresponding GitHub run is confirmed absent.
- `POST /telegram` for Telegram webhook updates.
- `GET /health` for non-sensitive health checks.

D1 stores callback capabilities and Telegram update IDs so one click can
produce at most one `telegram-opportunity-decision` repository dispatch.
When a role button has expired, the expired click produces no GitHub dispatch:
the Worker rotates all three controls on the same Telegram card and asks the
owner to press the intended action again.
`👎` first opens a force-reply question. Its lifecycle exists in D1 before the
prompt is sent, and the owner's exact reply is stored in D1 before GitHub
dispatch.
An uncertain GitHub transport outcome is retained for manual reconciliation;
it is never retried automatically. After confirming that no corresponding
GitHub run exists, use `python -m hosted_artifact_review recover-dispatch` with
the exact review identity and `--confirmed-absent`. A 204 repository dispatch
only moves the review to `dispatch_accepted`; `approved` or
`regenerate_requested` is recorded only after Actions publishes its state and
calls `decision-ack`.
Artifact-review callbacks delete both protected PDFs and their controls before
dispatching the exact package decision. A 15-minute cron sweep removes any
undecided review once its 24-hour window expires.

Secrets are Worker bindings, never committed:

- `INTERNAL_API_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_BOT_TOKEN`
- `GITHUB_DISPATCH_TOKEN`

Create the real `wrangler.jsonc` from `wrangler.jsonc.example` after
`wrangler d1 create job-agent-telegram-gateway`, apply the migrations, set
the secrets, deploy, and finally register `/telegram` with Telegram using
the same webhook secret.
