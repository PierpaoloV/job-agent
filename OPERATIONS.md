# Job agent operations

This runbook is for the owner-operated release. Discovery, non-sensitive
grading, and approved professional-document generation may run in GitHub
Actions. Telegram authorization, package decryption, private state, browser
filling, and submission stay on the Mac.

## 1. Install and verify

Use Python 3.11 or newer in a dedicated virtual environment, then install the
pinned project dependencies:

```sh
python -m pip install -r requirements.txt
PYTHONPATH=.:src python -m pytest -q -p no:cacheprovider
```

Keep `data/private/`, OAuth files, browser state, generated application
packages, and Keychain values out of Git. A release is not ready if the full
test suite fails.

## 2. Connect the dedicated Gmail account

Configure a dedicated Gmail address for career monitoring and new ATS
accounts. Create a Google OAuth desktop client, keep its JSON outside the
repository (the default private location is
`~/.config/google/client_secret.json`), and authorize only `gmail.readonly`:

```sh
export JOB_AGENT_CAREER_GMAIL="career.user@gmail.com"
python auth_gmail.py --career-email "$JOB_AGENT_CAREER_GMAIL" \
  --credentials ~/.config/google/client_secret.json \
  --token ~/.config/google/job-agent-token.json
```

Confirm that the browser consent screen names the dedicated account. The
runtime rechecks the authenticated address and exact read-only Gmail scope on
every fetch; it never falls back to a personal mailbox. If remote discovery
needs the token, copy it to the GitHub Actions secret with the printed `gh
secret set` command. Never paste it into a tracked file or a Telegram message.

## 3. Dedicated browser profile

Create a browser profile named exactly `Job Applications`. Sign into ATS sites
with the dedicated Gmail address only. Do not reuse a personal browser profile.
CAPTCHA, non-email MFA, unusual consent, unsupported controls, and site
restrictions are manual interventions: resolve the page in this profile and
resume from Telegram. `Compila` must stop on the review page; only a fresh
`Invia` authorization may activate submit.

## 4. Keychain and local configuration

Store the Telegram bot token in macOS Keychain under the service/account named
by the local worker configuration. Use Keychain Access so the token does not
enter shell history. Generated ATS passwords are stored by the worker under
the configured ATS service (the Workday default is `job-agent.workday`) and the
dedicated Gmail account.

Place the non-secret worker configuration beside the worker state, normally at
`~/Library/Application Support/job-agent/worker-config.json`:

```json
{
  "version": "job-agent.local-worker-config.v1",
  "telegram": {
    "actor_id": "YOUR_TELEGRAM_USER_ID",
    "chat_id": "YOUR_TELEGRAM_CHAT_ID",
    "token_keychain_service": "job-agent.telegram",
    "token_keychain_account": "worker-bot"
  },
  "hosted_artifacts": {
    "repository": "example-org/job-agent",
    "branch": "main",
    "workflow": "run.yml",
    "github_token_keychain_service": "job-agent.github",
    "github_token_keychain_account": "example-org/job-agent",
    "handoff_key_keychain_service": "job-agent.artifact-handoff",
    "handoff_key_keychain_account": "example-org/job-agent"
  }
}
```

This file contains coordinates only. The LaunchAgent plist must contain no
secret or environment token.

The hybrid artifact handoff needs two additional GitHub Secrets:

- `JOB_AGENT_EVIDENCE_YAML`: the professional-only `evidence.yaml`; never add
  health, demographic, identity-document, credential, or ATS-answer data.
- `JOB_AGENT_ARTIFACT_HANDOFF_KEY`: one random 32-byte key encoded as base64.

Store the exact same handoff key in macOS Keychain under service
`job-agent.artifact-handoff` and account `example-org/job-agent`. Store a
fine-grained GitHub token able to dispatch the workflow and read Actions
artifacts under service `job-agent.github` and the same account. Neither value
belongs in the worker JSON, shell profile, repository, logs, or Telegram.
The GitHub token needs Actions read and Contents write for this repository;
GitHub requires Contents write for repository dispatch.

