# Job Agent

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Job Agent reads job-alert emails, extracts and deduplicates job listings, applies
a deterministic no-LLM screen, and deep-grades only verified official vacancies.
It sends human-gated Telegram alerts and digests, and can prepare application
artifacts without automatically submitting anything.

The repository is designed so private credentials and resume context are supplied
at runtime through local ignored files or GitHub Actions secrets, not committed to
the public repo.

## What It Does

- Fetches job-alert emails from Gmail for LinkedIn, Indeed, Glassdoor, Welcome to
  the Jungle, EuroTechJobs, and compatible fallback sources.
- Parses listings into structured fields such as title, company, location, URL,
  snippet, salary, seniority, remote policy, and detected skills.
- Deduplicates seen jobs and persists authoritative discovery state across
  GitHub Actions runs.
- Screens locally without an LLM; only verified vacancies with an official
  description can cross the deep-grading boundary.
- Deep-grades eligible vacancies with OpenAI using a bounded professional
  profile and private runtime preferences.
- Sends immediate alerts for top-tier roles or imminent deadlines and a ranked
  digest every three days directly from GitHub Actions.
- Adds one-use, short-lived `👍`, `👎`, and `Dimmi di più` controls scoped to
  the configured Telegram user, chat, and exact vacancy. Cloudflare receives
  Telegram callbacks and dispatches their exact decisions to GitHub Actions.
- Applies user-configured language, ownership, and title exclusions from the
  ignored preferences file.
- Lets you mark applications locally with `scripts/mark_applied.py`.

## Repository Layout

```text
.
├── main.py                    # Pipeline entry point
├── preferences.example.yaml   # Synthetic ranking-preference template
├── examples/                  # Synthetic hosted configuration/profile inputs
├── docs/hosted-setup.md       # Hosted deployment guide and trust boundaries
├── src/
│   ├── fetch_gmail.py         # Gmail API fetcher
│   ├── parse_jobs.py          # Email parser and source-specific canonicalization
│   ├── dedupe.py              # SQLite dedupe/application tracking
│   ├── portfolio_policy.py    # Deterministic first-stage screening
│   ├── discovery_schedule.py  # Three-day digest and immediate-alert policy
│   ├── opportunity_decisions.py # Verified role buttons and decisions
│   ├── cloudflare_telegram.py  # Hosted callback-capability client
│   ├── hosted_opportunity_decision.py # Hosted details/discard handler
│   ├── local_worker_main.py    # Owner-local ATS/application runtime
│   ├── telegram_delivery.py   # Idempotent Telegram delivery
│   ├── telegram_smoke.py      # Explicit transport smoke test
│   └── notify_telegram.py     # Telegram message transport and rendering
├── scripts/
│   ├── job_agent.py           # Hosted init/dry-run/doctor CLI
│   └── mark_applied.py        # Local application logger
├── cloudflare/
│   └── telegram-gateway/      # Always-on Telegram webhook + D1 ledger
└── tests/
    ├── test_fetch_gmail.py     # Gmail token-refresh error handling
    ├── test_parse_jobs.py      # Current source-specific email formats
    └── test_telegram_smoke.py  # Diagnostic-message boundary
```

## Security Model

Do not commit secrets or private resume content.

Ignored local files include:

- `credentials.json`
- `token.json`
- `resume.md`
- `preferences.yaml`
- `hosted-config.yaml`
- `.job-agent/`
- `data/`
- `.env`
- local caches and SQLite files

GitHub Actions uses a read-only OAuth token restricted to the configured
dedicated career Gmail account. Alert snippets are screened locally and
are not sent to an LLM. OpenAI receives only a sanitized professional profile
and verified official-vacancy fields for deep grading. Anthropic is used later,
after explicit application preparation, to generate CV and cover-letter
artifacts from bounded evidence. Telegram receives ranked public job summaries,
two protected review PDFs, clickable links, and compact opaque callback tokens.
The review controls approve the exact package or request a new generation; the
gateway deletes the review messages after the choice or after 24 hours. The full callback scope
stays in Cloudflare D1. The gateway accepts only Telegram's configured webhook
secret and the configured owner/chat, then creates an exact GitHub repository
dispatch. These are intentional external data flows.

