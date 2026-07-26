# Third-party reuse roadmap

This roadmap tracks the work required to turn Job Agent from a working,
owner-specific deployment into a safe, documented, self-service project that a
third party can fork and configure without editing application code.

## Definition of reusable

A new user should be able to:

1. fork or clone the repository;
2. choose either hosted discovery or the full hybrid setup;
3. provide their own professional profile, preferences, provider accounts, and
   secrets through documented inputs;
4. provision GitHub Actions, Gmail, Telegram, and Cloudflare without editing
   Python, workflow, or Worker source;
5. run smoke tests that prove every configured boundary;
6. remove the deployment and revoke credentials through a documented teardown;
7. understand which application and ATS actions are supported, gated, or still
   unavailable.

## P0 — Safe public foundation

- [x] Add an OSI-approved license (`Apache-2.0`) and expose it from the README.
- [ ] Audit the complete Git history for credentials, tokens, private resume
  content, personal identifiers, and generated application data.
- [ ] Add automated secret scanning and dependency/security update checks.
- [ ] Replace every owner-specific production default with required
  configuration:
  - candidate name and email address;
  - email signature;
  - dedicated career Gmail address;
  - GitHub owner, repository, workflow, and branch;
  - canonical CV URL or local CV source;
  - Telegram actor and chat IDs;
  - Cloudflare account, Worker, and D1 identifiers;
  - locale, timezone, languages, and target-market preferences;
  - macOS service names and filesystem paths.
- [x] Move the real Cloudflare configuration to an ignored file and publish only
  a synthetic `wrangler.jsonc.example`.
- [x] Confirm that all examples contain synthetic identities and data.
- [x] Add a machine-validated configuration schema with actionable errors for
  missing, conflicting, or unsafe values.
- [ ] Make the repository fail closed when an owner-specific value remains in a
  third-party deployment.

## P1 — Reproducible hosted setup

- [x] Add and track a generic setup CLI instead of an owner-specific script.
- [ ] Detect prerequisites and versions for Python, Git, GitHub CLI, Node.js,
  Wrangler, and supported operating systems.
- [ ] Discover the fork's repository, owner, and default branch automatically.
- [ ] Guide the user through creating a dedicated career Gmail account.
- [ ] Guide Google Cloud project creation, Gmail API enablement, OAuth consent,
  read-only authorization, token refresh, and revocation.
- [ ] Guide Telegram bot creation and obtain the private actor/chat IDs without
  exposing the bot token in logs or shell history.
- [x] Provision the Cloudflare Worker and D1 database from the user's account.
- [x] Apply D1 migrations and configure the Telegram webhook idempotently.
- [ ] Generate and store callback, webhook, and artifact-encryption secrets
  without printing them.
- [x] Configure all required GitHub Actions secrets and variables.
- [ ] Accept OpenAI and Anthropic provider choices through configuration; do not
  assume that both providers are enabled.
- [ ] Make schedules, digest cadence, maximum results, and urgent-alert policy
  configurable.
- [x] Add a hosted-only setup path that requires no always-on local computer.
- [x] Add idempotent reruns so interrupted setup can safely resume.
- [x] Add a dry-run mode that describes every external change before applying it.
- [ ] Add a teardown command for webhooks, Worker/D1 resources, local tokens, and
  GitHub secrets.

## P2 — Portable professional profile and ranking

- [x] Replace the current personal preferences with
  `preferences.example.yaml`.
- [ ] Define a versioned, documented schema for the sanitized grading profile.
- [ ] Define a separate private evidence schema for CV and cover-letter claims.
- [ ] Provide synthetic example profiles covering research, engineering, and
  production-oriented candidates.
- [ ] Add profile import and validation commands that never send private content
  to a model.
- [ ] Make excluded companies, geographies, languages, seniority, compensation,
  relocation, sponsorship, and remote-work policies configurable.
- [ ] Make model selection, reasoning level, token budget, and per-run cost
  limits configurable.
- [x] Document exactly which fields cross into Gmail, Telegram, Cloudflare,
  OpenAI, Anthropic, GitHub Actions, and local storage.
- [ ] Add profile and preference migrations with explicit versioning.

## P3 — Portable hybrid and ATS setup

- [x] Split hosted discovery from optional local ATS/browser automation in both
  code and documentation.
- [ ] Generate the macOS LaunchAgent from a template; remove absolute owner
  paths and personal service names.
- [ ] Decide and document support for Linux and Windows local workers.
- [ ] Add a portable secrets adapter instead of assuming macOS Keychain.
- [ ] Add a dedicated browser-profile setup and verification flow.
- [ ] Publish a support matrix for each ATS: inspect, fill, upload, review, and
  submit.
- [ ] Keep unsupported ATS operations fail-closed and explain the required human
  intervention.
- [ ] Add a non-production ATS fixture for end-to-end tests.
- [ ] Verify application identity, vacancy version, attachments, answers, and
  confirmation evidence across the complete journey.
- [ ] Document that health, disability, demographic, salary, consent, and legal
  answers are never inferred.

## P4 — Onboarding and operations

- [ ] Add a five-minute project overview and an architecture diagram.
- [ ] Add separate quickstarts for local development, hosted discovery, and the
  full hybrid deployment.
- [x] Publish a feature-status table distinguishing available, experimental,
  planned, and unsupported behavior.
- [x] Document expected provider and infrastructure costs with configurable
  assumptions.
- [x] Document Gmail, Telegram, GitHub, Cloudflare, model-provider, and ATS data
  retention.
- [ ] Add troubleshooting guides for OAuth expiry, webhook failures, D1
  migrations, missing alerts, model failures, and uncertain external outcomes.
- [ ] Add backup, restore, reconciliation, credential rotation, pause, shutdown,
  and disaster-recovery procedures.
- [ ] Add an automated post-setup checklist and a single health/status command.
- [ ] Make all user-facing messages configurable for language and locale.

## P5 — Quality, releases, and community

- [ ] Test setup from a fresh fork with a brand-new Gmail, Telegram, GitHub, and
  Cloudflare account.
- [ ] Add CI coverage for every supported Python and operating-system version.
- [ ] Test configuration examples and setup scripts without real external calls.
- [ ] Add contract tests for provider adapters and recorded synthetic job-alert
  formats.
- [ ] Add a complete synthetic journey from email ingest to Telegram decision,
  artifact preparation, ATS review, and verified submission report.
- [ ] Add semantic versioning, changelog generation, signed releases, and
  documented database/configuration migrations.
- [ ] Add `CONTRIBUTING.md`, `SECURITY.md`, a code of conduct, issue templates,
  and pull-request templates.
- [ ] Decide whether contributions use a Developer Certificate of Origin or a
  Contributor License Agreement.
- [ ] Document support expectations and the boundary between personal project,
  community support, and production use.

## Release gate for “self-service”

Do not describe Job Agent as self-service until:

- [ ] no owner-specific value is required in tracked source;
- [ ] a clean fork completes hosted setup without source edits;
- [ ] all external resources can be provisioned, verified, and removed through
  documented commands;
- [ ] privacy and security checks pass against the full Git history and a fresh
  deployment;
- [ ] the feature-status table accurately reflects ATS and submission support;
- [ ] an independent tester completes the documented setup without maintainer
  intervention.
