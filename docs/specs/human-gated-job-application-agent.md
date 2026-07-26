# Human-gated job application agent

> Original tracker issue: #1
> Tracker: GitHub Issues (`ready-for-agent`); this file is the synchronized local mirror.

## Problem Statement

A candidate needs to find and pursue high-quality roles
without turning job search into a second full-time job. The current agent only
parses job-alert emails, ranks snippets, sends a Telegram digest, and records a
small applied/not-applied history. It does not verify the official vacancy,
evaluate wealth potential and immigration constraints, tailor application
materials from a controlled evidence base, complete ATS forms, submit after a
specific approval, or monitor the result.

The desired search may span multiple locations and work arrangements. That
creates materially different cost-of-living, work-authorization, sponsorship,
language, ownership, and compensation questions. A single salary floor or
generic fit score could discard valuable opportunities and obscure important
risks. At the same time, fully automatic application submission would create
unacceptable risks: fabricated claims, wrong eligibility answers, duplicate
submissions, disclosure of sensitive information, and unintended external
actions.

The candidate therefore needs one system that automates repetitive discovery,
analysis, document preparation, form filling, and status tracking while keeping
explicit human gates around every consequential decision. The agent must remain
truthful, protect sensitive data, explain its rankings, preserve application
quality, and never submit an application merely because an earlier step was
approved.

## Solution

Extend the existing job agent into a local-first, Telegram-controlled
application workflow. It will discover roles daily, verify them against official
sources, rank them as a portfolio, and send a digest of at most ten roles every
three days. Rare top-tier roles and imminent deadlines will generate immediate
alerts. Each role card will explain fit, gaps, compensation, wealth potential,
language, immigration, ownership, and risk, with controls to prepare, discard,
or inspect the role in more detail.

Remote discovery will use two explicitly separated GitHub Actions jobs. The
first job ingests career-email alerts, applies source-specific preprocessing,
normalization, deduplication, deterministic policy filters, and local NLP
screening without any LLM credential or call. It emits a versioned shortlist
artifact. A dependent deep-grading job runs only when that shortlist is not
empty and makes exactly one LLM call for each shortlisted role with a verified
official description. That call produces both the portfolio evaluation and the
requirements-to-evidence matrix later reused for tailoring.

An approved role will progress through an explicit application state machine.
`Prepara candidatura` dispatches only the application ID and exact official
vacancy version to GitHub Actions. The hosted job restores the authoritative
grading snapshot, generates and audits the tailored CV and cover letter from
professional evidence, and publishes only an authenticated encrypted package.
The Mac accepts only a newly produced package with the expected identity,
authority, and hashes, decrypts it into owner-only storage, and only then exposes
`Compila`. `Compila` authorizes the local browser worker to attach those
artifacts and fill the ATS, but not submit it. `Invia` authorizes one attempt to
submit that exact, revalidated application. Every approval is scoped to a
particular application version, expires when the prepared application becomes
stale, and is recorded in the local audit history.

The system will separate professional evidence that may be used for tailoring
from sensitive form data that must remain local. It will pause for missing
answers, salary expectations, CAPTCHA, non-email MFA, unusual consent, active
duplicate applications, and uncertain submission outcomes. After submission it
will store a complete local application package, monitor the dedicated career
mailbox for unambiguous status changes, and ask the candidate to classify ambiguous
messages. It may draft communications, but it will never send them
automatically.

## User Stories

