# Job-agent domain context

This repository uses the following discovery and delivery vocabulary:

- **Shortlist artifact**: versioned, candidate-safe output of deterministic
  ingest and screening.
- **Pending shortlist**: verified shortlisted roles that remain retryable until
  a validated cached deep grade exists for each exact vacancy version.
- **Digest batch**: immutable ranked snapshot; at most ten roles are presented
  initially and its overflow is a read-only view of the same snapshot.
- **Schedule outbox**: durable intents for due digests and urgent alerts.
- **Delivery claim**: unique transactional key for one Telegram message.
- **Local ATS callback state**: Mac-side `pending`, `completed`, or `uncertain`
  processing state for one Telegram update that controls browser or artifact
  work. An uncertain external outcome is surfaced and never retried blindly.
- **Hosted opportunity callback authorization**: short-lived, opaque, one-use
  capability in Cloudflare D1, bound to one Telegram actor/chat, exact
  application ID, official vacancy version, and action.
- **Hosted opportunity update**: Telegram webhook update authenticated by its
  webhook secret and claimed exactly once in D1 before it can cross the GitHub
  dispatch boundary. A transport-uncertain GitHub outcome is retained and
  never retried blindly.
- **Discard reason request**: D1 lifecycle created before the bot sends an
  authorized `discard` force-reply prompt, then bound to that exact prompt,
  actor, chat, application ID, and official vacancy version. The owner's
  non-empty reply is staged durably in D1 before GitHub dispatch; an uncertain
  external outcome retains the exact reason for reconciliation.
- **Opportunity decision dispatch**: exact, versioned `repository_dispatch`
  envelope emitted by the hosted callback gateway. GitHub Actions resolves
  `prepare`, `discard`, and `details`; browser/ATS actions remain owner-local.
- **Worker control generation**: durable owner-local `pause`, `resume`, or
  `stop` command plus a generation number. Every external action is checked
  against the current generation immediately before crossing its action
  boundary.
- **Capability claim**: exclusive owner, token, generation, and renewable lease
  for one local worker capability. An expired lease becomes `uncertain`; it is
  never stolen or retried automatically.
- **Worker heartbeat**: durable timestamp and worker identity used to distinguish
  a live local runtime from stale persisted state. It does not override an
  uncertain capability claim.
- **Reconciliation decision**: capability-specific, verifier-produced outcome
  with evidence, provenance, timestamp, and actor. Every decision is audited
  locally, and only a verified-idempotent retry clears an uncertain claim.
- **Authoritative Actions state**: versioned, hash-validated immutable artifact
  restored across hosted runs. A hosted opportunity card is sent only after
  its exact role state is published; the Mac synchronizes it read-only before
  later artifact or ATS work. The Actions cache is only an optimization.
- **Local grading handoff**: authority-, hash-, and schema-validated transfer of
  candidate-safe `needs_local_fetch` records from authoritative Actions state.
  Its identity binds one stable opportunity ID to one canonical official
  vacancy hash; only the locally retrieved official snapshot enters the shared
  deep-grading contract. The imported artifact remains immutable; a private
  local consumption projection records which identities remain or completed.
- **Handoff grading intent**: owner-, token-, lease-, vacancy-version-, and
  grading-input-bound claim persisted before local retrieval and again before
  the model boundary. A possible provider call without exact durable cache
  evidence becomes `uncertain`; it cannot run again until exact-cache
  reconciliation succeeds or a typed human resolution confirms no result.
- **Hosted preparation input**: candidate-safe, canonical snapshot persisted in
  authoritative Actions state for one exact official vacancy version. It holds
  only the verified description, artifact family, and requirements-to-evidence
  matrix needed for professional-document generation.
- **Encrypted artifact handoff**: AES-256-GCM package produced after
  `Prepara candidatura`. Its identity binds the application ID and official
  vacancy version, while its authority binds repository, workflow, and branch.
  The Mac persists a pre-dispatch workflow-run baseline, binds only one new run
  in the exact workflow/branch/event scope, downloads only that run's package,
  decrypts it with a Keychain-held key, verifies owner, permissions and hashes,
  and only then enables `Compila`. Ambiguous or absent run identity fails
  closed.
- **ATS review evidence**: typed local snapshot of the review page, exact form
  values, and attachment hashes produced by a supported fill journey before
  submission is authorized.
- **Answer disclosure**: typed privacy label derived from trusted ATS question
  semantics. Only explicitly principal answers may cross into the Telegram
  pre-submit summary; unknown, health, and demographic answers stay local.
- **Submission evidence**: locally captured confirmation-page, ATS, and career
  mailbox markers that justify a verified `inviata` transition; never an
  unverified success assumption.
- **Browser intervention**: a durable pre-action pause for CAPTCHA, non-email
  MFA, unusual consent, site restrictions, or unsupported controls. The
  dedicated browser stays human-ready and only a scoped `Riprendi` continues
  the existing operation.
- **Uncertain submission**: a durable post-attempt state backed by a read-only
  ATS/mailbox inspection. It forbids another submit until the human resolves
  the ambiguity as not submitted and then grants a fresh `Invia` authorization.
- **Local application package**: owner-only, reconstructable record of one exact
  application, including its vacancy, brief, artifacts, answers, audit trail,
  and submission evidence.
- **Application package outbox**: durable claim stored atomically with every
  application mutation. It is cleared only after the matching private package
  and indexes publish, and remains recoverable after publication failure.
- **Application index**: deliberately non-secret Markdown and CSV projection of
  application identity, role, location, lifecycle, status, and update time.
- **Targeted-company seed**: the losslessly preserved existing company list.
  Each changed hash becomes an append-only revision: newly parsed names merge
  into the imported candidate set while prior names and source hashes remain
  retained. Seed membership alone never activates monitoring; current evidence
  and explicit approval are still required.
- **Verified company addition**: a proposed company version carrying current,
  sourced ownership and sponsorship evidence. No more than five are surfaced
  in a rolling 14-day window, and active monitoring starts only after approval
  of that exact evidence version through a short-lived, random authorization
  bound to the intended Telegram actor and chat.
- **Job-alert proposal**: a source, query, location and expected-coverage bundle
  shown for human confirmation. It creates no external subscription by itself.
- **Subscription intent**: the durable, idempotency-keyed record written after
  exact Telegram confirmation and before an external subscription attempt. A
  definitively failed attempt may be proposed again as a fresh revision with
  the same semantic idempotency key. An ambiguous transport outcome remains
  `uncertain` and cannot be retried until reconciliation.
- **Dedicated career mailbox**: the Gmail identity configured by the operator
  for discovery and correspondence ingest. OAuth and every ingest fail closed
  unless the authenticated profile matches that runtime configuration and the
  only granted Gmail permission is `gmail.readonly`. No fallback mailbox is
  used.
