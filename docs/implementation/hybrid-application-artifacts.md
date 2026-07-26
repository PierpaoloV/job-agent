# Hybrid application-artifact implementation

## Outcome

`Prepara candidatura` sends only an approved application ID and the exact
official vacancy version to GitHub Actions. The hosted runtime restores the
authoritative candidate-safe snapshot, generates and audits CV plus cover
letter, then publishes only an authenticated encrypted package. The Mac
durably records the workflow-run baseline, dispatches without blocking
Telegram, and binds the request only when exactly one new run appears in the
persisted workflow/branch/event scope. A later worker cycle downloads only that
run's artifact, decrypts and verifies it with a Keychain-held key, and enables
`Compila` only after the local files are intact.

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