1. As a candidate, I want the agent to fetch new jobs on my configured schedule, so that I do not miss newly opened roles.
2. As a candidate, I want a ranked digest on my configured cadence, so that I can review opportunities in efficient batches.
3. As a candidate, I want each regular digest limited by a configured maximum, so that reviewing it remains manageable.
4. As a candidate, I want to reveal additional roles on demand, so that the digest limit does not hide the remainder of the funnel.
5. As a candidate, I want immediate alerts for genuinely top-tier roles, so that rare opportunities are not delayed until the next digest.
6. As a candidate, I want immediate alerts for imminent deadlines, so that a strong role does not close before I can act.
7. As a candidate, I want official career pages and ATS records treated as the source of truth, so that I do not apply from incomplete aggregator data.
8. As a candidate, I want job-alert email, LinkedIn, and aggregators used as discovery leads, so that the agent can find roles across several channels.
9. As a candidate, I want the full official job description retrieved before tailoring begins, so that my application responds to the actual requirements.
10. As a candidate, I want the agent to stop when the official description is unavailable or ambiguous, so that it never tailors from an email snippet alone.
11. As a candidate, I want locations ranked from my private preferences, so that the search reflects my current priorities.
12. As a candidate, I want location preferences treated as ranking inputs, so that an excellent lower-priority role can outrank a weak preferred-location role.
13. As a candidate, I want my preferred role families ranked from private configuration, so that the public repository does not encode my career profile.
14. As a candidate, I want adjacent roles to remain competitive when their overall fit is stronger, so that preferences do not become unintended hard filters.
15. As a candidate, I want company-stage preferences configurable, so that both established companies and startups can be considered.
16. As a candidate, I want exceptional applied or production roles to remain competitive with research roles when configured, so that one preference does not close off better opportunities.
17. As a candidate, I want startups included, so that high-growth opportunities remain in scope.
18. As a candidate, I want startup stability and financial risk labelled clearly, so that I can evaluate the downside separately from the upside.
19. As a candidate, I want no global salary-floor rejection, so that high-quality roles with unpublished compensation remain visible.
20. As a candidate, I want unpublished compensation displayed as unknown, so that benchmarks are not presented as facts about the offer.
21. As a candidate, I want a role-specific wealth-potential assessment, so that I can compare disposable income and savings rather than gross salary alone.
22. As a candidate, I want base cash evaluated independently from bonus and equity, so that uncertain upside does not make my living and investment plan appear viable.
23. As a candidate, I want bonus and equity shown as separately risky upside, so that I can decide whether to invest them without depending on them.
24. As a candidate, I want cost of living, tax context, health insurance, housing, and travel friction considered, so that location comparisons reflect real life.
25. As a candidate, I want missing salary information to produce a suggested recruiter question, so that I know what to clarify later.
26. As a candidate, I want the agent to ask me before entering a mandatory salary expectation, so that it never invents or prematurely anchors my answer.
27. As a candidate, I want the salary question to include the published range and role-specific benchmarks, so that I can answer with useful context.
28. As a candidate, I want work authorization answered from my private configured status and the exact form wording, so that a generic sponsorship answer does not misrepresent my eligibility.
29. As a candidate, I want roles requiring immigration support marked sponsorship yes, no, or not stated with source and date, so that immigration risk is explicit.
30. As a candidate, I want roles blocked by configured work-authorization constraints placed in a visa-obstacle section, so that they remain visible without triggering wasted tailoring work.
31. As a candidate, I want to promote a visa-obstacle role manually, so that I retain the option to pursue an exceptional opportunity.
32. As a candidate, I want only my configured application languages considered, so that I am not routed into processes I cannot complete honestly.
33. As a candidate, I want unconfigured language requirements rejected, so that the agent never claims languages I do not speak.
34. As a candidate, I want willingness to relocate represented from private configuration, so that in-person roles are not discarded incorrectly.
35. As a candidate, I want my privately configured availability date used as the standard start date, so that applications reflect my plans without disclosing private details.
36. As a candidate, I want my configured company-ownership exclusions applied, so that the search respects my boundaries without embedding them in public source code.
37. As a candidate, I want ownership decisions based on current evidence, so that acquisitions or ownership changes can alter a prior decision.
38. As a candidate, I want sector preferences evaluated independently from ownership preferences, so that unrelated rules are not conflated.
39. As a candidate, I want each role card to show company, title, location, modality, source, and freshness, so that I can orient quickly.
40. As a candidate, I want each role card to explain why the role matches now, so that the ranking is inspectable.
41. As a candidate, I want each role card to show important gaps and missing requirements, so that stretch applications are deliberate.
42. As a candidate, I want each role card to show compensation and wealth-potential confidence, so that unknowns are visible.
43. As a candidate, I want each role card to show language, immigration, ownership, and material risks, so that I can decide without opening several pages.
44. As a candidate, I want a clear rank explanation, so that score order is not an opaque LLM judgement.
45. As a candidate, I want a `Dimmi di più` action, so that I can inspect the complete official description, requirement analysis, sources, and risks.
46. As a candidate, I want `Dimmi di più` to avoid tailoring and browser work, so that requesting information has no external side effects.
47. As a candidate, I want `Prepara candidatura` to begin document preparation, so that a thumbs-up has one predictable meaning.
48. As a candidate, I want a discard action with a reason, so that future ranking can learn from the context of my decision.
49. As a candidate, I want discard reasons treated as conditional rather than permanent blocks, so that a materially improved role can reappear.
50. As a candidate, I want similar discarded roles suppressed until a material property changes, so that I do not repeatedly review the same proposition.
51. As a candidate, I want material changes to include role, team, location, seniority, salary, requirements, ownership, and sponsorship, so that reappearance is explainable.
52. As a candidate, I want the active preparation workload normally held to four to six applications, so that application quality stays high.
53. As a candidate, I want the agent to exceed the workload target only for a top-tier role or deadline and explain why, so that urgency does not silently lower quality.
54. As a candidate, I want a manually approved role revalidated against the official vacancy before preparation, so that closed or changed roles do not consume work.
55. As a candidate, I want a tailored CV and cover letter for every approved role, so that each application has complete materials.
56. As a candidate, I want the cover letter uploaded whenever the ATS supports it, so that prepared work is actually used.
57. As a candidate, I want tailoring limited to selection, reordering, compression, and truthful rephrasing of verified evidence, so that no achievement is fabricated.
58. As a candidate, I want the agent to mark insufficient fit and explain the gaps, so that I can consciously choose whether to make a stretch application.
59. As a candidate, I want only me to edit the canonical CV and evidence bank, so that the source of professional truth remains under my control.
60. As a candidate, I want a Telegram command to reload my master CV, so that later applications use my newest manually curated version.
61. As a candidate, I want my configured CV families generated from the same evidence, so that specialization does not fragment truth.
62. As a candidate, I want a completion message when the CV and cover letter are ready, so that I know when the next gate is available.
63. As a candidate, I want `Compila` to authorize ATS form filling and attachment upload only, so that it cannot accidentally submit the application.
64. As a candidate, I want a dedicated browser profile for applications, so that career sessions remain separate from personal browsing.
65. As a candidate, I want ATS accounts created with my dedicated career Gmail when necessary, so that application identities stay consistent.
66. As a candidate, I want generated ATS passwords stored in macOS Keychain, so that credentials are not written to source files or logs.
67. As a candidate, I want unknown mandatory answers sent to me on Telegram, so that the agent never guesses consequential information.
68. As a candidate, I want to mark an answer as one-use or save it as a default, so that I control whether it is reused.
69. As a candidate, I want saved answers matched to the exact form question semantics, so that a superficially similar question does not receive a wrong response.
70. As a candidate, I want my standard demographic answers available to the local form filler, so that voluntary forms can be completed consistently.
71. As a candidate, I want standardized voluntary disability self-identification answered from my private saved choice when applicable, so that the selected default is respected.
72. As a candidate, I want medical diagnoses excluded from free text, CVs, and cover letters, so that unnecessary health detail is not disclosed.
73. As a candidate, I want demographic, disability, identity-document, and exact form data kept out of LLM prompts, so that sensitive information remains local.
74. As a candidate, I want a pre-submit Telegram summary of the job, attachments, and principal answers, so that I can inspect the exact application before sending.
75. As a candidate, I want `Invia` to authorize only that exact application version, so that later changes require fresh approval.
76. As a candidate, I want the vacancy rechecked immediately before submission, so that the agent does not submit to a closed or materially changed role.
77. As a candidate, I want CAPTCHA, non-email MFA, and unusual consent to pause the workflow, so that the agent never bypasses controls or accepts unexpected terms.
78. As a candidate, I want the dedicated browser left ready when intervention is required, so that I can resolve the blocker efficiently.
79. As a candidate, I want submission to resume only after I issue `Riprendi`, so that completing a challenge does not imply broader authorization.
80. As a candidate, I want one controlled submission attempt after `Invia`, so that network uncertainty cannot cause blind duplicate retries.
81. As a candidate, I want an uncertain submission outcome investigated before any retry, so that duplicates are prevented.
82. As a candidate, I want an unresolved outcome reported as uncertain and handed back to me, so that ambiguity is not reported as success.
83. As a candidate, I want prepared applications to expire after 72 hours, so that stale approvals and materials are not used indefinitely.
84. As a candidate, I want a reminder after 48 hours and deadline-aware prioritization, so that approved work does not quietly expire.
85. As a candidate, I want reopened roles proposed again with the earlier application and detected differences, so that reapplication is informed.
86. As a candidate, I want an active ATS application shown before sending, so that a duplicate is never submitted silently.
87. As a candidate, I want no references supplied by default, so that contacts are not disclosed without a later decision.
88. As a candidate, I want possible referrals flagged and outreach drafted, so that I can use my network without automatic contact.
89. As a candidate, I want recruiter or hiring-manager messages summarized and draft replies prepared, so that correspondence is easier to manage.
90. As a candidate, I want all outbound messages to require me to send them, so that the agent never contacts people automatically.
91. As a candidate, I want each submitted application stored as a complete local package, so that I can reconstruct exactly what was sent.
92. As a candidate, I want the package to include the official description snapshot, brief, CV, cover letter, answers, timestamps, states, and evidence of submission, so that the audit record is complete.
93. As a candidate, I want a central Markdown and CSV application index, so that applications can be browsed by humans and processed by tools.
94. As a candidate, I want sensitive reports ignored by Git and public synchronization, so that exact application data remains private.
95. As a candidate, I want unambiguous receipt, rejection, and interview emails to update application status automatically, so that the tracker stays current.
96. As a candidate, I want ambiguous correspondence sent to me for classification, so that the agent does not infer the wrong status.
97. As a candidate, I want the agent to propose job-alert subscriptions on Telegram, so that monitoring coverage can expand deliberately.
98. As a candidate, I want job-alert subscriptions to require explicit confirmation, so that the agent does not create unwanted external subscriptions.
99. As a candidate, I want the existing company watchlist used as the starting point, so that prior research is preserved.
100. As a candidate, I want at most five verified new companies proposed every two weeks, so that watchlist expansion remains reviewable.
101. As a candidate, I want a company added to monitoring only after my approval, so that the watchlist reflects intentional choices.
102. As a candidate, I want observable market facts used instead of invented saturation scores, so that ranking uncertainty is honest.
103. As a candidate, I want scheduled discovery able to run remotely while sensitive and interactive work runs on my Mac, so that the system is available without exporting local secrets.
104. As a candidate, I want the local worker to start automatically at macOS login, so that the always-on Mac recovers after restarts.
105. As a candidate, I want local logs and an explicit stop mechanism, so that I can diagnose and control the worker.
106. As a candidate, I want Telegram commands for pause, resume, and status, so that I can control the system away from the Mac.
107. As a candidate, I want pause to stop fetches, browser actions, and pending work immediately, so that it is a real safety control.
108. As a maintainer, I want every workflow transition persisted, so that crashes and restarts do not lose the current application state.
109. As a maintainer, I want external actions to be idempotent or guarded by durable intent records, so that retries do not duplicate subscriptions or submissions.
110. As a maintainer, I want secrets and sensitive fields redacted from logs, Telegram, model prompts, and exceptions, so that operational diagnostics do not leak private data.
111. As a maintainer, I want each email source preprocessed through a source-specific adapter, so that LinkedIn, Indeed, Glassdoor, Welcome to the Jungle, and fallback formats produce the same normalized opportunity schema.
112. As a candidate, I want deterministic filters and local NLP to perform initial screening without an LLM, so that obvious incompatibilities and weak matches do not consume model calls.
113. As a candidate, I want low-scoring screened roles retained outside the main shortlist, so that a local screening mistake does not become a permanent invisible rejection.
114. As a maintainer, I want the no-LLM screening job to emit a versioned shortlist artifact, so that deep grading has an auditable and reproducible input.
115. As a candidate, I want exactly one deep-grading call per verified shortlisted role, so that ranking and requirement analysis are consistent and do not duplicate model work.
116. As a candidate, I want blocked official-page retrieval marked `needs_local_fetch`, so that the agent never grades or tailors from an email snippet while still allowing the Mac to recover the opportunity.
117. As a candidate, I want `Prepara candidatura` to send only an application ID and exact vacancy version to the hosted runtime, so that the command cannot leak local form answers.
118. As a candidate, I want GitHub Actions to publish only an encrypted CV-and-cover-letter package, so that a public repository never exposes application documents.
119. As a candidate, I want the Mac to reject stale, mismatched, tampered, or wrongly authored packages before `Compila` becomes available, so that only the requested artifacts enter the ATS.
120. As a candidate, I want the handoff key stored in both GitHub Secrets and macOS Keychain, so that it is never committed or sent through Telegram.

