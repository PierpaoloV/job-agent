# Resume Context Template

This file is intentionally non-sensitive.

For local runs, create an ignored `resume.md` file in the repo root with the
private resume/profile context used by the ranker.

For GitHub Actions, add a repository secret named `JOB_AGENT_RESUME_MD` whose
value is the full Markdown resume context. The workflow passes that secret to
the ranker at runtime, so the private resume does not need to be committed.
