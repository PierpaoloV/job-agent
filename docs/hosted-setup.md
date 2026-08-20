# Hosted setup

This guide describes the single-user hosted deployment of Job Agent. In this
mode, scheduled discovery runs on GitHub Actions and Telegram callbacks are
handled by a Cloudflare Worker backed by D1. No always-on personal computer is
required for discovery, grading, Telegram decisions, or preparation of
application artifacts.

The hosted installer is designed for a fork or clone of this repository. It
does not turn Job Agent into a multi-user SaaS and it does not automate browser
submission to an applicant tracking system (ATS).

## What the hosted mode supports

| Capability | Hosted status | Boundary |
| --- | --- | --- |
| Read supported job-alert emails | Available | Dedicated Gmail mailbox, read-only OAuth |
| Normalize, deduplicate, and screen leads | Available | GitHub Actions; deterministic first pass |
| Resolve the official vacancy and deep-grade it | Available | One OpenAI request per lead that crosses the deterministic screen |
| Send ranked digests and urgent alerts | Available | Telegram |
| `👍`, `👎`, and `Dimmi di più` decisions | Available | Telegram webhook through Cloudflare |
| Ask for and retain a discard reason | Available | Telegram, D1, and authoritative Actions state |
| Prepare a tailored CV and cover letter after approval | Available | Anthropic plus an encrypted short-lived GitHub artifact |
| Review the prepared files | Available | Two protected Telegram PDFs with one-use `Approva` / `Rigenera` controls; messages are deleted on choice or after 24 hours |
| Fill an ATS form (`Compila`) | Not hosted | Requires a supported local browser/ATS adapter |
| Submit an application (`Invia`) | Not hosted | Requires a supported local browser/ATS adapter and an explicit final gate |
| Answer CAPTCHA, medical, demographic, salary, consent, or legal questions | Unsupported | Must remain a human decision |

`Compila` and `Invia` therefore remain fail-closed in a hosted-only
installation. A successful hosted setup must not be interpreted as permission
or capability to submit applications.

## Accounts and prerequisites

The model-provider API keys alone are not enough. A hosted installation needs:

- a GitHub account and a fork or clone of this repository;
- a Cloudflare account with Workers and D1 available;
- a dedicated career Gmail account;
- a Google Cloud project with the Gmail API enabled and an OAuth **Desktop app**
  client;