## Implementation Decisions

- Preserve the existing email parsing and deduplication capabilities, but place
  them behind a broader opportunity-discovery boundary rather than letting the
  current linear script define the product workflow.
- Run remote discovery as two dependent GitHub Actions jobs:
  `ingest-and-screen` and `deep-grade`. The first job must not receive or load an
  LLM API credential. The second receives a model credential only when a
  non-empty shortlist artifact exists.
- Give LinkedIn, Indeed, Glassdoor, Welcome to the Jungle, and known additional
  sources explicit preprocessing adapters. Each adapter decodes its email
  layout, resolves or strips tracking links, derives a stable external job ID,
  and emits the same normalized lead contract. Unknown sources use a labelled,
  lower-confidence fallback rather than pretending to be fully supported.
- Keep `discovered_at`, email date, and the vacancy's actual publication date as
  distinct fields. Ingestion time must not be represented as the employer's
  posting time.
- Perform deduplication, explicit policy checks, keyword/taxonomy matching, and
  local semantic NLP screening before any LLM call. The local score controls
  shortlist priority but cannot create an irreversible silent rejection;
  overflow and sampled low-score records remain auditable.
- Publish the no-LLM stage as a versioned, non-sensitive shortlist artifact with
  normalized records, source confidence, screening features, reasons, and
  stable identifiers. Downstream processing must reject incompatible artifact
  versions rather than guessing their meaning.
