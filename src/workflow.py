"""Public workflow boundary for discovery, screening, grading, and notification.

The coordinator owns orchestration while vendor-specific behavior is supplied
through small adapters.  The two public stages deliberately exchange a
serialized, versioned artifact so remote ingestion cannot accidentally acquire
an LLM dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from vacancy_policy import PUBLIC_VACANCY_FIELDS


SHORTLIST_ARTIFACT_VERSION = "job-agent.shortlist.v1"
SUPPORTED_SHORTLIST_ARTIFACT_VERSIONS = frozenset({SHORTLIST_ARTIFACT_VERSION})
_EMAIL_ADDRESS = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_CANDIDATE_SENSITIVE_KEYS = frozenset({
    "candidate",
    "candidate_profile",
    "ats_answer",
    "ats_answers",
    "health",
    "diagnosis",
    "demographic",
    "passport",
    "credential",
    "password",
    "token",
    "api_key",
    "secret",
    "authorization",
    "browser_state",
    "browserstate",
    "cookie",
    "cover_letter",
    "coverletter",
    "cv",
    "disability",
    "gender",
    "identity",
    "identity_card",
    "identity_document",
    "identity_documents",
    "identitydocument",
    "identitydocuments",
    "medical",
    "medical_condition",
    "medicalcondition",
    "oauth",
    "protected_report",
    "protectedreport",
    "race",
    "resume",
    "social_security_number",
    "socialsecuritynumber",
    "tax_id",
    "veteran",
})

# Only public vacancy data may cross the remote artifact boundary. Raw email
# bodies, credentials, and candidate data are excluded; the limited alert
# context retained for legacy ranking has email addresses redacted.
class DiscoveryAdapter(Protocol):
    def fetch(self, days_back: int) -> list[dict]: ...


class ParserAdapter(Protocol):
    def parse(self, emails: Sequence[dict]) -> list[dict]: ...


class PersistenceAdapter(Protocol):
    def filter_new(self, jobs: Sequence[dict]) -> list[dict]: ...

    def is_applied(self, url: str) -> bool: ...

    def mark_seen(self, jobs: Sequence[dict]) -> None: ...

    def save_shortlist(self, artifact: "ShortlistArtifact") -> None: ...


class ScreeningAdapter(Protocol):
    def screen(self, job: Mapping[str, Any]) -> Mapping[str, Any]: ...


class GradingAdapter(Protocol):
    def rank(self, jobs: Sequence[dict], top_n: int) -> list[dict]: ...


class NotificationAdapter(Protocol):
    def send_digest(self, jobs: Sequence[dict]) -> None: ...

    def send_error(self, message: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class PassThroughScreener:
    """Preserve the legacy funnel until deterministic policy lands in #4.

    This adapter is intentionally local and makes no model or network calls.
    It keeps every valid, previously unseen lead eligible for the legacy deep
    grader while establishing the replaceable screening seam.
    """

    def screen(self, job: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "score": 1.0,
            "reasons": ["new lead accepted by legacy screening policy"],
            "shortlisted": True,
        }


@dataclass(frozen=True)
class NormalizedOpportunity:
    stable_id: str
    discovered_at: str
    source_confidence: str
    local_score: float
    screening_reasons: tuple[str, ...]
    shortlisted: bool
    job: dict[str, Any]
    screening_outcome: str = "unknown"
    screening_features: dict[str, Any] = field(default_factory=dict)

    def as_job(self) -> dict[str, Any]:
        return dict(self.job)

    def as_grading_job(self) -> dict[str, Any]:
        return {
            **self.job,
            "stable_id": self.stable_id,
            "local_score": self.local_score,
            "screening_reasons": list(self.screening_reasons),
            "screening_outcome": self.screening_outcome,
            "screening_features": self.screening_features,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "discovered_at": self.discovered_at,
            "source_confidence": self.source_confidence,
            "local_score": self.local_score,
            "screening_reasons": list(self.screening_reasons),
            "shortlisted": self.shortlisted,
            "screening_outcome": self.screening_outcome,
            "screening_features": self.screening_features,
            "job": self.as_job(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NormalizedOpportunity":
        return cls(
            stable_id=str(value["stable_id"]),
            discovered_at=str(value["discovered_at"]),
            source_confidence=str(value["source_confidence"]),
            local_score=float(value["local_score"]),
            screening_reasons=tuple(str(reason) for reason in value.get("screening_reasons", [])),
            shortlisted=bool(value["shortlisted"]),
            job=dict(value["job"]),
            screening_outcome=str(value.get("screening_outcome", "unknown")),
            screening_features=dict(value.get("screening_features", {})),
        )


@dataclass(frozen=True)
class ShortlistArtifact:
    version: str
    created_at: str
    opportunities: tuple[NormalizedOpportunity, ...]

    def validate(self) -> None:
        if self.version not in SUPPORTED_SHORTLIST_ARTIFACT_VERSIONS:
            raise ValueError(f"Unsupported shortlist artifact version: {self.version}")
        if any(_contains_candidate_sensitive_data(item.to_dict()) for item in self.opportunities):
            raise ValueError("Shortlist artifact contains candidate-sensitive data")
        public_fields = set(PUBLIC_VACANCY_FIELDS)
        if any(set(item.job) - public_fields for item in self.opportunities):
            raise ValueError("Shortlist artifact contains non-public vacancy fields")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "version": self.version,
            "created_at": self.created_at,
            "opportunities": [opportunity.to_dict() for opportunity in self.opportunities],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")

    def screening_audit_sample(self, limit: int = 5) -> tuple[NormalizedOpportunity, ...]:
        """Return a deterministic sample of records omitted from the shortlist."""
        if limit < 0:
            raise ValueError("Audit sample limit cannot be negative")
        overflow = (item for item in self.opportunities if not item.shortlisted)
        return tuple(
            sorted(
                overflow,
                key=lambda item: (-item.local_score, item.stable_id),
            )[:limit]
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShortlistArtifact":
        artifact = cls(
            version=str(value["version"]),
            created_at=str(value["created_at"]),
            opportunities=tuple(
                NormalizedOpportunity.from_dict(item)
                for item in value.get("opportunities", [])
            ),
        )
        artifact.validate()
        return artifact

    @classmethod
    def from_json(cls, value: str) -> "ShortlistArtifact":
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("Shortlist artifact must be a JSON object")
        return cls.from_dict(payload)

    @classmethod
    def read(cls, path: Path) -> "ShortlistArtifact":
        return cls.from_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class WorkflowResult:
    status: str
    artifact: ShortlistArtifact
    ranked_jobs: tuple[dict[str, Any], ...] = ()


class WorkflowCoordinator:
    """Coordinate the legacy digest through replaceable public boundaries."""

    def __init__(
        self,
        *,
        discovery: DiscoveryAdapter,
        parser: ParserAdapter,
        persistence: PersistenceAdapter,
        screener: ScreeningAdapter,
        grader: GradingAdapter,
        notifier: NotificationAdapter,
        clock: Clock,
        top_n: int = 10,
    ) -> None:
        self._discovery = discovery
        self._parser = parser
        self._persistence = persistence
        self._screener = screener
        self._grader = grader
        self._notifier = notifier
        self._clock = clock
        self._top_n = top_n

    def ingest_and_screen(self, *, days_back: int = 2) -> ShortlistArtifact:
        """Run the deterministic first stage without model credentials."""
        emails = self._discovery.fetch(days_back)
        jobs = self._parser.parse(emails) if emails else []
        new_jobs = self._persistence.filter_new(jobs) if jobs else []
        eligible_jobs = _unique_jobs([
            job for job in new_jobs
            if not self._persistence.is_applied(job.get("url", ""))
        ])

        created_at = self._clock.now().isoformat()
        opportunities = tuple(
            self._normalize(job, self._screener.screen(job), created_at)
            for job in eligible_jobs
            if job.get("dedup_key") or job.get("url")
        )
        artifact = ShortlistArtifact(
            version=SHORTLIST_ARTIFACT_VERSION,
            created_at=created_at,
            opportunities=opportunities,
        )
        self._persistence.save_shortlist(artifact)
        return artifact

    def deep_grade(self, artifact: ShortlistArtifact) -> WorkflowResult:
        """Grade only shortlisted records from a compatible stage-one artifact."""
        artifact.validate()
        shortlisted = [
            opportunity.as_grading_job()
            for opportunity in artifact.opportunities
            if opportunity.shortlisted
        ]
        ranked_jobs = self._grader.rank(shortlisted, self._top_n) if shortlisted else []

        # Preserve the legacy invariant: all new eligible records become seen,
        # including records that a future screener keeps outside the shortlist.
        all_jobs = [opportunity.as_job() for opportunity in artifact.opportunities]
        if all_jobs:
            self._persistence.mark_seen(all_jobs)
        self._notifier.send_digest(ranked_jobs)
        return WorkflowResult(
            status="completed",
            artifact=artifact,
            ranked_jobs=tuple(ranked_jobs),
        )

    def run(self, *, days_back: int = 2) -> WorkflowResult:
        """Run both stages and reduce any failure to one safe notification."""
        artifact = ShortlistArtifact(
            version=SHORTLIST_ARTIFACT_VERSION,
            created_at="unavailable",
            opportunities=(),
        )
        try:
            artifact = self.ingest_and_screen(days_back=days_back)
            return self.deep_grade(artifact)
        except Exception as exc:
            self._report_failure(exc)
            return WorkflowResult(status="failed", artifact=artifact)

    def _normalize(
        self,
        job: Mapping[str, Any],
        decision: Mapping[str, Any],
        discovered_at: str,
    ) -> NormalizedOpportunity:
        stable_id = str(job.get("dedup_key") or job.get("url"))
        public_job = {
            field: (
                _EMAIL_ADDRESS.sub("[email redacted]", str(job[field]))
                if field == "raw_email_context"
                else job[field]
            )
            for field in PUBLIC_VACANCY_FIELDS
            if field in job
        }
        # The stable key is required by existing SQLite persistence even when a
        # source supplied only a URL.
        public_job["dedup_key"] = stable_id
        return NormalizedOpportunity(
            stable_id=stable_id,
            discovered_at=discovered_at,
            source_confidence=_source_confidence(str(job.get("source", ""))),
            local_score=float(decision.get("score", 0.0)),
            screening_reasons=tuple(str(reason) for reason in decision.get("reasons", [])),
            shortlisted=bool(decision.get("shortlisted", False)),
            job=public_job,
            screening_outcome=str(decision.get("outcome", "unknown")),
            screening_features=_public_screening_features(decision.get("features", {})),
        )

    def _report_failure(self, exc: Exception) -> None:
        # Exception text may contain tokens, provider response bodies, or local
        # paths.  Only the exception type is safe for local diagnostics.
        print(f"Workflow failed ({type(exc).__name__}); operator notified safely.")
        try:
            self._notifier.send_error(
                "Job workflow failed safely. No further action was taken; "
                "check the local or GitHub Actions logs."
            )
        except Exception:
            print("Workflow error notification could not be delivered.")


def _source_confidence(source: str) -> str:
    normalized = source.casefold()
    supported = ("linkedin", "indeed", "glassdoor", "welcome to the jungle", "eurotechjobs")
    return "supported" if any(name in normalized for name in supported) else "fallback"


def _unique_jobs(jobs: Sequence[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for job in jobs:
        stable_id = str(job.get("dedup_key") or job.get("url") or "").strip()
        if stable_id and stable_id not in unique:
            unique[stable_id] = job
    return list(unique.values())


def _public_screening_features(value: Any) -> dict[str, Any]:
    """Keep only JSON-like, non-candidate screening evidence in the artifact."""
    if not isinstance(value, Mapping):
        return {}

    def public(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): public(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [public(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)

    return public(value)


def _contains_candidate_sensitive_data(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            split_camel_case = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)
            )
            normalized = re.sub(
                r"[^a-z0-9]+", "_", split_camel_case.casefold()
            ).strip("_")
            parts = {part for part in normalized.split("_") if part}
            if normalized in _CANDIDATE_SENSITIVE_KEYS or bool(
                parts & _CANDIDATE_SENSITIVE_KEYS
            ):
                return True
            if _contains_candidate_sensitive_data(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_candidate_sensitive_data(item) for item in value)
    return False