- a private Telegram bot created with
  [BotFather](https://core.telegram.org/bots/features#botfather), plus a private
  chat that the user has started;
- an OpenAI project API key for vacancy resolution and grading;
- an Anthropic API key for hosted CV and cover-letter preparation;
- Python 3.11 or later, Git, the GitHub CLI (`gh`), Node.js, and `npx`;
- a Cloudflare API token accepted by Wrangler, or an authenticated Wrangler
  session;
- a canonical CV PDF and a stable HTTPS URL from which the Actions runner can
  download the same PDF without interactive authentication.

Run these checks from the repository root:

```bash
python --version
git --version
gh --version
node --version
npx wrangler --version
gh auth status
```

Use a private repository if the candidate does not want the configuration,
workflow history, and public vacancy metadata to be visible. GitHub Secrets
protect secret values, but workflow logs and artifacts still require careful
access control.

## Obtain the deployment credentials

Authenticate the installer itself with GitHub:

```bash
gh auth login
gh auth status
```

Separately create a fine-grained GitHub token for the Worker. Select only the
target repository, grant repository **Contents: read and write**, choose a
finite expiry, and expose it using the name configured by
`github.dispatch_token_env`. This credential exists because a Telegram
callback reaches GitHub from Cloudflare, outside a GitHub Actions run.

Create a Cloudflare token restricted to the account that will own this
deployment, with edit access for Workers scripts and D1. Export it using the
name configured by `cloudflare.api_token_env`. The token is used only during
provisioning and is not installed in the Worker.

Create a Telegram bot with BotFather, open a private chat with the bot, and send
`/start`. Before any webhook is registered, inspect `getUpdates` locally and
copy `message.from.id` to `telegram.actor_id` and `message.chat.id` to
`telegram.chat_id`:

```bash
python - <<'PY'
import json
import os
import urllib.request

token = os.environ["TELEGRAM_BOT_TOKEN"]
with urllib.request.urlopen(
    f"https://api.telegram.org/bot{token}/getUpdates"
) as response:
    print(json.dumps(json.load(response), indent=2))
PY
```

Treat the response as private because it contains account, chat, and message
metadata. For a one-user private chat, the two numeric IDs will often match,
but configure them from the response rather than assuming that they do.

Finally, determine the expected Worker URL. With `workers_dev: true` it normally
has the form:

```text
https://WORKER-NAME.ACCOUNT-SUBDOMAIN.workers.dev
```

Set the complete expected URL in `cloudflare.worker_url`. The post-deployment
smoke check must fail if that endpoint does not belong to this deployment.

## Prepare the inputs

Copy the synthetic configuration and replace every example value:

```bash
cp examples/hosted-config.example.yaml hosted-config.yaml
```

Keep `hosted-config.yaml` untracked. The configuration contains coordinates and
paths, not secret values. Every secret is read from the environment variable
named in the configuration. Relative paths are resolved from the directory
containing the configuration file, so update the example's relative paths after
copying it to the repository root.

Create private copies of the two bounded candidate inputs:

```bash
cp examples/grading-profile.example.json grading-profile.json
cp examples/professional-evidence.example.yaml professional-evidence.yaml
cp preferences.example.yaml preferences.yaml
```

The inputs have deliberately different purposes:

- `grading-profile.json` is the sanitized professional profile sent to OpenAI
  for fit assessment. Do not include contact details, date of birth, health
  information, government identifiers, or other application-form answers.
- `professional-evidence.yaml` is the private, approved claim bank used to
  generate the CV and cover letter. Every claim must be true and traceable to
  the canonical CV or another named source.
- `preferences.yaml` controls deterministic screening and ranking preferences.
- the canonical CV PDF is used as the source-version anchor. The current hosted
  transport expects its configured URL to be directly downloadable over HTTPS.

The installer rejects inline secret fields. Export secrets into the variable
names referenced by `hosted-config.yaml`:

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
export GITHUB_DISPATCH_TOKEN="..."
export CLOUDFLARE_API_TOKEN="..."
export JOB_AGENT_CALLBACK_GATEWAY_TOKEN="..."
export JOB_AGENT_ARTIFACT_HANDOFF_KEY="..."
export TELEGRAM_WEBHOOK_SECRET="..."
```

Use randomly generated, independent values for the callback, webhook, and
artifact-handoff secrets. The handoff key must be a URL-safe base64-encoded
32-byte key. Do not reuse an API key. Avoid putting exports in shell history; a
password manager, temporary protected environment file, or process-scoped
secret injection is preferable.

Generate the three internal secrets locally if they do not already exist:

```bash
export JOB_AGENT_CALLBACK_GATEWAY_TOKEN="$(
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export TELEGRAM_WEBHOOK_SECRET="$(
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export JOB_AGENT_ARTIFACT_HANDOFF_KEY="$(
  python -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
)"
```

Store these values in a password manager before closing the shell. Losing the
handoff key makes existing encrypted application packages unreadable.

The Gmail OAuth client JSON and authorized-user token JSON are files, not
environment variables. Generate the token against the dedicated mailbox with:

```bash
python auth_gmail.py \
  --credentials /absolute/path/to/gmail-credentials.json \
  --token /absolute/path/to/gmail-token.json \
  --career-email career.example@gmail.com