- Retrieve and snapshot the full official vacancy before deep grading. If a
  GitHub-hosted runner cannot retrieve it reliably, mark the opportunity
  `needs_local_fetch`, make no grading call from the email snippet, and hand the
  record to the Mac for official-page retrieval.
- Make exactly one deep-grading LLM call for each verified role admitted to the
  shortlist. The input is the official vacancy plus a sanitized professional
  grading profile; it excludes the full private candidate profile, health and
  demographic data, identity documents, credentials, and exact ATS answers.
- Require the deep-grading response to contain the complete portfolio
  evaluation, rank explanation, gaps, and a structured requirements-to-evidence
  matrix. Persist this result and reuse the matrix after `Prepara candidatura`;
  do not run a separate requirements-analysis model call during tailoring.
- Persist a candidate-safe hosted-preparation snapshot for each exact verified
  vacancy version in authoritative Actions state. `Prepara candidatura`
  dispatches only `application_id` and `official_vacancy_version`; the hosted
  job must refuse absent or mismatched state rather than accepting role text
  from the callback.
- Generate and audit CV plus cover letter together in GitHub Actions. Package
  the resulting PDFs, artifact metadata, evidence-source version, and hashes
  with AES-256-GCM. Upload only the encrypted package with three-day retention;
  remove runner plaintext after the upload step.
