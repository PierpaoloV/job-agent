"""Domain records for verified opportunities and human decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from opportunity_sources import OpportunityLead


MATERIAL_FIELDS = (
    "company",
    "role",
    "team",
    "location",
    "modality",
    "seniority",
    "compensation",
    "requirements",
    "ownership",
    "sponsorship",
    "official_job_id",
)


class Runtime(str, Enum):
    HOSTED = "hosted"
    LOCAL = "local"


class VerificationStatus(str, Enum):
    LEAD = "lead"
    VERIFIED = "verified"
    NEEDS_LOCAL_FETCH = "needs_local_fetch"
    NEEDS_OFFICIAL_DESCRIPTION = "needs_official_description"


class DecisionAction(str, Enum):
    MORE_DETAILS = "Dimmi di più"
    PREPARE = "Prepara candidatura"
    DISCARD = "Scarta"


class DecisionStatus(str, Enum):
    DETAILS = "details"
    APPROVED = "approved"
    DISCARDED = "discarded"
    STALE = "stale"
    NOT_VERIFIED = "not_verified"
    EXPIRED = "expired"
    REPLAYED = "replayed"
    MISMATCHED = "mismatched"
    INVALID_STATE = "invalid_state"
    NEEDS_REASON = "needs_reason"


class OpportunityLifecycle(str, Enum):
    DISCOVERED = "scoperta"
    PROPOSED = "proposta"
    APPROVED = "approvata"
    DISCARDED = "scartata"


@dataclass(frozen=True)
class OfficialVacancyData:
    official_job_id: str
    canonical_url: str
    company: str
    role: str
    team: str
    location: str
    modality: str
    seniority: str
    compensation: str
    requirements: tuple[str, ...]
    ownership: str
    sponsorship: str
    description: str
    published_at: str | None = None


@dataclass(frozen=True)
class MaterialChange:
    field: str
    before: Any
    after: Any

    @property
    def explanation(self) -> str:
        return f"{self.field}: {self.before!r} -> {self.after!r}"


@dataclass(frozen=True)
class OfficialVacancySnapshot:
    version: str
    material_fingerprint: str
    retrieved_at: str
    vacancy: OfficialVacancyData
    changes: tuple[MaterialChange, ...] = ()

    @classmethod
    def capture(
        cls,
        vacancy: OfficialVacancyData,
        *,
        retrieved_at: str,
        previous: "OfficialVacancySnapshot | None" = None,
    ) -> "OfficialVacancySnapshot":
        material = {
            field: getattr(vacancy, field)
            for field in MATERIAL_FIELDS
        }
        full = asdict(vacancy)
        changes = ()
        if previous is not None:
            changes = tuple(
                MaterialChange(
                    field, getattr(previous.vacancy, field), material[field]
                )
                for field in MATERIAL_FIELDS
                if getattr(previous.vacancy, field) != material[field]
            )
        return cls(
            version=_fingerprint(full),
            material_fingerprint=_fingerprint(material),
            retrieved_at=retrieved_at,
            vacancy=vacancy,
            changes=changes,
        )

    @property
    def change_explanation(self) -> tuple[str, ...]:
        return tuple(change.explanation for change in self.changes)


@dataclass(frozen=True)
class Evaluation:
    fit_summary: str
    gaps: tuple[str, ...]
    compensation_status: str
    wealth_potential_confidence: str
    immigration: str
    ownership: str
    risks: tuple[str, ...]
    rank_explanation: str
    requirement_analysis: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ApprovedApplication:
    application_id: str
    opportunity_id: str
    opportunity_version: str
    actor: str
    approved_at: str
    action: DecisionAction
    expires_at: str


@dataclass(frozen=True)
class DecisionAuthorization:
    token: str
    stable_id: str
    verified_version: str
    action: DecisionAction
    actor: str
    issued_at: str
    expires_at: str
    awaiting_reason_at: str | None = None
    consumed_at: str | None = None


@dataclass(frozen=True)
class ConditionalDiscard:
    opportunity_id: str
    opportunity_version: str
    role_similarity_key: str
    material_fingerprint: str
    material_values: dict[str, Any]
    reason: str
    actor: str
    discarded_at: str


@dataclass(frozen=True)
class DecisionCommand:
    token: str
    stable_id: str
    verified_version: str
    action: DecisionAction
    reason: str | None = None


@dataclass(frozen=True)
class RoleCard:
    identity: str
    location: str
    modality: str
    source: str
    freshness: str
    fit_summary: str
    gaps: tuple[str, ...]
    compensation_status: str
    wealth_potential_confidence: str
    immigration: str
    ownership: str
    risks: tuple[str, ...]
    rank_explanation: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class OpportunityDetails:
    description: str
    requirement_analysis: tuple[str, ...]
    sources: tuple[str, ...]
    link: str
    risks: tuple[str, ...]


@dataclass(frozen=True)
class DecisionResult:
    status: DecisionStatus
    details: OpportunityDetails | None = None
    approved_application: ApprovedApplication | None = None
    message: str | None = None


@dataclass(frozen=True)
class SuppressionResult:
    suppressed: bool
    reason: str | None = None
    material_changes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpportunityRecord:
    lead: OpportunityLead
    lifecycle: OpportunityLifecycle = OpportunityLifecycle.DISCOVERED
    status: VerificationStatus = VerificationStatus.LEAD
    snapshots: tuple[OfficialVacancySnapshot, ...] = ()
    evaluation: Evaluation | None = None
    evaluation_version: str | None = None
    operator_request: str | None = None
    approved_applications: tuple[ApprovedApplication, ...] = ()
    discard: ConditionalDiscard | None = None
    decision_authorizations: tuple[DecisionAuthorization, ...] = ()

    @property
    def latest_snapshot(self) -> OfficialVacancySnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpportunityRecord":
        return cls(
            lead=OpportunityLead(**value["lead"]),
            lifecycle=OpportunityLifecycle(str(value.get("lifecycle", "scoperta"))),
            status=VerificationStatus(str(value.get("status", "lead"))),
            snapshots=tuple(
                _snapshot_from_dict(item) for item in value.get("snapshots", [])
            ),
            evaluation=(
                None
                if value.get("evaluation") is None
                else _evaluation_from_dict(value["evaluation"])
            ),
            evaluation_version=_optional_string(value.get("evaluation_version")),
            operator_request=_optional_string(value.get("operator_request")),
            approved_applications=tuple(
                ApprovedApplication(
                    **{
                        **item,
                        "action": DecisionAction(str(item["action"])),
                    }
                )
                for item in value.get("approved_applications", [])
            ),
            discard=(
                None
                if value.get("discard") is None
                else ConditionalDiscard(**value["discard"])
            ),
            decision_authorizations=tuple(
                DecisionAuthorization(
                    **{
                        **item,
                        "action": DecisionAction(str(item["action"])),
                    }
                )
                for item in value.get("decision_authorizations", [])
            ),
        )


@dataclass(frozen=True)
class VerificationResult:
    stable_id: str
    status: VerificationStatus
    snapshot: OfficialVacancySnapshot | None
    evaluation: Evaluation | None
    operator_request: str | None


def _snapshot_from_dict(value: Mapping[str, Any]) -> OfficialVacancySnapshot:
    return OfficialVacancySnapshot(
        version=str(value["version"]),
        material_fingerprint=str(value["material_fingerprint"]),
        retrieved_at=str(value["retrieved_at"]),
        vacancy=_official_vacancy_from_dict(value["vacancy"]),
        changes=tuple(
            MaterialChange(
                field=str(change["field"]),
                before=change.get("before"),
                after=change.get("after"),
            )
            for change in value.get("changes", [])
        ),
    )


def _official_vacancy_from_dict(value: Mapping[str, Any]) -> OfficialVacancyData:
    return OfficialVacancyData(
        official_job_id=str(value["official_job_id"]),
        canonical_url=str(value["canonical_url"]),
        company=str(value["company"]),
        role=str(value["role"]),
        team=str(value["team"]),
        location=str(value["location"]),
        modality=str(value["modality"]),
        seniority=str(value["seniority"]),
        compensation=str(value["compensation"]),
        requirements=tuple(map(str, value.get("requirements", []))),
        ownership=str(value["ownership"]),
        sponsorship=str(value["sponsorship"]),
        description=str(value["description"]),
        published_at=_optional_string(value.get("published_at")),
    )


def _evaluation_from_dict(value: Mapping[str, Any]) -> Evaluation:
    return Evaluation(
        fit_summary=str(value["fit_summary"]),
        gaps=tuple(map(str, value.get("gaps", []))),
        compensation_status=str(value["compensation_status"]),
        wealth_potential_confidence=str(value["wealth_potential_confidence"]),
        immigration=str(value["immigration"]),
        ownership=str(value["ownership"]),
        risks=tuple(map(str, value.get("risks", []))),
        rank_explanation=str(value["rank_explanation"]),
        requirement_analysis=tuple(map(str, value.get("requirement_analysis", []))),
        sources=tuple(map(str, value.get("sources", []))),
    )


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    return value


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)
