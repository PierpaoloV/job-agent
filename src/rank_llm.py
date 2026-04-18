"""LLM-based job ranking — adapted from weekly_ai_paper agent."""
import os, json, re, pathlib
import openai
import yaml

PREFS_PATH = pathlib.Path(__file__).parent.parent / "preferences.yaml"
RESUME_PATH = pathlib.Path(__file__).parent.parent / "resume.md"


def _load_context():
    prefs = yaml.safe_load(PREFS_PATH.read_text())
    resume = RESUME_PATH.read_text()
    return prefs, resume


def _check_red_flags(job: dict, prefs: dict) -> list[str]:
    text = f"{job.get('title','')} {job.get('company','')} {job.get('location','')} {job.get('description','')}".lower()
    return [kw for kw in prefs.get("red_flag_keywords", []) if kw.lower() in text]


def score_job(job: dict) -> dict:
    prefs, resume = _load_context()
    openai.api_key = os.environ["OPENAI_API_KEY"]

    red_flags = _check_red_flags(job, prefs)
    if red_flags:
        return {**job, "score": 0.0, "rationale": f"Red flags: {', '.join(red_flags)}", "red_flags": red_flags}

    target_roles = ", ".join(prefs["target_roles"])
    locations = ", ".join(prefs["locations"]["preferred"] + prefs["locations"]["acceptable"])
    salary_note = (
        f"Minimum salary: €{prefs['salary']['italy_min_eur']:,}/yr for Italy roles, "
        f"€{prefs['salary']['elsewhere_min_eur']:,}/yr elsewhere."
    )

    prompt = f"""You are evaluating job fit for a candidate. Return JSON only.

## Candidate Resume
{resume}

## Target Roles
{target_roles}

## Preferred Locations
{locations}

## Salary Requirements
{salary_note}

## Job Listing
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Location: {job.get('location', 'N/A')}
Source: {job.get('source', 'N/A')}
URL: {job.get('url', 'N/A')}

## Task
Score this job on a scale of 0.0–1.0 for fit with the candidate profile.
Consider: role match, seniority match, location/remote policy, domain relevance (medical AI is a plus but not required).
Be strict — 0.8+ means genuinely strong match.

Return JSON with keys:
- "score": float 0.0–1.0
- "rationale": 2–3 sentence explanation
- "location_ok": true/false
- "role_match": "strong" | "moderate" | "weak"
"""

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = resp.choices[0].message.content.strip()
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