- Use one shared random handoff key stored as
  `JOB_AGENT_ARTIFACT_HANDOFF_KEY` in GitHub Secrets and under
  `job-agent.artifact-handoff` in macOS Keychain. The local worker records
  pre-dispatch artifact IDs, accepts only a later artifact, verifies repository,
  workflow, branch, application ID, vacancy version, manifest, and file hashes,
  then installs the PDFs into owner-only local storage.
- Introduce a durable application-workflow coordinator as the central deep
  module. It owns state transitions, authorization gates, expiry, retry policy,
  and recovery. Telegram handlers, schedulers, browser automation, and mailbox
  monitoring issue commands to this coordinator instead of mutating application
  state independently.
- Use the fixed lifecycle vocabulary: `scoperta`, `proposta`, `scartata`,
  `approvata`, `CV pronto`, `compilazione in corso`, `pronta da inviare`,
  `inviata`, `colloquio`, `rifiutata`, and `chiusa`. Operational substates such
  as intervention required, uncertain outcome, and expired preparation annotate
  the lifecycle without falsely advancing it.
- Model approvals as durable, scoped records rather than booleans. Each approval
  identifies the application, action, artifact/form version, actor, timestamp,
  and expiry. Preparation approval cannot satisfy fill authorization, and fill
  authorization cannot satisfy submission authorization.
