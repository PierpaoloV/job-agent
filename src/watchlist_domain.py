"""Domain records for approved company monitoring and job-alert subscriptions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EligibilityEvidence:
    """A sourced, time-bound classification; never a permanent company label."""

    classification: str
    source_url: str
    verified_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EligibilityEvidence":
        return cls(
            classification=str(value["classification"]),
            source_url=str(value["source_url"]),
            verified_at=str(value["verified_at"]),
        )


@dataclass(frozen=True)
class CompanyCandidate:
    name: str
    careers_url: str
    jurisdiction: str
    ownership: EligibilityEvidence
    sponsorship: EligibilityEvidence
    discovery_source: str
    jurisdiction_country_code: str | None = None

    @property
    def evidence_version(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "careers_url": self.careers_url,
            "jurisdiction": self.jurisdiction,
            "ownership": self.ownership.to_dict(),
            "sponsorship": self.sponsorship.to_dict(),
            "discovery_source": self.discovery_source,
            "jurisdiction_country_code": self.jurisdiction_country_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompanyCandidate":
        return cls(
            name=str(value["name"]),
            careers_url=str(value["careers_url"]),
            jurisdiction=str(value["jurisdiction"]),
            ownership=EligibilityEvidence.from_dict(value["ownership"]),
            sponsorship=EligibilityEvidence.from_dict(value["sponsorship"]),
            discovery_source=str(value["discovery_source"]),
            jurisdiction_country_code=(
                None
                if value.get("jurisdiction_country_code") is None
                else str(value["jurisdiction_country_code"])
            ),
        )


@dataclass(frozen=True)
class CompanyProposal:
    proposal_id: str
    candidate: CompanyCandidate
    evidence_version: str
    proposed_at: str

    @property
    def name(self) -> str:
        return self.candidate.name

    @property
    def ownership(self) -> EligibilityEvidence:
        return self.candidate.ownership

    @property
    def sponsorship(self) -> EligibilityEvidence:
        return self.candidate.sponsorship


@dataclass(frozen=True)
class JobAlertCandidate:
    source: str
    source_url: str
    expected_coverage: str
    query: str
    location: str

    @property
    def version(self) -> str:
        return _canonical_digest(asdict(self))

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobAlertCandidate":
        return cls(
            source=str(value["source"]),
            source_url=str(value["source_url"]),
            expected_coverage=str(value["expected_coverage"]),
            query=str(value["query"]),
            location=str(value["location"]),
        )


@dataclass(frozen=True)
class JobAlertProposal:
    proposal_id: str
    alert: JobAlertCandidate
    version: str
    proposed_at: str


@dataclass(frozen=True)
class DecisionResult:
    status: str
    proposal_id: str | None = None


@dataclass(frozen=True)
class SubscriptionReport:
    status: str
    proposal_id: str
    idempotency_key: str
    source: str
    expected_coverage: str
    external_reference: str | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"subscribed", "failed", "uncertain"}:
            raise ValueError(f"Unsupported subscription status: {self.status}")

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubscriptionReport":
        return cls(
            status=str(value["status"]),
            proposal_id=str(value["proposal_id"]),
            idempotency_key=str(value["idempotency_key"]),
            source=str(value["source"]),
            expected_coverage=str(value["expected_coverage"]),
            external_reference=(
                None
                if value.get("external_reference") is None
                else str(value["external_reference"])
            ),
            error_type=(
                None if value.get("error_type") is None else str(value["error_type"])
            ),
        )
