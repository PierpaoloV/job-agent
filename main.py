"""Job agent entry point."""
import json
import os
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from fetch_gmail import fetch_job_emails
from parse_jobs import parse_emails
from dedupe import filter_new, mark_seen, is_applied
from notify_telegram import send_digest, send_error
from portfolio_policy import LocalPortfolioScreener, PortfolioPolicy
from deep_grading import (
    DeepGradeStore,
    DeepGradingService,
    GradingContractError,
    SanitizedProfessionalProfile,
)
from opportunity_domain import OfficialVacancyData, OfficialVacancySnapshot
from openai_grading_provider import OpenAIGradingProvider
from search_official_source import is_official_company_url
from vacancy_policy import (
    ScreeningOutcome,
    VerificationState,
    screening_outcome,
    verification_state,
)
from watchlist_composition import build_watchlist_runtime as _build_watchlist_runtime
from watchlist_service import CompanyEligibilityPolicy
from workflow import SystemClock, WorkflowCoordinator


class GmailDiscovery:
    def fetch(self, days_back: int):
        return fetch_job_emails(days_back=days_back)


class EmailParser:
    def parse(self, emails):
        return parse_emails(emails)


class LegacyPersistence:
    """Keep the established seen/applied SQLite records readable in place."""

    def __init__(self, shortlist_dir=None):
        self._shortlist_dir = shortlist_dir or (
            pathlib.Path(__file__).parent / "data" / "shortlists"
        )
        self._replay_sources = tuple(
            source.strip()
            for source in os.environ.get(
                "JOB_AGENT_REPLAY_SOURCES", ""
            ).split(",")
            if source.strip()
        )
        self._replay_id = os.environ.get("JOB_AGENT_REPLAY_ID") or None

    def filter_new(self, jobs):
        return filter_new(
            jobs,
            replay_sources=self._replay_sources,
            replay_id=self._replay_id,
        )

    def is_applied(self, url: str):
        return is_applied(url)

    def mark_seen(self, jobs):
        mark_seen(
            jobs,
            replay_sources=self._replay_sources,
            replay_id=self._replay_id,
        )

    def save_shortlist(self, artifact):
        timestamp = artifact.created_at.replace(":", "-").replace("+", "_")
        artifact.write(self._shortlist_dir / f"{timestamp}.json")