- Separate the system into discovery, verification, evaluation, decision,
  preparation, form filling, submission, correspondence monitoring, reporting,
  and scheduling capabilities with explicit interfaces between them.
- Define external integrations as adapters: Gmail and official job sources,
  Telegram, model provider, document renderer, browser/ATS controller, macOS
  Keychain, scheduler, and persistence. Domain policy must not depend directly
  on any specific vendor library.
- Store an immutable official-description snapshot and retrieval metadata before
  tailoring. A snippet or aggregator listing may create an opportunity, but it
  cannot create an application brief.
- Give each opportunity a stable identity plus a material-change fingerprint.
  The fingerprint covers company, role, team, location, modality, seniority,
  compensation, requirements, ownership, sponsorship, and official job ID when
  available. This supports deduplication, conditional discard suppression, and
  explainable resurfacing.
- Treat rankings as structured evaluations, not a single opaque number. Persist
  fit, research preference, geography, compensation confidence, wealth
  potential, language, immigration, ownership, freshness, deadline, risk, and
  explanation. Missing evidence remains unknown rather than receiving an
  invented value.
- Implement geographic preference as a scoring portfolio policy, not a hard
  filter, except for explicit language, ownership, and other agreed exclusions.
- Implement top-tier alerts from a configurable policy combining exceptional
  overall evaluation with priority-company/team status and real fit. Missing
  compensation alone cannot prevent a top-tier alert.
- Keep compensation facts, market benchmarks, and inferred wealth potential as
  distinct data with source, date, currency, confidence, and assumptions.
  Equity and bonus are displayed separately from base-cash sustainability.
- Determine work authorization from private candidate configuration, the
  jurisdiction, and exact question text. Sponsorship evidence records yes, no,
  or not stated together with source and verification date.
- Resolve company ownership through dated evidence. The exclusion is based on
  configured current headquarters/control classifications and is re-evaluated
  after a material ownership change; sector rules are configured independently.
- Implement Telegram actions with short-lived, application-scoped callback
  tokens. Replayed, expired, or mismatched callbacks produce no external action
  and return a clear status message.
- Generate a concise role card and an expanded role view from the same persisted
  evaluation so that `Dimmi di più` remains read-only and consistent with the
  digest.
- Maintain a configured active-preparation capacity. The coordinator may admit
  an exception only when the configured priority/deadline policy applies and
  must record the explanation.
- Treat the canonical CV and evidence bank as read-only inputs owned by
  the candidate. Reloading rebuilds the agent's read model but cannot modify source
  evidence.
- Tailoring receives only the approved opportunity description and permitted
  professional evidence. It may select, reorder, compress, and truthfully
  rephrase; every material claim in the output must be traceable to evidence.
- Generate both CV and cover-letter artifacts for each approved opportunity and
  version them together. A change to either artifact invalidates any existing
  fill or submission authorization for the older version.
- Keep reusable professional evidence separate from local form answers and
  sensitive profile data. Health, demographic, identity-document, veteran, and
  exact submitted-answer data must never cross the model boundary.
- Resolve mandatory unknown answers through Telegram with explicit one-use or
  save-as-default scope. Defaults are keyed to normalized question semantics and
  remain reviewable locally.
- Always require a new Telegram answer for mandatory salary expectations. The
  question includes verified published compensation and dated benchmarks but
  does not preselect a number.
- Apply the agreed local demographic defaults only to appropriate standardized
  voluntary forms. Never insert diagnosis details in free text or
  professional artifacts.