## Hosted Setup

Hosted discovery, grading, Telegram decisions, and application-artifact
preparation can run without an always-on personal computer. Start with a
mutation-free preview:

```bash
python scripts/job_agent.py hosted init \
  --config examples/hosted-config.example.yaml \
  --dry-run
```

Then follow the [hosted deployment guide](docs/hosted-setup.md) to create the
private configuration, authorize Gmail, provide scoped credentials, provision
GitHub Actions plus Cloudflare Worker/D1, register Telegram, and run:

```bash
python scripts/job_agent.py hosted doctor --config hosted-config.yaml
python scripts/job_agent.py hosted init --config hosted-config.yaml
```

The installer is resumable and keeps secrets out of its state and logs.
`Compila` and `Invia` remain local, human-gated operations; hosted mode does not
submit ATS applications.

## Local Setup

1. Create and activate a Python environment.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create a private local resume context.

```bash
cp resume.example.md resume.md
cp preferences.example.yaml preferences.yaml
```

Then replace `resume.md` with your private Markdown resume/profile context.
Adapt `preferences.yaml` to your target markets and screening policy. Both
files are ignored by git.

3. Create Gmail OAuth files.

Place your Google OAuth desktop client file at:

```text
credentials.json
```

The auth helper also checks `~/.config/google/client_secret.json` and
`~/.config/google/credentials.json`, or you can point it explicitly with
`GMAIL_CREDENTIALS_PATH`.

Then run:

```bash
export JOB_AGENT_CAREER_GMAIL="career.user@gmail.com"
python auth_gmail.py --career-email "$JOB_AGENT_CAREER_GMAIL"
```

This creates ignored `token.json`.
If Gmail starts returning `invalid_grant` or `Token has been expired or revoked`, rerun
`python auth_gmail.py` and replace the `GMAIL_TOKEN_JSON` GitHub Actions secret with
the new `token.json` contents. If the token keeps expiring after a short time, check
whether the Google OAuth consent screen is still in Testing mode.

To refresh the token and update the GitHub Actions secret in one step:

```bash
python auth_gmail.py \
  --career-email "$JOB_AGENT_CAREER_GMAIL" \
  --repo OWNER/REPOSITORY \
  --sync-secret
```

By default the helper prefers storing refreshed tokens at
`~/.config/google/job-agent-token.json` when that directory exists; otherwise it falls
back to the local ignored `token.json`. You can override the path with
`GMAIL_TOKEN_PATH` or `python auth_gmail.py --token /path/to/token.json`.

4. Export runtime secrets for local runs.

