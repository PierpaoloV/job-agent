"""Typed domain records for the local application workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any, ClassVar, Mapping

from application_interventions import InterventionRecord, UncertainSubmissionRecord


class WorkflowAction(str, Enum):
    PREPARE = "Prepara candidatura"
    RETRY_PREPARATION = "Riprova preparazione"
    FILL = "Compila"
    SUBMIT = "Invia"
    RESUME = "Riprendi"
    RESOLVE_NOT_SUBMITTED = "Conferma non inviata"


class ArtifactFamily(str, Enum):
    RESEARCH = "research"
    CV_APPLIED_ML = "cv_applied_ml"
    AGENTIC_AI = "agentic_ai"


class EvidenceKind(str, Enum):
    EXPERIENCE = "experience"
    SKILL = "skill"
    IMPACT = "impact"


class ArtifactDocument(str, Enum):
    CV = "cv"
    COVER_LETTER = "cover_letter"


class LifecycleState(str, Enum):
    PROPOSED = "proposta"
    APPROVED = "approvata"
    CV_READY = "CV pronto"
    FILLING = "compilazione in corso"
    READY_TO_SUBMIT = "pronta da inviare"
    SUBMITTED = "inviata"
    INTERVIEW = "colloquio"
    REJECTED = "rifiutata"
    DISCARDED = "scartata"


class CorrespondenceClassification(str, Enum):
    RECEIPT = "receipt"
    REJECTION = "rejection"
    INTERVIEW = "interview"
    AMBIGUOUS = "ambiguous"
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    REFERRAL = "referral"


class CorrespondenceTrustEvidence(str, Enum):
    CONFIGURED_DOMAIN = "configured_domain"
    TRUSTED_THREAD = "trusted_thread"


class OperationalStatus(str, Enum):
    PREPARATION_FAILED = "preparation_failed"
    FILL_FAILED = "fill_failed"
    ARTIFACT_MISMATCH = "artifact_version_mismatch"
    SUBMISSION_STARTED = "submission_started"
    SUBMISSION_UNCERTAIN = "submission_outcome_uncertain"
    EXPIRED_PREPARATION = "expired_preparation"
    VACANCY_CHANGED = "vacancy_changed"
    ATS_REVIEW_REPREPARE_REQUIRED = "ats_review_reprepare_required"
    MASTER_CV_RELOADED = "master_cv_or_evidence_reloaded"
    INTERVENTION_REQUIRED = "intervention_required"


class CommandStatus(str, Enum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"
    MISMATCHED = "mismatched"
    REPLAYED = "replayed"
    EXPIRED = "expired"
    STALE = "stale"
    UNCERTAIN = "uncertain"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    INTERVENTION_REQUIRED = "intervention_required"
    RESOLVED = "resolved"
    CAPACITY_REACHED = "capacity_reached"


class SubmissionStatus(str, Enum):
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"


class ReviewEvidencePage(str, Enum):
    REVIEW = "review"


class AnswerVisibility(str, Enum):
    LOCAL_ONLY = "local_only"
    PUBLIC_SUMMARY = "public_summary"


class SubmissionVerificationKind(str, Enum):
    CONFIRMATION_PAGE = "confirmation_page"
    CONFIRMATION_ID = "confirmation_id"
    ATS_SUBMITTED = "ats_submitted"
    EMAIL_RECEIPT = "email_receipt"


class PreparationReminderPriority(str, Enum):
    NORMAL = "normal"
    DEADLINE = "deadline"


class PreparationCapacityExceptionKind(str, Enum):
    TOP_TIER = "top_tier"
    DEADLINE = "deadline"


@dataclass(frozen=True)
class MaterialRoleFingerprint:
    """Normalized role facts used for deterministic resurfacing explanations."""

    values: tuple[tuple[str, str], ...]

    FIELD_SOURCES: ClassVar[tuple[tuple[str, tuple[str, ...]], ...]] = (
        ("company", ("company",)),
        ("title", ("title", "role")),
        ("team", ("team",)),
        ("location", ("location",)),
        ("modality", ("modality",)),
        ("seniority", ("seniority",)),
        ("compensation", ("compensation", "salary")),
        ("requirements", ("requirements",)),
        ("ownership", ("ownership",)),
        ("sponsorship", ("sponsorship",)),
        ("language", ("language",)),
        ("official_job_id", ("official_job_id", "official_id", "job_id")),
        (
            "official_job_version",
            ("official_job_version", "vacancy_version", "posting_version"),
        ),
        ("official_url", ("official_url",)),
        ("official_description", ("official_description",)),
        (
            "application_deadline",
            ("application_deadline", "deadline"),
        ),
    )

    @classmethod
    def from_opportunity(
        cls, opportunity: Mapping[str, Any]
    ) -> "MaterialRoleFingerprint":
        values = []
        for field, sources in cls.FIELD_SOURCES:
            raw = next(
                (opportunity[source] for source in sources if source in opportunity),
                None,
            )
            values.append((field, _canonical_material_value(raw)))
        return cls(tuple(values))

    def changes_from(self, previous: "MaterialRoleFingerprint") -> tuple[str, ...]:
        previous_values = dict(previous.values)
        return tuple(
            field for field, value in self.values if previous_values.get(field) != value
        )


@dataclass(frozen=True)
class PreparationCapacityException:
    kind: PreparationCapacityExceptionKind
    reason: str
    deadline_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", PreparationCapacityExceptionKind(self.kind))
        if not self.reason.strip():
            raise ValueError("Capacity exception reason is required")
        if self.kind == PreparationCapacityExceptionKind.DEADLINE:
            if self.deadline_at is None:
                raise ValueError("Deadline capacity exception requires a deadline")
            _require_iso_timestamp(self.deadline_at, "Capacity exception deadline")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PreparationCapacityException":
        return cls(
            kind=PreparationCapacityExceptionKind(str(value["kind"])),
            reason=str(value["reason"]),
            deadline_at=_optional_string(value.get("deadline_at")),
        )


@dataclass(frozen=True)
class PreparationReminder:
    reminder_id: str
    application_id: str
    emitted_at: str
    preparation_expires_at: str
    priority: PreparationReminderPriority
    deadline_at: str | None = None
    delivered_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "priority", PreparationReminderPriority(self.priority))
        if not self.reminder_id or not self.application_id:
            raise ValueError("Preparation reminder scope is required")
        _require_iso_timestamp(self.emitted_at, "Reminder emission timestamp")
        _require_iso_timestamp(
            self.preparation_expires_at, "Preparation expiry timestamp"
        )
        if self.deadline_at is not None:
            _require_iso_timestamp(self.deadline_at, "Application deadline")
        if self.delivered_at is not None:
            _require_iso_timestamp(self.delivered_at, "Reminder delivery timestamp")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PreparationReminder":
        return cls(
            reminder_id=str(value["reminder_id"]),
            application_id=str(value["application_id"]),
            emitted_at=str(value["emitted_at"]),
            preparation_expires_at=str(value["preparation_expires_at"]),
            priority=PreparationReminderPriority(str(value["priority"])),
            deadline_at=_optional_string(value.get("deadline_at")),
            delivered_at=_optional_string(value.get("delivered_at")),
        )


@dataclass(frozen=True)
class PriorApplicationEvidence:
    application_id: str
    lifecycle_state: LifecycleState
    opportunity_version: str
    recorded_at: str
    material_changes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "lifecycle_state", LifecycleState(self.lifecycle_state)
        )
        if not self.application_id or not self.opportunity_version:
            raise ValueError("Prior application scope is required")
        _require_iso_timestamp(self.recorded_at, "Prior application timestamp")
        object.__setattr__(
            self, "material_changes", tuple(map(str, self.material_changes))
        )

    @property
    def is_active(self) -> bool:
        return self.lifecycle_state in {
            LifecycleState.SUBMITTED,
            LifecycleState.INTERVIEW,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PriorApplicationEvidence":
        return cls(
            application_id=str(value["application_id"]),
            lifecycle_state=LifecycleState(str(value["lifecycle_state"])),
            opportunity_version=str(value["opportunity_version"]),
            recorded_at=str(value["recorded_at"]),
            material_changes=tuple(map(str, value.get("material_changes", []))),
        )


@dataclass(frozen=True)
class ArtifactClaimTrace:
    statement: str
    kind: EvidenceKind
    evidence_ids: tuple[str, ...]
    appears_in: tuple[ArtifactDocument, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactClaimTrace":
        return cls(
            statement=str(value["statement"]),
            kind=EvidenceKind(value["kind"]),
            evidence_ids=tuple(map(str, value.get("evidence_ids", []))),
            appears_in=tuple(
                ArtifactDocument(item) for item in value.get("appears_in", [])
            ),
        )


@dataclass(frozen=True)
class StretchDecision:
    is_stretch: bool
    gaps: tuple[str, ...] = ()
    explanation: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StretchDecision":
        return cls(
            is_stretch=bool(value["is_stretch"]),
            gaps=tuple(map(str, value.get("gaps", []))),
            explanation=str(value.get("explanation", "")),
        )


@dataclass(frozen=True)
class PreparedArtifacts:
    version: str
    cv_path: str
    cover_letter_path: str
    cv_hash: str
    cover_letter_hash: str
    evidence_source_version: str | None = None
    matrix_version: str | None = None
    family: ArtifactFamily | None = None
    claims: tuple[ArtifactClaimTrace, ...] = ()
    stretch_decision: StretchDecision = StretchDecision(False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PreparedArtifacts":
        return cls(
            version=str(value["version"]),
            cv_path=str(value["cv_path"]),
            cover_letter_path=str(value["cover_letter_path"]),
            cv_hash=str(value["cv_hash"]),
            cover_letter_hash=str(value["cover_letter_hash"]),
            evidence_source_version=_optional_string(
                value.get("evidence_source_version")
            ),
            matrix_version=_optional_string(value.get("matrix_version")),
            family=(
                None if value.get("family") is None else ArtifactFamily(value["family"])
            ),
            claims=tuple(map(ArtifactClaimTrace.from_dict, value.get("claims", []))),
            stretch_decision=StretchDecision.from_dict(
                value.get("stretch_decision", {"is_stretch": False})
            ),
        )


@dataclass(frozen=True)
class ReviewEvidence:
    page: ReviewEvidencePage
    form_snapshot: dict[str, str]
    attachment_hashes: dict[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", ReviewEvidencePage(self.page))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewEvidence":
        return cls(
            page=ReviewEvidencePage(str(value["page"])),
            form_snapshot=_string_dict(value["form_snapshot"]),
            attachment_hashes=_string_dict(value["attachment_hashes"]),
        )


@dataclass(frozen=True)
class AnswerDisclosure:
    field_id: str
    visibility: AnswerVisibility

    def __post_init__(self) -> None:
        object.__setattr__(self, "visibility", AnswerVisibility(self.visibility))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnswerDisclosure":
        return cls(
            field_id=str(value["field_id"]),
            visibility=AnswerVisibility(str(value["visibility"])),
        )


@dataclass(frozen=True)
class FilledApplication:
    answers: dict[str, str]
    artifact_version: str
    unresolved_warnings: tuple[str, ...] = ()
    review_evidence: ReviewEvidence | None = None
    answer_disclosures: tuple[AnswerDisclosure, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FilledApplication":
        review_evidence = value.get("review_evidence")
        return cls(
            answers=_string_dict(value["answers"]),
            artifact_version=str(value["artifact_version"]),
            unresolved_warnings=tuple(map(str, value.get("unresolved_warnings", []))),
            review_evidence=(
                None
                if review_evidence is None
                else ReviewEvidence.from_dict(review_evidence)
            ),
            answer_disclosures=tuple(
                AnswerDisclosure.from_dict(item)
                for item in value.get("answer_disclosures", [])
            ),
        )


@dataclass(frozen=True)
class OfficialVacancy:
    version: str
    fingerprint: str
    freshness: str
    description: str
    available: bool = True
    verified: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfficialVacancy":
        return cls(
            version=str(value["version"]),
            fingerprint=str(value["fingerprint"]),
            freshness=str(value["freshness"]),
            description=str(value["description"]),
            available=bool(value.get("available", True)),
            verified=bool(value.get("verified", True)),
        )


@dataclass(frozen=True)
class PreSubmitManifest:
    version: str
    application_id: str
    opportunity_version: str
    role_fingerprint: str
    artifact_version: str
    artifact_hashes: dict[str, str]
    answers: dict[str, str]
    answer_hash: str
    review_evidence: ReviewEvidence | None
    answer_disclosures: tuple[AnswerDisclosure, ...]
    vacancy_freshness: str
    unresolved_warnings: tuple[str, ...]

    @property
    def form_snapshot_hash(self) -> str:
        snapshot = (
            self.review_evidence.form_snapshot
            if self.review_evidence is not None
            else self.answers
        )
        return _sha256(_canonical_json(snapshot))

    @property
    def review_page(self) -> str:
        return (
            self.review_evidence.page.value
            if self.review_evidence is not None
            else "legacy/unknown"
        )

    @property
    def public_summary_answers(self) -> tuple[tuple[str, str], ...]:
        public_ids = {
            item.field_id
            for item in self.answer_disclosures
            if item.visibility == AnswerVisibility.PUBLIC_SUMMARY
        }
        return tuple(
            (key, value)
            for key, value in sorted(self.answers.items())
            if key in public_ids
        )

    @classmethod
    def build(
        cls,
        *,
        application_id: str,
        opportunity_version: str,
        official_vacancy: OfficialVacancy,
        artifacts: PreparedArtifacts,
        filled: FilledApplication,
    ) -> "PreSubmitManifest":
        canonical_answers = _canonical_json(filled.answers)
        answer_hash = _sha256(canonical_answers)
        review_evidence = filled.review_evidence
        disclosures = _complete_answer_disclosures(
            filled.answers, filled.answer_disclosures
        )
        prepared_hashes = {
            "cv": artifacts.cv_hash,
            "cover_letter": artifacts.cover_letter_hash,
        }
        artifact_hashes = (
            dict(prepared_hashes)
            if review_evidence is None
            else dict(review_evidence.attachment_hashes)
        )
        if not artifact_hashes or any(
            prepared_hashes.get(kind) != digest
            for kind, digest in artifact_hashes.items()
        ):
            raise ValueError("Filled attachment hashes do not match prepared artifacts")
        payload = {
            "application_id": application_id,
            "opportunity_version": opportunity_version,
            "role_fingerprint": official_vacancy.fingerprint,
            "artifact_version": filled.artifact_version,
            "artifact_hashes": artifact_hashes,
            "answers": filled.answers,
            "answer_hash": answer_hash,
            "review_evidence": (
                None
                if review_evidence is None
                else {
                    "page": review_evidence.page.value,
                    "form_snapshot": review_evidence.form_snapshot,
                    "attachment_hashes": review_evidence.attachment_hashes,
                }
            ),
            "answer_disclosures": [
                {
                    "field_id": item.field_id,
                    "visibility": item.visibility.value,
                }
                for item in disclosures
            ],
            "vacancy_freshness": official_vacancy.freshness,
            "unresolved_warnings": list(filled.unresolved_warnings),
        }
        return cls(
            version=_sha256(_canonical_json(payload)),
            unresolved_warnings=tuple(filled.unresolved_warnings),
            review_evidence=review_evidence,
            answer_disclosures=disclosures,
            **{
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "unresolved_warnings",
                    "review_evidence",
                    "answer_disclosures",
                }
            },
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PreSubmitManifest":
        return cls(
            version=str(value["version"]),
            application_id=str(value["application_id"]),
            opportunity_version=str(value["opportunity_version"]),
            role_fingerprint=str(value["role_fingerprint"]),
            artifact_version=str(value["artifact_version"]),
            artifact_hashes=_string_dict(value["artifact_hashes"]),
            answers=_string_dict(value["answers"]),
            answer_hash=str(value["answer_hash"]),
            review_evidence=(
                None
                if value.get("review_evidence") is None
                else ReviewEvidence.from_dict(value["review_evidence"])
            ),
            answer_disclosures=_complete_answer_disclosures(
                _string_dict(value["answers"]),
                tuple(
                    AnswerDisclosure.from_dict(item)
                    for item in value.get("answer_disclosures", [])
                ),
            ),
            vacancy_freshness=str(value["vacancy_freshness"]),
            unresolved_warnings=tuple(map(str, value.get("unresolved_warnings", []))),
        )


@dataclass(frozen=True)
class SubmissionEvidence:
    captured_at: str
    verified_by: tuple[SubmissionVerificationKind, ...]
    confirmation_page: str | None = None
    confirmation_id: str | None = None
    ats_application_id: str | None = None
    ats_status: str | None = None
    email_receipt_id: str | None = None
    email_receipt_received_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verified_by",
            tuple(SubmissionVerificationKind(item) for item in self.verified_by),
        )
        _require_iso_timestamp(self.captured_at, "Submission evidence timestamp")
        if (
            SubmissionVerificationKind.CONFIRMATION_PAGE in self.verified_by
            and not self.confirmation_page
        ):
            raise ValueError("Confirmation-page verification requires page evidence")
        if (
            SubmissionVerificationKind.CONFIRMATION_ID in self.verified_by
            and not self.confirmation_id
        ):
            raise ValueError("Confirmation verification requires an identifier")
        if SubmissionVerificationKind.ATS_SUBMITTED in self.verified_by:
            if (
                not self.ats_application_id
                or not self.ats_status
                or self.ats_status.casefold()
                not in {
                    "application received",
                    "application submitted",
                    "received",
                    "submitted",
                }
            ):
                raise ValueError("ATS verification requires a submitted status")
        if SubmissionVerificationKind.EMAIL_RECEIPT in self.verified_by:
            if not self.email_receipt_id or not self.email_receipt_received_at:
                raise ValueError("Email verification requires a dated receipt")
            _require_iso_timestamp(
                self.email_receipt_received_at, "Email receipt timestamp"
            )

    @property
    def has_verification_marker(self) -> bool:
        return bool(self.verified_by)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubmissionEvidence":
        return cls(
            captured_at=str(value.get("captured_at", "")),
            verified_by=tuple(
                SubmissionVerificationKind(str(item))
                for item in value.get("verified_by", [])
            ),
            confirmation_page=_optional_string(value.get("confirmation_page")),
            confirmation_id=_optional_string(value.get("confirmation_id")),
            ats_application_id=_optional_string(value.get("ats_application_id")),
            ats_status=_optional_string(value.get("ats_status")),
            email_receipt_id=_optional_string(value.get("email_receipt_id")),
            email_receipt_received_at=_optional_string(
                value.get("email_receipt_received_at")
            ),
        )


@dataclass(frozen=True)
class SubmissionOutcome:
    status: SubmissionStatus
    confirmation_id: str | None = None
    evidence: SubmissionEvidence | None = None
    recorded_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SubmissionStatus(self.status))
        if self.recorded_at is not None:
            _require_iso_timestamp(self.recorded_at, "Submission outcome timestamp")
        evidence = self.evidence
        if evidence is not None:
            if (
                self.confirmation_id is not None
                and evidence.confirmation_id is not None
                and self.confirmation_id != evidence.confirmation_id
            ):
                raise ValueError("Submission confirmation identifiers disagree")
            if self.confirmation_id is None and evidence.confirmation_id is not None:
                object.__setattr__(self, "confirmation_id", evidence.confirmation_id)
        if self.status == SubmissionStatus.VERIFIED and (
            evidence is None or not evidence.has_verification_marker
        ):
            raise ValueError("Verified submission requires captured evidence")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubmissionOutcome":
        confirmation = value.get("confirmation_id")
        return cls(
            status=SubmissionStatus(str(value["status"])),
            confirmation_id=None if confirmation is None else str(confirmation),
            evidence=(
                None
                if value.get("evidence") is None
                else SubmissionEvidence.from_dict(value["evidence"])
            ),
            recorded_at=_optional_string(value.get("recorded_at")),
        )


@dataclass(frozen=True)
class LifecycleEvent:
    state: LifecycleState
    occurred_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LifecycleEvent":
        return cls(LifecycleState(str(value["state"])), str(value["occurred_at"]))


@dataclass(frozen=True)
class AuthorizationScope:
    application_id: str
    action: WorkflowAction
    version: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthorizationScope":
        return cls(
            application_id=str(value["application_id"]),
            action=WorkflowAction(str(value["action"])),
            version=str(value["version"]),
        )


@dataclass(frozen=True)
class AuthorizationRecord:
    token: str
    scope: AuthorizationScope
    actor: str
    issued_at: str
    expires_at: str
    consumed_at: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthorizationRecord":
        return cls(
            token=str(value["token"]),
            scope=AuthorizationScope.from_dict(value["scope"]),
            actor=str(value["actor"]),
            issued_at=str(value["issued_at"]),
            expires_at=str(value["expires_at"]),
            consumed_at=_optional_string(value.get("consumed_at")),
            invalidated_at=_optional_string(value.get("invalidated_at")),
            invalidation_reason=_optional_string(value.get("invalidation_reason")),
        )


@dataclass(frozen=True)
class ApprovalRecord:
    token: str
    scope: AuthorizationScope
    actor: str
    authorized_at: str
    expires_at: str
    invalidated_at: str | None = None
    invalidation_reason: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.invalidated_at is None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalRecord":
        return cls(
            token=str(value["token"]),
            scope=AuthorizationScope.from_dict(value["scope"]),
            actor=str(value["actor"]),
            authorized_at=str(value["authorized_at"]),
            expires_at=str(value["expires_at"]),
            invalidated_at=_optional_string(value.get("invalidated_at")),
            invalidation_reason=_optional_string(value.get("invalidation_reason")),
        )


@dataclass(frozen=True)
class OperationIntent:
    intent_id: str
    action: WorkflowAction
    version: str
    created_at: str
    completed_at: str | None = None
    cancelled_at: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.completed_at is None and self.cancelled_at is None

    def cancel(self, cancelled_at: str) -> "OperationIntent":
        return replace(self, cancelled_at=cancelled_at)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OperationIntent":
        return cls(
            intent_id=str(value["intent_id"]),
            action=WorkflowAction(str(value["action"])),
            version=str(value["version"]),
            created_at=str(value["created_at"]),
            completed_at=_optional_string(value.get("completed_at")),
            cancelled_at=_optional_string(value.get("cancelled_at")),
        )


@dataclass(frozen=True)
class SubmissionIntent:
    intent_id: str
    manifest_version: str
    created_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubmissionIntent":
        return cls(
            **{
                key: str(value[key])
                for key in ("intent_id", "manifest_version", "created_at")
            }
        )


@dataclass(frozen=True)
class CorrespondenceEvent:
    event_id: str
    application_id: str
    message_id: str
    thread_id: str
    classification: CorrespondenceClassification
    sender: str
    subject: str
    received_at: str
    recorded_at: str
    summary: str
    sender_trust_evidence: CorrespondenceTrustEvidence | None = None
    evidence_role: str | None = None
    draft_id: str | None = None
    classification_request_id: str | None = None
    previous_state: LifecycleState | None = None
    resulting_state: LifecycleState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "classification",
            CorrespondenceClassification(self.classification),
        )
        if self.sender_trust_evidence is not None:
            object.__setattr__(
                self,
                "sender_trust_evidence",
                CorrespondenceTrustEvidence(self.sender_trust_evidence),
            )
        if self.previous_state is not None:
            object.__setattr__(
                self, "previous_state", LifecycleState(self.previous_state)
            )
        if self.resulting_state is not None:
            object.__setattr__(
                self, "resulting_state", LifecycleState(self.resulting_state)
            )
        _require_iso_timestamp(self.received_at, "Correspondence received timestamp")
        _require_iso_timestamp(self.recorded_at, "Correspondence recorded timestamp")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CorrespondenceEvent":
        return cls(
            event_id=str(value["event_id"]),
            application_id=str(value["application_id"]),
            message_id=str(value["message_id"]),
            thread_id=str(value["thread_id"]),
            classification=CorrespondenceClassification(str(value["classification"])),
            sender=str(value["sender"]),
            subject=str(value["subject"]),
            received_at=str(value["received_at"]),
            recorded_at=str(value["recorded_at"]),
            summary=str(value["summary"]),
            sender_trust_evidence=(
                None
                if value.get("sender_trust_evidence") is None
                else CorrespondenceTrustEvidence(str(value["sender_trust_evidence"]))
            ),
            evidence_role=_optional_string(value.get("evidence_role")),
            draft_id=_optional_string(value.get("draft_id")),
            classification_request_id=_optional_string(
                value.get("classification_request_id")
            ),
            previous_state=(
                None
                if value.get("previous_state") is None
                else LifecycleState(str(value["previous_state"]))
            ),
            resulting_state=(
                None
                if value.get("resulting_state") is None
                else LifecycleState(str(value["resulting_state"]))
            ),
        )


@dataclass(frozen=True)
class ActionCommand:
    token: str
    scope: AuthorizationScope


@dataclass(frozen=True)
class ApplicationSnapshot:
    application_id: str
    opportunity: dict[str, Any]
    opportunity_version: str
    lifecycle_state: LifecycleState
    authorization_version: str
    history: tuple[LifecycleEvent, ...]
    authorizations: tuple[AuthorizationRecord, ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    official_vacancy: OfficialVacancy | None = None
    artifacts: PreparedArtifacts | None = None
    artifacts_expires_at: str | None = None
    manifest: PreSubmitManifest | None = None
    operation_intents: tuple[OperationIntent, ...] = ()
    submission_intents: tuple[SubmissionIntent, ...] = ()
    correspondence: tuple[CorrespondenceEvent, ...] = ()
    intervention: InterventionRecord | None = None
    uncertain_submission: UncertainSubmissionRecord | None = None
    outcome: SubmissionOutcome | None = None
    operational_status: OperationalStatus | None = None
    capacity_exception: PreparationCapacityException | None = None
    preparation_reminders: tuple[PreparationReminder, ...] = ()
    prior_applications: tuple[PriorApplicationEvidence, ...] = ()
    package_publication_pending: bool = False

    @property
    def next_action(self) -> WorkflowAction | None:
        if self.intervention is not None or self.uncertain_submission is not None:
            return None
        if self.outcome is not None or self.submission_intents:
            return None
        if self.operational_status == OperationalStatus.PREPARATION_FAILED:
            return None
        if any(intent.is_pending for intent in self.operation_intents):
            return None
        if self.operational_status in {
            OperationalStatus.EXPIRED_PREPARATION,
            OperationalStatus.VACANCY_CHANGED,
            OperationalStatus.MASTER_CV_RELOADED,
        }:
            return WorkflowAction.PREPARE
        if self.manifest is not None:
            return WorkflowAction.SUBMIT
        if self.artifacts is not None:
            return WorkflowAction.FILL
        return WorkflowAction.PREPARE

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApplicationSnapshot":
        return cls(
            application_id=str(value["application_id"]),
            opportunity=dict(value["opportunity"]),
            opportunity_version=str(value["opportunity_version"]),
            lifecycle_state=LifecycleState(str(value["lifecycle_state"])),
            authorization_version=str(value["authorization_version"]),
            history=tuple(map(LifecycleEvent.from_dict, value["history"])),
            authorizations=tuple(
                map(AuthorizationRecord.from_dict, value.get("authorizations", []))
            ),
            approvals=tuple(map(ApprovalRecord.from_dict, value.get("approvals", []))),
            official_vacancy=_optional_record(
                value.get("official_vacancy"), OfficialVacancy
            ),
            artifacts=_optional_record(value.get("artifacts"), PreparedArtifacts),
            artifacts_expires_at=_optional_string(value.get("artifacts_expires_at")),
            manifest=_optional_record(value.get("manifest"), PreSubmitManifest),
            operation_intents=tuple(
                map(OperationIntent.from_dict, value.get("operation_intents", []))
            ),
            submission_intents=tuple(
                map(SubmissionIntent.from_dict, value.get("submission_intents", []))
            ),
            correspondence=tuple(
                map(CorrespondenceEvent.from_dict, value.get("correspondence", []))
            ),
            intervention=_optional_record(
                value.get("intervention"), InterventionRecord
            ),
            uncertain_submission=_optional_record(
                value.get("uncertain_submission"), UncertainSubmissionRecord
            ),
            outcome=_optional_record(value.get("outcome"), SubmissionOutcome),
            operational_status=(
                None
                if value.get("operational_status") is None
                else OperationalStatus(str(value["operational_status"]))
            ),
            capacity_exception=_optional_record(
                value.get("capacity_exception"), PreparationCapacityException
            ),
            preparation_reminders=tuple(
                map(
                    PreparationReminder.from_dict,
                    value.get("preparation_reminders", []),
                )
            ),
            prior_applications=tuple(
                map(
                    PriorApplicationEvidence.from_dict,
                    value.get("prior_applications", []),
                )
            ),
            package_publication_pending=bool(
                value.get("package_publication_pending", False)
            ),
        )


@dataclass(frozen=True)
class CommandResult:
    status: CommandStatus
    lifecycle_state: LifecycleState | None
    next_action: WorkflowAction | None


def _canonical_material_value(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if item is None:
            return None
        if isinstance(item, str):
            return " ".join(item.split()).casefold()
        if isinstance(item, Mapping):
            return {
                " ".join(str(key).split()).casefold(): normalize(nested)
                for key, nested in sorted(
                    item.items(), key=lambda pair: str(pair[0]).casefold()
                )
            }
        if isinstance(item, (list, tuple)):
            return [normalize(nested) for nested in item]
        if isinstance(item, set):
            normalized = [normalize(nested) for nested in item]
            return sorted(normalized, key=_canonical_json)
        if isinstance(item, (bool, int, float)):
            return item
        return " ".join(str(item).split()).casefold()

    return _canonical_json(normalize(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _string_dict(value: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()}


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_record(value: Any, record_type):
    return None if value is None else record_type.from_dict(value)


def _complete_answer_disclosures(
    answers: Mapping[str, str],
    disclosures: tuple[AnswerDisclosure, ...],
) -> tuple[AnswerDisclosure, ...]:
    disclosed_ids = [item.field_id for item in disclosures]
    if len(disclosed_ids) != len(set(disclosed_ids)):
        raise ValueError("ATS answer disclosure metadata must be unique")
    unknown = set(disclosed_ids) - set(answers)
    if unknown:
        raise ValueError("ATS answer disclosure references an unknown field")
    completed = list(disclosures)
    completed.extend(
        AnswerDisclosure(field_id, AnswerVisibility.LOCAL_ONLY)
        for field_id in sorted(set(answers) - set(disclosed_ids))
    )
    return tuple(sorted(completed, key=lambda item: item.field_id))


def _require_iso_timestamp(value: str, label: str) -> None:
    if not value:
        raise ValueError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