class ProductionPortfolioGrader:
    """Grade verified vacancies; preserve unverified leads without model calls."""

    def __init__(
        self,
        *,
        store=None,
        provider=None,
        profile_loader=None,
        portfolio_policy=None,
    ):
        self._store = store or DeepGradeStore(
            pathlib.Path(__file__).parent / "data" / "deep-grades"
        )
        self._provider = provider or OpenAIGradingProvider()
        self._profile_loader = profile_loader or _load_grading_profile
        self._portfolio_policy = portfolio_policy or PortfolioPolicy.default()

    def rank(self, jobs, top_n: int):
        if not any(
            (
                verification_state(job.get("verification_status"))
                == VerificationState.VERIFIED
                and str(job.get("official_description", "")).strip()
            )
            or verification_state(job.get("verification_status"))
            == VerificationState.NEEDS_LOCAL_FETCH
            for job in jobs
        ):
            return []
        profile = self._profile_loader()
        service = DeepGradingService(
            provider=self._provider,
            store=self._store,
            hard_policy=self._portfolio_policy.hard_policy,
        )
        graded = []
        web_attempts = 0
        web_responses = 0
        grade_attempts = 0
        contract_failures = 0
        for job in jobs:
            grading_job = {
                **job,
                "stable_id": job.get("stable_id") or job.get("dedup_key"),
                "screening_outcome": job.get(
                    "screening_outcome",
                    ScreeningOutcome.SHORTLISTED.value,
                ),
            }
            state = verification_state(
                grading_job.get("verification_status")
            )
            if state == VerificationState.VERIFIED:
                resolved_job = grading_job
                grade_attempts += 1
                try:
                    result = service.grade_if_eligible(grading_job, profile)
                except GradingContractError:
                    contract_failures += 1
                    print(
                        "Deep grading "
                        f"{grading_job['stable_id']}: status=contract_error"
                    )
                    continue
            elif (
                state == VerificationState.NEEDS_LOCAL_FETCH
                and screening_outcome(
                    grading_job.get("screening_outcome")
                )
                == ScreeningOutcome.SHORTLISTED
            ):
                web_attempts += 1
                try:
                    resolved = self._provider.resolve_and_grade(
                        _public_alert_lead(grading_job),
                        profile.to_dict(),
                    )
                except RuntimeError:
                    print(
                        "Web grading resolution "
                        f"{grading_job['stable_id']}: "
                        "status=provider_error, accepted=false"
                    )
                    continue
                web_responses += 1
                resolution_status = str(
                    resolved.get("resolution_status", "unknown")
                )
                (
                    resolved_job,
                    raw_grade,
                    rejection_reason,
                ) = _accept_web_resolution(
                    grading_job,
                    resolved,
                )
                if resolved_job is None or raw_grade is None:
                    print(
                        "Web grading resolution "
                        f"{grading_job['stable_id']}: "
                        f"status={resolution_status}, accepted=false, "
                        f"reason={rejection_reason}"
                    )
                    continue
                print(
                    "Web grading resolution "
                    f"{grading_job['stable_id']}: "
                    f"status={resolution_status}, accepted=true"
                )
                raw_grade = _normalize_web_grade_matrix(
                    raw_grade,
                    resolved_job.get("requirements", ()),
                    profile,
                )
                grade_attempts += 1
                try:
                    result = DeepGradingService(
                        provider=_PreloadedGradeProvider(
                            raw_grade,
                            identity=f"{self._provider.identity}:web-search",
                        ),
                        store=self._store,
                        hard_policy=self._portfolio_policy.hard_policy,
                    ).grade_if_eligible(resolved_job, profile)
                except GradingContractError:
                    contract_failures += 1
                    print(
                        "Web grading resolution "
                        f"{grading_job['stable_id']}: status=contract_error, "
                        "accepted=false"
                    )
                    continue
            else:
                continue
            if result is None:
                continue
            grade = result.to_dict()
            graded.append({
                **resolved_job,
                "score": result.overall_score / 100,
                "priority": "high" if result.top_tier.value else "medium",
                "rationale": result.rank_explanation,
                "top_tier": grade["top_tier"],
                "portfolio_evaluation": grade,
                "requirements_evidence_matrix": grade[
                    "requirements_evidence_matrix"
                ],
            })
        if web_attempts and web_responses == 0:
            raise RuntimeError("All web grading resolutions failed safely")
        if grade_attempts and contract_failures == grade_attempts:
            raise RuntimeError(
                "All deep grading outputs failed contract validation"
            )
        return sorted(
            graded,
            key=lambda item: item["score"],
            reverse=True,
        )[:top_n]


class _PreloadedGradeProvider:
    def __init__(self, grade, *, identity):
        self._grade = dict(grade)
        self.identity = identity

    def complete(self, request):
        return dict(self._grade)


def _public_alert_lead(job):
    return {
        key: job.get(key)
        for key in (
            "stable_id",
            "title",
            "company",
            "location",
            "modality",
            "source",
            "url",
            "canonical_url",
            "snippet",
            "published_at",
        )
        if job.get(key) not in (None, "")
    }