- Run browser automation only in the dedicated Job Applications profile on the
  Mac. Account creation belongs to the fill phase; credentials are generated
  locally and stored through Keychain.
- Treat CAPTCHA, non-email MFA, unusual consent, unsupported fields, and site
  restrictions as explicit intervention states. The browser remains available
  for the human, and only a later resume command continues the workflow.
- Build a pre-submit manifest containing the verified role, artifact hashes,
  principal answers, unresolved warnings, and vacancy freshness. `Invia`
  authorizes exactly this manifest.
- Before submission, revalidate vacancy availability and material fields. A
  changed manifest invalidates the approval and returns the application for
  review.
- Record a submission intent durably before the external click. After the click,
  collect confirmation page, confirmation identifier, email receipt, and ATS
  status where available. Never retry solely because a request timed out.
- Represent an unresolved post-click state as an uncertain outcome, retaining
  evidence and requiring human resolution before another attempt.
- Store each application as a local package with the description snapshot,
  evaluation brief, exact generated artifacts, form-answer record, lifecycle
  history, approvals, and submission evidence. Maintain a human-readable
  Markdown index and machine-readable CSV index.
- Exclude sensitive packages, credentials, tokens, browser state, and candidate
  profiles from Git and public synchronization. Logs use allowlisted fields and
  redact secrets and sensitive values before formatting errors.
- Monitor the dedicated career Gmail after submission. Only deterministic
  receipt, rejection, and interview classifications may advance status
  automatically; ambiguous correspondence creates a Telegram classification
  task.
- Allow communication drafting as a local artifact, but expose no automatic send
  operation for referrals, recruiters, hiring managers, or email replies.
- Run discovery, deep grading, and approved professional-document generation
  remotely when useful. Telegram callback authorization, decryption, Keychain,
  browser work, sensitive ATS answers, private persistence, and submission
  execute on the Mac.
- Install the local worker as a macOS login service with health status, logs,
  graceful shutdown, and durable recovery. `Pausa` is a persisted global gate
  checked before scheduled, queued, and interactive work.
- Use the privately configured dedicated career Gmail for new ATS accounts,
  subscriptions, and correspondence once the candidate connects it.
  Implementation may support an unconfigured state but must not silently fall
  back to a personal mailbox. Passwords, OAuth tokens, and recovery credentials
  must remain outside the repository and persisted reports.
- Update stale ranking preferences as part of migration. The current private
  portfolio, eligibility, and safety rules supersede legacy configuration.
- Migrate existing seen and applied records without losing history. Earlier
  applications become prior-application evidence used by duplicate and reopened
  role checks.

## Testing Decisions

- Test externally observable workflow behavior rather than private helper
  functions or specific vendor SDK calls. A good test sends commands and events
  through a public application-workflow interface, then asserts returned
  decisions, durable state, emitted notifications, prepared artifacts, and
  recorded external intents.
- Use one primary high-level seam: the application-workflow coordinator with
  in-memory or deterministic fake adapters for discovery, official-description
  retrieval, evaluation, Telegram, tailoring, document rendering, browser/ATS,
  Keychain, persistence, clock, and correspondence. This is the smallest seam
  that proves the safety gates across the complete product journey.
- The main acceptance scenario begins with a discovered opportunity and proves
  that `Prepara candidatura` retrieves and prepares without filling,
  `Compila` fills without submitting, and only a manifest-matching `Invia`
  produces one submission intent and a verified application report.
- Add high-level scenarios for a missing official description, materially
  changed vacancy, insufficient truthful fit, expired preparation, stale
  callback, capacity exception, active prior application, reopened role,
  CAPTCHA/MFA intervention, missing mandatory answer, salary expectation,
  uncertain submission, pause/resume, ambiguous correspondence, and process
  restart after each durable state.
- Assert negative behavior explicitly: no model call before role approval for
  tailoring, no browser activity from `Dimmi di più`, no form submission from
  `Compila`, no blind retry after uncertain outcome, and no outbound email or
  referral contact from any workflow state.
