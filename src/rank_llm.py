"""LLM-based job ranking using Claude (Anthropic)."""
import os, json, re, pathlib
import anthropic
import yaml

PREFS_PATH = pathlib.Path(__file__).parent.parent / "preferences.yaml"
RESUME_PATH = pathlib.Path(__file__).parent.parent / "resume.md"

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY secret is not set in GitHub Actions — add it under Settings → Secrets → Actions")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _load_context():
    prefs = yaml.safe_load(PREFS_PATH.read_text())
    resume = RESUME_PATH.read_text()
    return prefs, resume


def _check_red_flags(job: dict, prefs: dict) -> list[str]:
    text = f"{job.get('title','')} {job.get('company','')} {job.get('location','')} {job.get('description','')}".lower()
    return [kw for kw in prefs.get("red_flag_keywords", []) if kw.lower() in text]


def score_job(job: dict) -> dict:
    prefs, resume = _load_context()
    client = _get_client()

    red_flags = _check_red_flags(job, prefs)
    if red_flags:
        return {**job, "score": 0.0, "rationale": f"Red flags: {', '.join(red_flags)}", "red_flags": red_flags}

    target_roles = ", ".join(prefs["target_roles"])
    locations = ", ".join(prefs["locations"]["preferred"] + prefs["locations"]["acceptable"])
    italy_min = prefs["salary"]["italy_min_eur"]
    elsewhere_min = prefs["salary"]["elsewhere_min_eur"]

    # System prompt is stable across all jobs in a run — cache it to save ~90% on tokens from job 2 onward
    system_prompt = f"""You are evaluating job fit for a candidate. Return JSON only.

## Candidate Resume
{resume}

## Target Roles
{target_roles}

## Preferred Locations
{locations}

## Salary Requirements
Minimum salary:
- Italy roles: €{italy_min:,}/year
- All other roles: €{elsewhere_min:,}/year

## Scoring Rules
Score from 0.0 to 1.0 using these criteria:

1. Role match (40%)
- Strong overlap with candidate skills and target roles: high
- Partial overlap: medium
- Little overlap: low

2. Seniority match (20%)
- Roles requiring much more leadership/management or many more years than candidate has: penalize
- Mid-level IC roles aligned with profile: good match

3. Location / remote compatibility (20%)
- Must be compatible with Italy, Netherlands, EU remote, or USA remote from Europe only
- If relocation is required or remote geography is incompatible, mark as not compatible

4. Domain / technical relevance (10%)
- Digital pathology, medical imaging, computer vision, ML systems, LLM tooling are positives
- Adjacent ML roles are acceptable if technical fit is strong

5. Salary fit (10%)
- If salary is explicitly below minimum, strongly penalize
- If salary is missing, do not assume it is acceptable; mark as unknown in rationale and apply a small penalty

## Hard Rules
- If location is clearly incompatible, "location_ok" must be false and score should usually be <= 0.4
- If the role is primarily managerial/executive and not hands-on, role_match should usually be "weak" or "moderate"
- If the role requires relocation outside preferred locations, score should usually be <= 0.3
- A score >= 0.8 means a genuinely strong match worth serious consideration
- A score between 0.6 and 0.79 means plausible but imperfect
- A score below 0.6 means weak or constrained fit

## Missing Information
If salary, remote policy, or seniority are unclear, do not invent details. State uncertainty briefly in the rationale and score conservatively.

Return JSON with keys:
- "score": float 0.0–1.0
- "rationale": 2–3 sentences
- "location_ok": true/false
- "role_match": "strong" | "moderate" | "weak"
"""

    user_prompt = f"""## Job Listing
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Location: {job.get('location', 'N/A')}
Source: {job.get('source', 'N/A')}
URL: {job.get('url', 'N/A')}

Score this job for the candidate above.
"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = response.content[0].text.strip()
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            result = json.loads(match.group(0)) if match else {}

        return {
            **job,
            "score": float(result.get("score", 0.0)),
            "rationale": result.get("rationale", ""),
            "location_ok": result.get("location_ok", True),
            "role_match": result.get("role_match", "unknown"),
            "red_flags": [],
        }
    except Exception as e:
        print(f"LLM scoring failed for {job.get('title')}: {e}")
        return {**job, "score": 0.0, "rationale": f"Error: {e}", "red_flags": []}


def rank_jobs(jobs: list[dict], top_n: int = 10) -> list[dict]:
    print(f"Scoring {len(jobs)} jobs with LLM...")
    scored = [score_job(job) for job in jobs]
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    top = [j for j in ranked if j["score"] > 0.3][:top_n]
    print(f"Top {len(top)} jobs above threshold (score > 0.3)")
    return top