```

The only Gmail scope accepted by the runtime is:

```text
https://www.googleapis.com/auth/gmail.readonly
```

## Configuration map

The installer treats GitHub Variables as non-secret operational configuration
and GitHub Secrets as confidential material.

| Source | Captured value | Destination | Why it is needed |
| --- | --- | --- | --- |
| `profile.candidate_name` | Candidate display name | GitHub Variable `JOB_AGENT_CANDIDATE_NAME` | Artifact rendering |
| `gmail.account` | Dedicated mailbox address | GitHub Variable `JOB_AGENT_CAREER_GMAIL` | Fail-closed mailbox identity check |
| `profile.canonical_cv_url` | Direct HTTPS PDF URL | GitHub Variable `JOB_AGENT_CANONICAL_CV_URL` | Fetch the canonical CV during preparation |
| Git repository context | `owner/repository` | Trusted GitHub context; Worker variable `GITHUB_REPOSITORY` | State restore and exact repository dispatch |
| `github.branch` | Configured branch | Validated branch and workflow dispatch ref | Bind setup and smoke dispatch to one branch |
| Gmail credentials file | OAuth client JSON | GitHub Secret `GMAIL_CREDENTIALS_JSON` | Refresh-token client identity |
| Gmail token file | Authorized-user JSON | GitHub Secret `GMAIL_TOKEN_JSON` | Read the dedicated mailbox |
| OpenAI environment variable | Project API key | GitHub Secret `OPENAI_API_KEY` | Deep grading |
| Grading profile file | Sanitized JSON | GitHub Secret `JOB_AGENT_GRADING_PROFILE_JSON` | Candidate-to-role comparison |
| Preferences file | Screening/ranking YAML | GitHub Secret `JOB_AGENT_PREFERENCES_YAML` | Owner-specific deterministic policy |
| Anthropic environment variable | Project API key | GitHub Secret `ANTHROPIC_API_KEY` | Artifact generation after `👍` |
| Evidence file | Approved evidence YAML | GitHub Secret `JOB_AGENT_EVIDENCE_YAML` | Truth-bounded application claims |
| `profile.artifact_handoff_key_env` | Encryption key | GitHub Secret `JOB_AGENT_ARTIFACT_HANDOFF_KEY` | Encrypt prepared application packages |
| Telegram environment variable | Bot token | GitHub Secret and Worker secret `TELEGRAM_BOT_TOKEN` | Send messages and answer callbacks |
| `telegram.chat_id` | Private destination chat | GitHub Secret and Worker variable `TELEGRAM_CHAT_ID` | Scope delivery and callbacks |
| `telegram.actor_id` | Allowed Telegram user | GitHub Secret and Worker variable `TELEGRAM_ACTOR_ID` | Reject decisions from other users |
| Callback environment variable | Shared random token | GitHub Secret `JOB_AGENT_CALLBACK_GATEWAY_TOKEN`; Worker secret `INTERNAL_API_TOKEN` | Authorize one-use callback creation |
| Webhook environment variable | Telegram webhook secret | Worker secret `TELEGRAM_WEBHOOK_SECRET`; Telegram webhook registration | Authenticate inbound updates |
| `github.dispatch_token_env` | Fine-grained repository token | Worker secret `GITHUB_DISPATCH_TOKEN` | Dispatch exact decisions to Actions |
| D1 creation result | Database ID | Generated Worker configuration | Bind the callback ledger |
| `cloudflare.worker_url` | Expected deployed Worker URL | GitHub Variable `JOB_AGENT_CALLBACK_GATEWAY_URL` | Mint callback capabilities |

The configuration names the environment variables; it never contains their
values. `TELEGRAM_CHAT_ID` and `TELEGRAM_ACTOR_ID` are treated as secrets in
GitHub even though they are not authentication credentials, because they are
personal identifiers.

## Least-privilege access

Use separate credentials for this deployment and restrict each one:

- **Google:** grant only `gmail.readonly` to the dedicated career mailbox. The
  runtime rejects broader Gmail scopes and a token belonging to another
  mailbox. The official scope list is documented by
  [Google](https://developers.google.com/workspace/gmail/api/auth/scopes).
- **GitHub Actions:** the workflow's automatic `GITHUB_TOKEN` is restricted to
  `contents: read` and `actions: read`. The Worker's fine-grained token should
  target only this repository. GitHub currently requires repository
  **Contents: write** for the create-repository-dispatch endpoint; it does not
  need organization-wide access. See the
  [repository dispatch permissions](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event).
- **Cloudflare:** restrict the provisioning token to one account and the
  permissions needed to edit Workers scripts and D1 resources. Do not store the
  Cloudflare provisioning token in the Worker. Cloudflare documents token
  restriction and expiry in its
  [API token guide](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/).
- **Telegram:** use a dedicated bot in a one-user private chat. A bot token
  grants control of the bot, so rotate it immediately if exposed. The bot
  cannot initiate the first conversation; the user must send it a message.
- **OpenAI and Anthropic:** use project-scoped keys with spend limits rather
  than account-wide administrative credentials. Keep artifact preparation
  disabled when an Anthropic key is not configured.

## Preview the installation

The dry run validates the configuration shape and prints the ordered plan
without requiring the referenced private files or secret values and without
creating resources, uploading secrets, writing installer state, deploying the
Worker, or registering the webhook:

```bash
python scripts/job_agent.py hosted init \
  --config examples/hosted-config.example.yaml \
  --dry-run