def _accept_web_resolution(job, response):
    if response.get("resolution_status") != "verified":
        return None, None, "resolution_not_verified"
    vacancy = response.get("resolved_vacancy")
    grade = response.get("grade")
    if not isinstance(vacancy, dict):
        return None, None, "missing_resolved_vacancy"
    if not isinstance(grade, dict):
        return None, None, "missing_grade"
    official_url = str(vacancy.get("official_url", "")).strip()
    company = str(vacancy.get("company", "")).strip()
    title = str(vacancy.get("title", "")).strip()
    description = str(
        vacancy.get("official_description", "")
    ).strip()
    if not official_url:
        return None, None, "missing_official_url"
    if not company:
        return None, None, "missing_company"
    if not title:
        return None, None, "missing_title"
    if len(description) < 120:
        return None, None, "description_too_short"
    lead_company = str(job.get("company", "")).strip()
    company_matches_lead = _same_identity(company, lead_company)
    url_scoped_to_lead = is_official_company_url(
        official_url, lead_company
    )
    if not company_matches_lead and not url_scoped_to_lead:
        return None, None, "company_mismatch"
    canonical_company = company if company_matches_lead else lead_company
    if not _same_identity(title, str(job.get("title", ""))):
        return None, None, "title_mismatch"
    if not is_official_company_url(official_url, canonical_company):
        return None, None, "untrusted_official_url"
    sources = tuple(
        str(source)
        for source in grade.get("sources", ())
        if str(source).startswith("http")
    )
    if not any(
        is_official_company_url(source, canonical_company)
        for source in sources
    ):
        return None, None, "no_trusted_grade_source"
    now = SystemClock().now().isoformat()
    official = OfficialVacancyData(
        official_job_id=str(
            vacancy.get("official_job_id") or official_url
        ),
        canonical_url=official_url,
        company=canonical_company,
        role=title,
        team=str(vacancy.get("team", "")),
        location=str(vacancy.get("location", "")),
        modality=str(vacancy.get("modality", "")),
        seniority=str(vacancy.get("seniority", "")),
        compensation=json.dumps(
            grade.get("compensation", {}),
            sort_keys=True,
        ),
        requirements=tuple(
            str(value)
            for value in vacancy.get("requirements", ())
            if str(value).strip()
        ),
        ownership=str(
            grade.get("ownership", {}).get(
                "classification", "unknown"
            )
        ),
        sponsorship=str(
            grade.get("sponsorship", {}).get("status", "not_stated")
        ),
        description=description,
        published_at=(
            str(vacancy["published_at"])
            if vacancy.get("published_at")
            else None
        ),
    )
    snapshot = OfficialVacancySnapshot.capture(
        official,
        retrieved_at=now,
    )
    verified = {
        **job,
        "title": title,
        "role": title,
        "company": canonical_company,
        "team": official.team,
        "location": official.location,
        "modality": official.modality,
        "seniority": official.seniority,
        "official_description": description,
        "official_description_url": official_url,
        "official_url": official_url,
        "canonical_url": official_url,
        "official_vacancy_version": snapshot.version,
        "verification_status": VerificationState.VERIFIED.value,
        "retrieved_at": now,
        "published_at": official.published_at,
        "requirements": list(official.requirements),
        "compensation": grade.get("compensation", {}),
        "sponsorship": grade.get("sponsorship", {}),
        "ownership": grade.get("ownership", {}),
        "process_language": str(
            vacancy.get("process_language", "unknown")
        ),
    }
    return verified, grade, None


def _normalize_web_grade_matrix(raw_grade, requirements, profile):
    """Project model output onto canonical requirements and evidence IDs."""
    grade = dict(raw_grade)
    matrix = grade.get("requirements_evidence_matrix")
    if not isinstance(matrix, dict):
        return grade
    raw_rows = matrix.get("rows")
    if not isinstance(raw_rows, list):
        return grade

    known_evidence = {
        str(item.get("id"))
        for item in profile.professional_evidence
        if item.get("id")
    }
    rows_by_requirement = {
        str(row.get("requirement", "")).strip().casefold(): row
        for row in raw_rows
        if isinstance(row, dict)
        and str(row.get("requirement", "")).strip()
    }
    canonical_requirements = [
        str(requirement).strip()
        for requirement in requirements
        if str(requirement).strip()
    ]
    if not canonical_requirements:
        canonical_requirements = [
            str(row.get("requirement", "")).strip()
            for row in raw_rows
            if isinstance(row, dict)
            and str(row.get("requirement", "")).strip()
        ]

    normalized_rows = []
    for index, requirement in enumerate(canonical_requirements, start=1):
        source = rows_by_requirement.get(requirement.casefold(), {})
        evidence_ids = [
            str(evidence_id)
            for evidence_id in source.get("evidence_ids", ())
            if str(evidence_id) in known_evidence
        ]
        status = str(source.get("status", "unknown"))
        explanation = str(source.get("explanation", "")).strip()
        if status in {"matched", "partial"} and not evidence_ids:
            status = "unknown"
            explanation = (
                f"{explanation} " if explanation else ""
            ) + "No canonical professional evidence ID supports this assessment."
        if not explanation:
            explanation = "Held for review because no canonical assessment was returned."
        importance = str(source.get("importance", "required"))
        if importance not in {"required", "preferred"}:
            importance = "required"
        if status not in {"matched", "partial", "gap", "unknown"}:
            status = "unknown"
        normalized_rows.append(
            {
                "id": f"requirement-{index:03d}",
                "requirement": requirement,
                "importance": importance,
                "status": status,
                "evidence_ids": evidence_ids,
                "explanation": explanation,
            }
        )
    grade["requirements_evidence_matrix"] = {
        **matrix,
        "rows": normalized_rows,
    }
    return grade


