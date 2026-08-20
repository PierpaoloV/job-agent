# Hybrid application-artifact implementation

## Outcome

`Prepara candidatura` sends only an approved application ID and the exact
official vacancy version to GitHub Actions. The hosted runtime restores the
authoritative candidate-safe snapshot, generates and audits CV plus cover
letter, then publishes only an authenticated encrypted package. In hosted
mode, GitHub Actions durably arms and sends the completion notice directly;
artifact preparation and its Telegram acknowledgement do not depend on an
always-on Mac. A trusted local consumer remains optional: it can download,
decrypt, and verify a bound package before enabling the non-hosted `Compila`
gate.

The generation contract sends the professional text extracted from the
candidate-owned canonical CV together with the approved evidence bank. Sonnet
selects, but may not invent or paraphrase, contact details, profile excerpts,
one to three relevant roles, education, and at least one relevant publication.
Roles and education are selected as contiguous source blocks whose leading
field pairs are validated together, preventing a role from being combined with
another employer or date. The runtime validates every selected excerpt against
the master source, requires an email, substantial summary, experience,
education, technical skills, publications, and approved tailored evidence,
then renders a styled ATS-readable document capped at two pages.
Before the model call, the canonical projection removes health, demographic,
identity-document, secret-credential, ATS-answer, and personal-interest lines.
Every selected material excerpt becomes an exact, deterministic canonical-CV
evidence record and claim trace. The orchestration service independently
rebuilds approval from the exact CV version; a generator cannot approve its own
record and source membership never bypasses the audit.
The letter must name a role copied from the official vacancy, cite one to three
persisted requirement IDs, include the approved evidence tied to each selected
requirement, and use one or two substantial master-CV paragraphs with at least
one first-person passage. At least one selected approved evidence record must
be a technical skill.

## Safety invariants

- No health, demographic, identity-document, credential, or ATS-answer data
  enters the hosted preparation snapshot or model prompt.
- Application ID and official vacancy version are one canonical identity and
  must match the authoritative snapshot.
- A crash after a possible repository dispatch never causes a blind second
  generation call.
- Zero runs after the discovery deadline or more than one candidate run fail
  closed and require explicit resolution.
- Public Actions artifacts contain no plaintext CV, cover letter, or evidence.
- Hosted completion delivery enters durable `sending` state before Telegram;
  an ambiguous outcome is never retried automatically.
- The application reaches `CV pronto` only after encrypted publication, and
  that durable transition names the exact vacancy, package hash, and Actions
  run. A definitive Telegram rejection may replace it on a controlled retry;
  an uncertain or possibly sent delivery cannot.
- Repository, workflow, branch, application, vacancy, manifest, and file hashes
  are authenticated before installation.
- Installed files are revalidated as owner-only, regular, non-symlinked files
  with exact hashes before `Compila` is exposed.
- GitHub and handoff credentials exist only in GitHub Secrets and macOS
  Keychain.

## Ticket graph

1. Shared application and handoff identity contract — complete.
2. Authoritative hosted preparation and encrypted Actions output — complete.
3. Durable local dispatch, download, decrypt, and recovery — complete.
4. Keychain-backed production composition — complete.
5. End-to-end audit — complete; it identified tickets 6–8.
6. Full-identity authoritative preparation storage — complete.
7. Owner-only artifact revalidation at the fill gate — complete.
8. Run-correlated asynchronous dispatch reconciliation — complete.
9. Production entrypoint with real ATS/vacancy capabilities — blocked by 8 and
   the live-browser ticket; intentionally blocked.
10. Final docs synchronization, review, and commit — blocked by 17 and 18;
    ticket 9 is
    tracked honestly as the remaining production-release edge; complete.
11. Durable preparation-completion notification — blocked by 8 and required
    before the final audit because the product spec promises `CV completo`;
    complete.
12. Identity-bound run metadata and conservative dispatch outcomes — review
    correction; complete.
13. Recoverable pre-send Telegram completion claim — review correction;
    complete.
14. Explicit safe preparation-resolution action — blocked by 12 and 13;
    complete.
15. Fair terminal-notification reconciliation and complete active GitHub
    statuses — final review correction; complete.
16. Reissue an expired preparation-retry control after a fresh safety check —
    final review correction; complete.
17. Recover a replayed stale retry only before the replacement send boundary —
    final review correction; complete.
18. Persist terminal-notification fairness cursors across worker restarts —
    final review hardening; complete.

Tickets 2 and 3 form the parallel frontier after ticket 1.
