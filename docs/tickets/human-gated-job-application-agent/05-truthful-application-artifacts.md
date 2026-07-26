# 05 — Generate truthful CV and cover-letter artifacts

> GitHub issue: [#6](https://github.com/example-org/job-agent/issues/6)
> Status: `ready-for-agent`
> Parent: [#1](https://github.com/example-org/job-agent/issues/1)

## Parent

#1

## What to build

After preparation approval, reuse the persisted requirements-to-evidence matrix from deep grading to generate a versioned tailored CV and cover letter in GitHub Actions from the verified official description and a read-only evidence bank, while preserving traceability to candidate-approved professional evidence. Publish only an authenticated encrypted package and verify it locally before enabling the next gate.

## Acceptance criteria

- [ ] Tailoring begins only after Prepara candidatura for a verified opportunity.
- [ ] Tailoring consumes the persisted deep-grading matrix and does not make a second requirements-analysis LLM call.
- [ ] The canonical CV and evidence bank are read-only to the agent and can be reloaded with Rileggi CV master.
- [ ] Research, computer-vision/applied-ML, and agentic-AI families select from the same verified evidence.
- [ ] Both CV and cover letter are generated and versioned together for every approved role.
- [ ] Every material professional claim is traceable to approved evidence.
- [ ] Selection, ordering, compression, and truthful rephrasing are allowed; unsupported skills or impact are rejected.
- [ ] Insufficient truthful fit produces an explained stretch decision instead of fabricated material.
- [ ] Changing an artifact invalidates later approvals tied to its previous version.
- [ ] `Prepara candidatura` dispatches only `application_id` and the canonical `official_vacancy_version`.
- [ ] Hosted generation restores the exact candidate-safe grading snapshot from authoritative Actions state and refuses mismatches.
- [ ] The public Actions artifact contains only an AES-256-GCM encrypted package; plaintext PDFs and evidence are removed from the runner.
- [ ] The Mac accepts only a newly produced package with matching repository, workflow, branch, application, vacancy, manifest, and file hashes.
- [ ] The shared handoff key exists only in GitHub Secrets and macOS Keychain.

## Blocked by

- #4 — Verify opportunities and expose Telegram decisions