The hosted job retrieves the canonical CV from the public `example-org/cv`
release. Its extractable professional text is the authoritative source for
contact details, role metadata, education, and other CV structure selected by
Sonnet; personal health, demographic, identity-document, secret-credential,
and ATS-answer lines are removed before that boundary. Every selected material
line receives a canonical-CV evidence ID that the orchestration service
independently revalidates against the exact master-CV version. Experience and
education are selected as contiguous source blocks, so fields from different
records cannot be combined. The private evidence YAML remains the authority
for tailored requirement claims.
Publishing a new master CV and updating `JOB_AGENT_EVIDENCE_YAML` must be
treated as one source-version update before preparing another role.

The CLI deliberately reports `disabled: application_coordinator_missing` until
production supplies real vacancy and ATS/browser adapters. Once those are
present, `hosted_artifact_configuration_missing`, `github_secret_missing`,
`artifact_handoff_secret_missing`, or
`hosted_artifact_composition_unavailable` identify a disabled hybrid
composition without printing any value. Do not install or label the worker
healthy before the full binding exists; the control-only Telegram runtime is
not a release. The repository includes no live production ATS driver, and the
offline Workday fixture must never be presented as one.

## 5. Schedules and health

GitHub Actions fetches every day at 06:00 UTC. Deep grading starts only for a
non-empty verified shortlist. The application schedule sends an immediate
top-tier alert, a digest every three days with at most ten roles, and exposes
the remaining roles on request.

When `Prepara candidatura` is approved, the Mac records the current workflow
run IDs in the configured workflow/branch/event scope, persists a dispatching
marker, and sends only the application ID plus exact official vacancy version.
The callback returns immediately while Actions restores the matching
preparation snapshot, makes one Sonnet generation call, audits and renders both
documents, rejects incomplete or non-source-bound selections, encrypts them,
and uploads only `application-artifacts.enc` for
three days. Later worker cycles bind exactly one new workflow run and download
only its artifact. The Mac verifies authenticated authority, identity,
manifest, PDF hashes, owner and permissions before exposing `Compila` and
emitting `CV pronto`. Zero runs after the deadline, multiple candidate runs,
or a failed run leave `Compila` unavailable and require explicit resolution;
they never cause a blind redispatch.

For every terminal preparation, Telegram reports the exact role and the
owner-local failure reason without claiming that a CV is ready. The message
contains `Riprova preparazione` only when a fresh GitHub check proves that the
exact prior identity has no active, successful, ambiguous, or package-bearing
run that could still complete. A message without that button is informational:
inspect GitHub and the local logs; do not use the ordinary `Prepara
candidatura` action to bypass the block. Even when the button is present, the
worker repeats the fresh check at click time, cancels the old intent, and
creates one new intent only after the scoped one-use authorization is accepted.
There is no automatic preparation retry.

The Mac worker starts at login through launchd, keeps private logs in a
directory with owner-only permissions, and reports `healthy`, `paused`,
`degraded`, or `stopped`. Run a non-mutating one-cycle health check with:

```sh
PYTHONPATH=src python -m local_worker_main --once
```

## 6. Pause, recovery, and uncertain outcomes

Use `/pausa` before maintenance or whenever no new local action should begin.
Use `/stato` to inspect state and `/riprendi` to create a new resume generation.
Buttons are actor/chat scoped, expire after at most 30 minutes, and are one-use;
reopen the current role card instead of retrying a stale button.

After a crash, restart the worker and let durable state choose the next safe
step. Never blindly retry a submission or provider call marked `uncertain`.
Inspect the ATS, the dedicated Gmail mailbox, and the protected application
report. Only then use `/riconcilia CAPABILITY`; a typed verifier must establish
that no external effect occurred before a new authorization can be issued.

If an artifact, answer manifest, official vacancy fingerprint, deadline, or
master CV changes, prepare and review again. An unchanged prior application is
blocked; a materially reopened role requires explicit evidence of the change.

## 7. Shutdown

Send the worker stop command through the owner-local control path, then remove
the LaunchAgent with `launchctl bootout` through the installer/runner. Confirm
that the worker reports `stopped`, no process remains, and no callback is in a
processing or uncertain state. Preserve private reports and audit state; do not
delete them as part of shutdown.

For an emergency containment, use `/pausa` first, revoke the Telegram token and
Gmail OAuth grant, stop the LaunchAgent, and rotate affected ATS credentials in
Keychain. Do not delete evidence needed to reconcile a possibly submitted
application.
