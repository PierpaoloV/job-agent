# Telegram callback gateway

This Cloudflare Worker is the always-on public boundary between Telegram
inline-button callbacks and the private GitHub Actions workflow.

It exposes:

- `POST /v1/authorizations` for GitHub Actions to mint short-lived,
  exact-role callback capabilities.
- `POST /telegram` for Telegram webhook updates.
- `GET /health` for non-sensitive health checks.

D1 stores callback capabilities and Telegram update IDs so one click can
produce at most one `telegram-opportunity-decision` repository dispatch.
`👎` first opens a force-reply question. Its lifecycle exists in D1 before the
prompt is sent, and the owner's exact reply is stored in D1 before GitHub
dispatch.
An uncertain GitHub transport outcome is retained for manual reconciliation;
it is never retried automatically.

Secrets are Worker bindings, never committed:

- `INTERNAL_API_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_BOT_TOKEN`
- `GITHUB_DISPATCH_TOKEN`

Create the real `wrangler.jsonc` from `wrangler.jsonc.example` after
`wrangler d1 create job-agent-telegram-gateway`, apply the migrations, set
the secrets, deploy, and finally register `/telegram` with Telegram using
the same webhook secret.