```

Run the same command with the private `hosted-config.yaml` before applying:

```bash
python scripts/job_agent.py hosted init \
  --config hosted-config.yaml \
  --dry-run
```

A dry run must contain no secret value. Stop if output includes a token, key,
private profile, evidence statement, or Gmail OAuth JSON.

## Install

Apply the plan:

```bash
python scripts/job_agent.py hosted init \
  --config hosted-config.yaml
```

The installer performs these stages in order:

1. **Preflight:** validate the schema, required files, secret environment
   variables, executable prerequisites, Git repository, and authenticated
   GitHub/Cloudflare access.
2. **Repository binding:** confirm the exact repository, configured branch, and
   workflow; refuse ambiguous or mismatched coordinates.
3. **GitHub configuration:** upload candidate inputs as the Secrets and
   Variables shown above. Secret contents are passed over standard input and
   must not appear in command arguments or logs.
4. **Gmail authorization:** upload the OAuth client and authorized-user JSON
   files. The hosted runtime then fails closed on first use unless the token is
   refreshable, grants only the read-only scope, and belongs to the configured
   dedicated mailbox.
5. **Telegram scope:** upload the bot token and bind the one allowed actor and
   private chat. Telegram validates the token during webhook registration.
6. **Cloudflare storage:** create or discover the named D1 database and apply
   every migration.
7. **Gateway deployment:** materialize an untracked Worker configuration,
   upload Worker secrets, deploy the Worker, and verify its configured HTTPS
   URL.
8. **Webhook registration:** register the exact `/telegram` endpoint with the
   configured webhook secret and only the update types used by Job Agent.
9. **Hosted smoke checks:** verify the Worker health endpoint and Telegram
   webhook registration, then dispatch the configured workflow. The smoke step
   does not independently query D1; the first interactive role authorization
   exercises that binding.

The installer writes resumable, non-secret progress to:

```text
.job-agent/hosted-state.json
```

By default this directory is created beside the configuration file. Use
`--state /path/to/hosted-state.json` to choose another non-secret state path.

Rerun the same `hosted init` command after an interruption. Completed stages
are skipped, and a stage that failed before any external command succeeded can
be retried. A partial or transport-uncertain stage is marked `uncertain` and is
not retried automatically. Inspect the named provider and reconcile the exact
resource first. An automated uncertain-state reconciliation command is not yet
available; use a new `--state` path only after confirming that rerunning the
full idempotent plan cannot duplicate or overwrite an unresolved effect.

## Verify with doctor

Run:

```bash
python scripts/job_agent.py hosted doctor \
  --config hosted-config.yaml