```bash
export OPENAI_API_KEY="..."
export JOB_AGENT_GRADING_PROFILE_JSON='{...}'
export JOB_AGENT_PREFERENCES_PATH="$PWD/preferences.yaml"
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

5. Run the agent.

```bash
python main.py --days 2
```

## GitHub Actions Setup

Add these repository secrets under:

`Settings` → `Secrets and variables` → `Actions`

- `GMAIL_CREDENTIALS_JSON`: full JSON content of `credentials.json`
- `GMAIL_TOKEN_JSON`: full JSON content of `token.json`
- `OPENAI_API_KEY`: OpenAI API key used for deep grading
- `JOB_AGENT_GRADING_PROFILE_JSON`: bounded professional profile for grading
- `JOB_AGENT_PREFERENCES_YAML`: private ranking and screening preferences
- `ANTHROPIC_API_KEY`: Anthropic API key used for application artifacts
- `JOB_AGENT_EVIDENCE_YAML`: private evidence source for CV and cover letters
- `JOB_AGENT_ARTIFACT_HANDOFF_KEY`: encryption key for application packages
- `TELEGRAM_BOT_TOKEN`: Telegram bot token
- `TELEGRAM_CHAT_ID`: Telegram chat ID
- `TELEGRAM_ACTOR_ID`: the only Telegram account allowed to decide
- `JOB_AGENT_CALLBACK_GATEWAY_TOKEN`: shared secret used only to mint callback
  capabilities

Add these Actions variables:

- `JOB_AGENT_CANDIDATE_NAME`
- `JOB_AGENT_CAREER_GMAIL`
- `JOB_AGENT_CANONICAL_CV_URL`
- `JOB_AGENT_CALLBACK_GATEWAY_URL`

The discovery
workflow runs daily at `06:00 UTC`, sends due cards itself, and can also be
started manually. The Cloudflare gateway owns the public webhook and its D1
idempotency ledger. Its deployment instructions and bindings are documented in
`cloudflare/telegram-gateway/README.md`.

## macOS ATS worker

The Mac is not required for daily ingest, grading, Telegram delivery, `👍`,
`👎`, or `Dimmi di più`. It becomes necessary only after artifact preparation,
when an ATS requires a signed-in browser to compile or submit the application.
That worker reads browser and artifact credentials from the macOS Keychain. Its
non-secret configuration lives at:

```text
~/Library/Application Support/job-agent/worker-config.json
```

The installed LaunchAgent is:

```text
~/Library/LaunchAgents/com.example.job-agent.plist
```

It reconciles hosted CV preparation and controls ATS interaction. `Compila` and
`Invia` remain fail-closed until a supported ATS/browser adapter is bound.

Inspect it without exposing secrets:

```bash
launchctl print "gui/$(id -u)/com.example.job-agent"
tail -n 100 "$HOME/Library/Application Support/job-agent/worker.err.log"
```

## Telegram behavior and smoke test

Silence is expected when no vacancy has completed the full eligibility path.
An email lead alone does not generate a Telegram message. A vacancy must:

1. be parsed and deduplicated;
2. pass deterministic policy screening;
3. be verified against an official vacancy with a non-empty description;
4. complete deep grading.

Top-tier roles and deadlines within 36 hours are sent by the same Actions run.
Other eligible roles wait for the three-day digest. Empty runs do not send a
“no matches” heartbeat.

To verify the Telegram transport independently of job discovery:

1. Open the repository's **Actions** page.
2. Select **Telegram smoke test**.
3. Choose **Run workflow**.

A successful run verifies Telegram delivery receipts. Discovery role cards get
their interactive controls from the Cloudflare gateway; the diagnostic smoke
test intentionally remains non-interactive.

### Synthetic application E2E

The gated `👍` → `Compila` → `Invia` journey can be exercised against a local
fake ATS. It creates synthetic PDFs, records exactly one fake submission, and
writes an ignored local report. For isolation, the live runner requires a
dedicated webhook-free Telegram test bot and refuses to alter the production
bot or its webhook. Before polling, it verifies the configured test-bot ID and
compares its Keychain token with the configured production-bot token.

Copy `examples/synthetic-bot-config.example.json`, store that test bot's token
under its configured macOS Keychain service/account, then run:

```bash
python scripts/job_agent.py synthetic-e2e run \
  --root applications/synthetic-e2e/manual-run \
  --test-bot-config /path/to/synthetic-bot-config.json
```

Never point this command at the production Telegram bot.

## Tests

Run deterministic parser/context tests locally:

```bash
python -m pytest
python -m compileall main.py src tests
```

The test suite uses synthetic fixtures and does not call Gmail, model providers,
or Telegram. Only the manually triggered smoke-test workflow calls Telegram.

## Mark Applications

After applying to a job:

```bash
python scripts/mark_applied.py "<job-url>" "optional notes"
```

Show application status:

```bash
python scripts/mark_applied.py --status
```

## Roadmap

The [third-party reuse roadmap](to-do.md) tracks the work required to make a
clean fork self-service without source-code edits.

## License

Copyright 2026 Job Agent contributors.

Licensed under the [Apache License 2.0](LICENSE).
