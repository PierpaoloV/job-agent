"""Typed safety records for human intervention and uncertain submissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class InterventionKind(str, Enum):
    CAPTCHA = "captcha"
    NON_EMAIL_MFA = "non_email_mfa"
    UNUSUAL_CONSENT = "unusual_consent"
    SITE_RESTRICTION = "site_restriction"
    UNSUPPORTED_CONTROL = "unsupported_control"


class InterventionContinuationKind(str, Enum):
    PENDING_AUTHORIZATION = "pending_authorization"
    OPERATION_INTENT = "operation_intent"
    SUBMISSION_INTENT = "submission_intent"


@dataclass(frozen=True)
class InterventionContinuation:
    kind: InterventionContinuationKind
    reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", InterventionContinuationKind(self.kind))
        if not self.reference.strip():
            raise ValueError("Intervention continuation reference is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterventionContinuation":
        return cls(
            kind=InterventionContinuationKind(str(value["kind"])),
            reference=str(value["reference"]),
        )


class SubmissionInspectionStatus(str, Enum):
    VERIFIED = "verified"
    NO_POSITIVE_EVIDENCE = "no_positive_evidence"
    INCOMPLETE = "incomplete"


class SubmissionInspectionSource(str, Enum):
    ATS = "ats"
    CAREER_MAILBOX = "career_mailbox"


class BrowserInterventionRequired(RuntimeError):
    """A supported adapter stopped before a guarded browser mutation.

    Raising this exception is a safety claim: the browser session is still
    available to the human and the guarded action has not happened.  Unsafe or
    post-action ambiguity must use the uncertain-submission path instead.
    """

    def __init__(
        self,
        *,
        kind: InterventionKind,
        explanation: str,
        browser_ready: bool,
        guarded_action_started: bool = False,
    ) -> None:
        self.kind = InterventionKind(kind)
        self.explanation = str(explanation).strip()
        self.browser_ready = bool(browser_ready)
        self.guarded_action_started = bool(guarded_action_started)
        if not self.explanation:
            raise ValueError("Intervention explanation is required")
        if not self.browser_ready:
            raise ValueError("Intervention requires a browser ready for the human")
        if self.guarded_action_started:
            raise ValueError(
                "A started guarded action is an uncertain outcome, not an intervention"
            )
        super().__init__(self.explanation)


@dataclass(frozen=True)
class SubmissionInspection:
    status: SubmissionInspectionStatus
    checked_at: str
    sources_checked: tuple[SubmissionInspectionSource, ...]
    sources_unavailable: tuple[SubmissionInspectionSource, ...] = ()
    evidence: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SubmissionInspectionStatus(self.status))
        _require_timestamp(self.checked_at, "Submission inspection timestamp")
        checked = tuple(
            SubmissionInspectionSource(_source_value(source))
            for source in self.sources_checked
            if _source_value(source)
        )
        unavailable = tuple(
            SubmissionInspectionSource(_source_value(source))
            for source in self.sources_unavailable
            if _source_value(source)
        )
        if not checked and not unavailable:
            raise ValueError("Submission inspection must account for its sources")
        if len(checked) != len(set(checked)) or len(unavailable) != len(
            set(unavailable)
        ):
            raise ValueError("Submission inspection sources must be unique")
        if set(checked) & set(unavailable):
            raise ValueError("A source cannot be both checked and unavailable")
        object.__setattr__(self, "sources_checked", checked)
        object.__setattr__(self, "sources_unavailable", unavailable)
        if self.status == SubmissionInspectionStatus.VERIFIED:
            if self.evidence is None or not getattr(
                self.evidence, "has_verification_marker", False
            ):
                raise ValueError("Verified inspection requires typed positive evidence")
        elif self.evidence is not None:
            raise ValueError("Non-verified inspection cannot carry positive evidence")

    @property
    def permits_human_resolution(self) -> bool:
        required = {
            SubmissionInspectionSource.ATS,
            SubmissionInspectionSource.CAREER_MAILBOX,
        }
        return (
            self.status == SubmissionInspectionStatus.NO_POSITIVE_EVIDENCE
            and required.issubset(self.sources_checked)
            and not self.sources_unavailable
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubmissionInspection":
        evidence = value.get("evidence")
        if evidence is not None:
            # Imported lazily so this module remains usable from application_domain.
            from application_domain import SubmissionEvidence

            evidence = SubmissionEvidence.from_dict(evidence)
        return cls(
            status=SubmissionInspectionStatus(str(value["status"])),
            checked_at=str(value["checked_at"]),
            sources_checked=tuple(map(str, value.get("sources_checked", []))),
            sources_unavailable=tuple(
                map(str, value.get("sources_unavailable", []))
            ),
            evidence=evidence,
        )


@dataclass(frozen=True)
class InterventionRecord:
    intervention_id: str
    kind: InterventionKind
    action: str
    explanation: str
    detected_at: str
    browser_ready: bool
    resume_token: str
    actor: str
    continuation: InterventionContinuation

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", InterventionKind(self.kind))
        _require_timestamp(self.detected_at, "Intervention timestamp")
        if not self.intervention_id or not self.action or not self.resume_token:
            raise ValueError("Intervention scope is incomplete")
        if not self.explanation or not self.actor:
            raise ValueError("Intervention explanation and actor are required")
        if not self.browser_ready:
            raise ValueError("Intervention must preserve a human-ready browser")
        if not isinstance(self.continuation, InterventionContinuation):
            object.__setattr__(
                self,
                "continuation",
                InterventionContinuation.from_dict(self.continuation),
            )
        if (
            self.continuation.kind == InterventionContinuationKind.OPERATION_INTENT
            and self.action != "Compila"
        ):
            raise ValueError("Only a fill operation can resume from an operation intent")
        if (
            self.continuation.kind
            in {
                InterventionContinuationKind.PENDING_AUTHORIZATION,
                InterventionContinuationKind.SUBMISSION_INTENT,
            }
        ) and self.action != "Invia":
            raise ValueError("Submission intervention continuation has the wrong action")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterventionRecord":
        continuation_value = value.get("continuation")
        if continuation_value is not None:
            continuation = InterventionContinuation.from_dict(continuation_value)
        else:
            legacy = tuple(
                (kind, str(reference))
                for kind, reference in (
                    (
                        InterventionContinuationKind.PENDING_AUTHORIZATION,
                        value.get("pending_authorization_token"),
                    ),
                    (
                        InterventionContinuationKind.OPERATION_INTENT,
                        value.get("operation_intent_id"),
                    ),
                    (
                        InterventionContinuationKind.SUBMISSION_INTENT,
                        value.get("submission_intent_id"),
                    ),
                )
                if reference is not None
            )
            if len(legacy) != 1:
                raise ValueError("Intervention must identify exactly one continuation")
            continuation = InterventionContinuation(
                kind=legacy[0][0], reference=legacy[0][1]
            )
        return cls(
            intervention_id=str(value["intervention_id"]),
            kind=InterventionKind(str(value["kind"])),
            action=str(value["action"]),
            explanation=str(value["explanation"]),
            detected_at=str(value["detected_at"]),
            browser_ready=bool(value["browser_ready"]),
            resume_token=str(value["resume_token"]),
            actor=str(value["actor"]),
            continuation=continuation,
        )


@dataclass(frozen=True)
class UncertainSubmissionRecord:
    version: str
    manifest_version: str
    submission_intent_id: str
    inspection: SubmissionInspection
    resolution_token: str | None
    actor: str

    def __post_init__(self) -> None:
        if not self.version or not self.manifest_version or not self.submission_intent_id:
            raise ValueError("Uncertain submission scope is incomplete")
        if not self.actor:
            raise ValueError("Uncertain submission actor is required")
        if self.inspection.permits_human_resolution != bool(self.resolution_token):
            raise ValueError(
                "A retry resolution is offered only after a complete no-evidence inspection"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UncertainSubmissionRecord":
        return cls(
            version=str(value["version"]),
            manifest_version=str(value["manifest_version"]),
            submission_intent_id=str(value["submission_intent_id"]),
            inspection=SubmissionInspection.from_dict(value["inspection"]),
            resolution_token=_optional(value.get("resolution_token")),
            actor=str(value["actor"]),
        )


def _require_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def _source_value(value: Any) -> str:
    if isinstance(value, SubmissionInspectionSource):
        return value.value
    return str(value).strip().casefold()


__all__ = [
    "BrowserInterventionRequired",
    "InterventionKind",
    "InterventionRecord",
    "SubmissionInspection",
    "SubmissionInspectionStatus",
    "SubmissionInspectionSource",
    "UncertainSubmissionRecord",
]