```

`doctor` reports failures with a non-zero exit code. Resolve every failure
before enabling the schedule. It checks local prerequisites and configuration
without printing secrets. After setup, also verify:

1. the Worker `GET /health` endpoint returns success without private data;
2. the manually triggered **Telegram smoke test** sends one non-interactive
   diagnostic message;
3. a manual **Daily job discovery** run can read the dedicated mailbox;
4. an eligible synthetic or real alert reaches Telegram with three controls;
5. `Dimmi di più` is accepted only from the configured actor/chat;
6. `👎` asks for a reason and one exact reply produces one decision;
7. `👍` prepares an encrypted package but does not fill or submit an ATS form.

Silence after a discovery run can be correct: an email must be parsed, pass the
deterministic policy, resolve to one current official vacancy, and complete
deep grading before it is eligible for Telegram delivery.

## Hosted data flows

```text
Dedicated Gmail
  -> GitHub Actions ingest
  -> deterministic parsing, dedupe, and screening
  -> OpenAI official-vacancy resolution + deep grading
  -> GitHub authoritative state/artifacts
  -> Telegram ranked card

Telegram callback/reply
  -> Cloudflare Worker
  -> D1 one-use authorization and update ledger
  -> exact GitHub repository_dispatch
  -> details, discard, or artifact preparation

Approved preparation
  -> private evidence + canonical CV on an ephemeral Actions runner
  -> Anthropic structured CV/cover-letter generation
  -> deterministic claim audit and PDF rendering
  -> encrypted GitHub artifact
  -> authoritative `CV pronto` state bound to the package SHA-256
  -> protected CV + cover-letter PDFs and one-use review controls on Telegram
  -> delete the Telegram review on choice or after 24 hours
  -> persist exact approval, or run a new Anthropic generation on `Rigenera`
  -> no hosted ATS fill or submission
```

| Processor/store | Data received or retained | Default repository behavior |
| --- | --- | --- |
| Gmail | Job-alert emails and account identity | Source mailbox; read-only access |
| GitHub Actions runner | Email bodies, OAuth files, profile, preferences, evidence, CV, vacancy data | Ephemeral runner; plaintext candidate material is removed in cleanup |
| OpenAI | Alert lead, public vacancy-resolution prompt, sanitized grading profile | Responses request uses `store: false`; provider policy still applies |
| Anthropic | Verified vacancy, requirement matrix, and approved evidence statements | Used only after explicit preparation approval |
| Telegram | Ranked public job summaries, protected review PDFs, links, status messages, and the user's discard reason | Review PDFs use `protect_content` and are deleted on decision or by the 24-hour cloud sweep; Telegram is not a true view-once PDF channel |
| Cloudflare Worker/D1 | Opaque callback capabilities, protected-message receipts, actor/chat scope, update IDs, action state, discard reason | Durable idempotency, one-use approval, and expiry reconciliation ledger |
| GitHub Actions artifacts | Public vacancy/grade state; encrypted application package | Current workflows use 14-day state retention and 3-day package retention |
| Canonical CV host | CV PDF | Must currently permit non-interactive HTTPS download |

OpenAI states that API data is not used for model training unless the customer
opts in, while default abuse-monitoring logs may be retained for up to 30 days.
Review the current
[OpenAI data controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
and the current Anthropic data terms before providing candidate material.

## Costs and service limits

Costs are usage-dependent and provider pricing changes. Check the linked
official pages before enabling the schedule.

- **GitHub Actions:** standard runners are free for public repositories. A
  GitHub Free private repository currently includes 2,000 minutes per month and
  500 MB of shared artifact/package storage; overages depend on the account and
  budget settings. See
  [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
- **Cloudflare:** a low-volume single-user gateway will often fit within the
  Workers and D1 free limits. The current free plan documents 100,000 Worker
  requests per day, 5 million D1 rows read per day, 100,000 rows written per
  day, and 5 GB D1 storage. The paid Workers plan has a minimum monthly charge.
  See [Cloudflare pricing](https://developers.cloudflare.com/workers/platform/pricing/).
- **Telegram:** the Bot Platform is free for ordinary bot use, subject to rate
  limits. See [Telegram bots](https://core.telegram.org/bots).
- **Gmail API:** normal API quota limits apply; this project does not request a
  paid Google service. A Google Workspace subscription, if used, is separate.
- **OpenAI:** every lead that crosses the deterministic screen can incur one
  Responses API request with required web search, input/output tokens, and tool
  usage. See [OpenAI API pricing](https://openai.com/api/pricing/).
- **Anthropic:** each approved artifact preparation can incur one structured
  request whose cost depends on the selected model and input/output tokens. See
  [Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing).
- **CV hosting:** any charge for the HTTPS host is external to Job Agent.

A practical estimate is:

```text
monthly cost =
  verified leads * OpenAI grading cost
  + approved preparations * Anthropic generation cost
  + GitHub/Cloudflare overages
  + CV-hosting cost
