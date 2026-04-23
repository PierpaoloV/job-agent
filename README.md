# Job Agent

Job Agent reads job-alert emails, extracts job listings, ranks them against a
private resume/profile context with Claude, and sends a concise Telegram digest.

The repository is designed so private credentials and resume context are supplied
at runtime through local ignored files or GitHub Actions secrets, not committed to
the public repo.

## What It Does

- Fetches job-alert emails from Gmail for LinkedIn, Indeed, Glassdoor, Welcome to
  the Jungle, EuroTechJobs, and compatible fallback sources.
- Parses listings into structured fields such as title, company, location, URL,
  snippet, salary, seniority, remote policy, and detected skills.
- Deduplicates seen jobs with SQLite.
- Scores new jobs with Anthropic Claude using your private resume context and
  `preferences.yaml`.
- Sends a decision-first Telegram digest with score, priority, reasons,
  concerns, verdict, application angle, and job link.
- Lets you mark applications locally with `scripts/mark_applied.py`.

## Repository Layout

```text
.
├── main.py                    # Pipeline entry point
├── preferences.yaml           # Non-secret ranking preferences
├── resume.example.md          # Non-sensitive template for private resume context
├── src/
│   ├── fetch_gmail.py         # Gmail API fetcher
│   ├── parse_jobs.py          # Email parser and source-specific canonicalization
│   ├── dedupe.py              # SQLite dedupe/application tracking
│   ├── rank_llm.py            # Claude ranking prompt and scoring
│   └── notify_telegram.py     # Telegram digest sender
├── scripts/
│   └── mark_applied.py        # Local application logger
└── tests/
    ├── test_parse_jobs.py
    └── test_rank_llm_context.py
```

## Security Model

Do not commit secrets or private resume content.

Ignored local files include:

- `credentials.json`
- `token.json`
- `resume.md`
- `data/`
- `.env`
- local caches and SQLite files

GitHub Actions expects secrets for Gmail, Anthropic, Telegram, and private resume
context. The ranker reads resume context from `JOB_AGENT_RESUME_MD` first and
falls back to local ignored `resume.md` only for local development.

Anthropic receives the private resume context and bounded job-alert text for
ranking. Telegram receives ranked job summaries and clickable job links. These
are intentional external data flows.

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
```

Then replace `resume.md` with your private Markdown resume/profile context.
`resume.md` is ignored by git.

3. Create Gmail OAuth files.

Place your Google OAuth desktop client file at:

```text
credentials.json
```

Then run:

```bash
python auth_gmail.py
```

This creates ignored `token.json`.

4. Export runtime secrets for local runs.

```bash
export ANTHROPIC_API_KEY="..."
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
- `ANTHROPIC_API_KEY`: Anthropic API key
- `JOB_AGENT_RESUME_MD`: private Markdown resume/profile context
- `TELEGRAM_BOT_TOKEN`: Telegram bot token
- `TELEGRAM_CHAT_ID`: Telegram chat ID

The workflow runs on schedule and can also be started manually from GitHub
Actions.

## Tests

Run deterministic parser/context tests locally:

```bash
python -m pytest
python -m compileall main.py src tests
```

The tests use synthetic fixtures and do not call Gmail, Anthropic, or Telegram.

## Mark Applications

After applying to a job:

```bash
python scripts/mark_applied.py "<job-url>" "optional notes"
```

Show application status:

```bash
python scripts/mark_applied.py --status
```