- Add a remote-pipeline acceptance test proving that source preprocessing,
  normalization, deduplication, deterministic filters, and local NLP complete
  with no model credential and zero model calls.
- Verify that an empty shortlist prevents the deep-grading job from starting,
  while a shortlist of N verified roles produces exactly N grading calls and N
  persisted requirement matrices.
- Add contract tests for the versioned shortlist artifact and every supported
  email-source adapter, including tracked URLs, template variations, localized
  domains, footer noise, missing fields, and stable deduplication identifiers.
- Verify that failed official-page retrieval emits `needs_local_fetch`, sends no
  snippet-derived grading request, and can resume through the same grading
  interface after successful local retrieval.
- Verify that tailoring consumes the persisted requirement matrix and never
  issues a duplicate requirements-analysis call after preparation approval.
- Add privacy-boundary tests that seed sensitive values and verify they never
  appear in model requests, Telegram role cards, general logs, exceptions, or
  committed/public artifacts while remaining available to the local form filler
  and protected application report.
- Add traceability tests that reject generated professional claims without a
  corresponding evidence reference and invalidate approvals when an artifact or
  answer manifest changes.
- Add policy-table tests for geography, language, EU work authorization, US
  sponsorship states, ownership exclusions, research preference, compensation
  unknowns, base-versus-upside treatment, conditional discards, and top-tier
  exceptions.
- Add adapter contract tests for Telegram callback encoding, official-source
  retrieval, Gmail classification, browser outcomes, Keychain operations,
  document rendering, and local report/index writing. These tests may use local
  fixtures or provider sandboxes but must never submit a real application.
- Preserve and extend the current deterministic parser tests as prior art for
  email-source canonicalization and tracking-link deduplication.
- Preserve the current Gmail credential and refresh-path tests as prior art for
  local external-service boundaries.
- Replace tests that assert stale profile preferences with tests for the new
  portfolio policy and evidence boundary.
- Use a controllable clock for daily fetches, three-day digests, biweekly company
  proposals, 48-hour reminders, 72-hour expiry, deadlines, and callback expiry.
- Verify restart safety by reconstructing the coordinator from persistent state
  after every consequential transition and ensuring that the next command has
  the same externally observable result.
- Keep all automated submission tests behind fake or non-production adapters.
  A manual acceptance checklist may exercise a disposable test form, but a real
  employer application is never a test fixture.

## Out of Scope

- Editing, enriching, or autonomously correcting the canonical CV or evidence
  bank.
- Inventing achievements, skills, production impact, domain experience,
  languages, salary expectations, eligibility answers, references, or dates.
- Automatic submission without a fresh, application-specific `Invia` approval.
- Automatic sending of referral outreach, recruiter messages, hiring-manager
  messages, or email replies.
- Automatic use of references or contact details for references.
- Bypassing CAPTCHA, MFA, bot controls, website restrictions, or terms of use.
- Applying through processes outside the privately configured languages.
- Treating a single discard, rejection, ownership result, or sponsorship result
  as a permanent silent block.
- Guaranteeing market returns, equity value, bonuses, visas, sponsorship,
  interview outcomes, or savings outcomes.
- Purchasing a car, choosing housing, executing investments, or providing tax,
  immigration, legal, or medical advice.
- Supporting every ATS from the first release. Unsupported forms must degrade to
  intervention required while retaining the prepared application.
- Filling or submitting an application from any hosted runtime.
- Creating the dedicated career Gmail account. Connecting it through OAuth and
  secret provisioning remains part of setup.

## Further Notes

- Availability, employment history, financial goals, location priorities, and
  prior applications are private runtime inputs. They must not be committed to
  the reusable repository.
- A private company watchlist and market-comparison research may be input
  sources. Current facts such as open vacancies, compensation, ownership,
  sponsorship, visa fees, and immigration policy must be reverified at the time
  they affect a decision.
- Implementation should be delivered in tracer-bullet increments that preserve
  the human gates from the first working vertical slice. A useful first slice is
  one synthetic opportunity progressing through Telegram approvals, local
  artifacts, fake ATS fill, fake submission, and an auditable report.
- This specification supersedes stale geographic, salary-floor, language, and
  relocation assumptions currently present in the agent configuration.