```

Set hard provider budgets before the first scheduled run. Start with a manual
one-day discovery window, inspect how many leads cross the deterministic
screen, and only then enable daily scheduling.

## Limitations

- Discovery depends on the HTML and link shapes of supported alert senders.
  Providers can change their email format without notice.
- Only a lead that resolves to one current official employer or employer ATS
  page is deep-graded. Ambiguous, expired, or inaccessible vacancies fail
  closed.
- Model output is schema-validated but remains probabilistic. Scores are
  decision support, not objective hiring probabilities.
- The canonical CV URL is currently an unauthenticated HTTPS fetch boundary.
  Do not use it for a document that must remain private.
- A dedicated Gmail mailbox is required. The runtime intentionally rejects a
  different mailbox or broader Gmail scope.
- The Telegram deployment is single-user and single-chat. It is not a
  multi-tenant access-control system.
- D1 and GitHub artifacts are operational stores, not a long-term applicant
  tracking system or backup.
- Hosted preparation generates and encrypts documents. It does not guarantee
  that an ATS accepts the layout or file format.
- Hosted v1 requires Anthropic because every role card includes the explicit
  `👍` artifact-preparation action. A discovery-only provider configuration is
  roadmap work rather than a partially functional installation.
- CAPTCHA, login challenges, ATS-specific questions, file upload, final review,
  and submission remain outside hosted mode.
- No health, disability, demographic, veteran, salary, consent, legal, or
  work-authorization answer is inferred.

## Pause, rotate, and teardown

The current CLI exposes `hosted init` and `hosted doctor`. Automated
`pause`, `rotate-secrets`, and `destroy` commands remain roadmap work. Until
those commands exist, use the following manual procedures and record every
external change.

### Pause

Disable the **Daily job discovery** workflow schedule in GitHub Actions. Leave
the Worker deployed if existing Telegram buttons must continue working; their
capabilities are short-lived.

### Rotate credentials

Rotate one boundary at a time:

1. create the replacement credential with the same or narrower permissions;
2. update the corresponding GitHub Secret and/or Worker secret;
3. run `hosted doctor` and the relevant smoke test;
4. revoke the old credential only after the new path succeeds.

For Gmail, rerun `auth_gmail.py` and replace `GMAIL_TOKEN_JSON`. For Telegram,
replace the bot token in both GitHub and Cloudflare. For the callback token,
update `JOB_AGENT_CALLBACK_GATEWAY_TOKEN` and `INTERNAL_API_TOKEN` together.
For the webhook secret, update the Worker secret and re-register the webhook as
one change.

### Teardown

Teardown is destructive and currently manual:

1. disable the GitHub Actions schedule;
2. delete the Telegram webhook;
3. delete the Cloudflare Worker;
4. export any required audit evidence, then delete the D1 database;
5. delete the repository's Job Agent Secrets and Variables;
6. revoke the GitHub dispatch token;
7. revoke Gmail OAuth access and delete local OAuth/token files;
8. revoke the Telegram bot token or delete the bot;
9. revoke model-provider and Cloudflare provisioning keys;
10. delete `.job-agent/hosted-state.json` and private local configuration only
    after the remote resources are confirmed absent.

Do not delete D1 or Actions state while a callback or artifact operation has an
uncertain outcome. Reconcile the exact application and vacancy version first.