def _same_identity(left, right):
    def words(value):
        return {
            word
            for word in __import__("re").findall(
                r"[a-z0-9]+", str(value).casefold()
            )
            if len(word) > 1
            and word not in {"senior", "sr", "junior", "jr", "the", "and"}
        }

    first, second = words(left), words(right)
    return bool(first and second) and (
        len(first & second) / min(len(first), len(second)) >= 0.6
    )


def _load_grading_profile() -> SanitizedProfessionalProfile:
    value = os.environ.get("JOB_AGENT_GRADING_PROFILE_JSON")
    if not value:
        raise RuntimeError(
            "JOB_AGENT_GRADING_PROFILE_JSON is required for verified deep grading"
        )
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise RuntimeError("JOB_AGENT_GRADING_PROFILE_JSON must be an object")
    return SanitizedProfessionalProfile.from_mapping(payload)


class TelegramNotifier:
    def send_digest(self, jobs):
        send_digest(jobs)

    def send_error(self, message: str):
        send_error(message)


def build_coordinator() -> WorkflowCoordinator:
    portfolio_policy = _load_portfolio_policy()
    return WorkflowCoordinator(
        discovery=GmailDiscovery(),
        parser=EmailParser(),
        persistence=LegacyPersistence(),
        screener=LocalPortfolioScreener(portfolio_policy),
        grader=ProductionPortfolioGrader(portfolio_policy=portfolio_policy),
        notifier=TelegramNotifier(),
        clock=SystemClock(),
    )


def build_watchlist_runtime(
    *,
    repository_root=None,
    clock=None,
    browser_driver=None,
    telegram_sender=None,
    eligibility_policy=None,
):
    """Expose the local watchlist composition without implicit external calls."""

    configured_eligibility = eligibility_policy
    if configured_eligibility is None:
        preferences_path = os.environ.get("JOB_AGENT_PREFERENCES_PATH", "").strip()
        if preferences_path:
            hard_policy = _load_portfolio_policy().hard_policy
            configured_eligibility = CompanyEligibilityPolicy.from_values(
                excluded_ownership=hard_policy.excluded_ownership,
            )
        else:
            configured_eligibility = CompanyEligibilityPolicy()
    return _build_watchlist_runtime(
        repository_root=(
            pathlib.Path(__file__).parent
            if repository_root is None
            else repository_root
        ),
        clock=SystemClock() if clock is None else clock,
        browser_driver=browser_driver,
        telegram_sender=telegram_sender,
        eligibility_policy=configured_eligibility,
    )


def _load_portfolio_policy(path: pathlib.Path | None = None) -> PortfolioPolicy:
    configured_path = path
    if configured_path is None:
        raw_path = os.environ.get("JOB_AGENT_PREFERENCES_PATH", "").strip()
        if not raw_path:
            raise RuntimeError(
                "Portfolio preferences are missing. "
                "Set JOB_AGENT_PREFERENCES_PATH explicitly."
            )
        configured_path = pathlib.Path(raw_path)
    payload = yaml.safe_load(configured_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("preferences.yaml must contain an object")
    return PortfolioPolicy.from_mapping(payload)


def main(days_back: int = 2):
    return build_coordinator().run(days_back=days_back)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="How many days back to fetch emails")
    args = parser.parse_args()
    main(days_back=args.days)
