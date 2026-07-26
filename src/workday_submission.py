"""Evidence-driven Workday submission adapter for the final human gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Protocol

from application_domain import (
    FilledApplication,
    PreSubmitManifest,
    PreparedArtifacts,
    SubmissionEvidence,
    SubmissionOutcome,
    SubmissionVerificationKind,
)
from application_interventions import (
    SubmissionInspection,
    SubmissionInspectionSource,
    SubmissionInspectionStatus,
)


@dataclass(frozen=True)
class WorkdaySubmissionCapture:
    application_id: str
    manifest_version: str
    captured_at: str
    confirmation_page: str | None = None
    confirmation_marker: "WorkdayConfirmationMarker | None" = None
    confirmation_id: str | None = None
    ats_application_id: str | None = None
    ats_status: str | None = None
    email_receipt_id: str | None = None
    email_receipt_received_at: str | None = None
    sources_checked: tuple[str, ...] = ()
    sources_unavailable: tuple[str, ...] = ()
    inspection_complete: bool = True

    def __post_init__(self) -> None:
        if self.confirmation_marker is not None:
            object.__setattr__(
                self,
                "confirmation_marker",
                WorkdayConfirmationMarker(self.confirmation_marker),
            )


@dataclass(frozen=True)
class CareerMailboxReceipt:
    message_id: str
    received_at: str


@dataclass(frozen=True)
class ScopedWorkdayReview:
    """Trusted fill-session scope plus a fresh observation of the review page."""

    application_id: str
    filled_url: str
    current_url: str
    answers: Mapping[str, str]
    attachment_hashes: Mapping[str, str]
    filled_attachment_names: Mapping[str, str]
    current_attachment_names: Mapping[str, str]


@dataclass(frozen=True)
class WorkdayConfirmationCapture:
    page_text: str
    positive_marker: "WorkdayConfirmationMarker | None" = None
    confirmation_id: str | None = None
    ats_application_id: str | None = None
    ats_status: str | None = None

    def __post_init__(self) -> None:
        if self.positive_marker is not None:
            object.__setattr__(
                self,
                "positive_marker",
                WorkdayConfirmationMarker(self.positive_marker),
            )


class WorkdayConfirmationMarker(str, Enum):
    APPLICATION_SUBMITTED = "application_submitted"


class WorkdayFiller(Protocol):
    def fill(
        self, application_id: str, intent_id: str, artifacts: PreparedArtifacts
    ) -> FilledApplication: ...
    def restore_submission(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> None: ...


class WorkdaySubmissionSession(Protocol):
    """One object owns both review capture and the only submit interaction."""

    def capture_submission_review(
        self, application_id: str
    ) -> ScopedWorkdayReview: ...
    def submission_review_is_visible(self) -> bool: ...
    def assert_pre_action_safe(self, guarded_action: str) -> None: ...
    def click_submission(self) -> None: ...
    def capture_submission_confirmation(self) -> WorkdayConfirmationCapture: ...


class WorkdaySubmissionBrowser(Protocol):
    """A live browser capability scoped to the already-reviewed Workday form."""

    def submit_reviewed_application(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> WorkdaySubmissionCapture: ...
    def validate_review(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> None: ...
    def inspect_submission_evidence(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> WorkdaySubmissionCapture: ...


class CareerMailboxReceiptReader(Protocol):
    def find_submission_receipt(
        self, application_id: str, confirmation_id: str | None
    ) -> CareerMailboxReceipt | None: ...


class LiveWorkdaySubmissionBrowser:
    """Click Submit on a live reviewed Workday page and capture positive evidence."""

    def __init__(
        self,
        *,
        now,
        mailbox: CareerMailboxReceiptReader | None = None,
        session: WorkdaySubmissionSession | None = None,
    ) -> None:
        self._now = now
        self._mailbox = mailbox
        self._session = session

    def bind_session(self, session: WorkdaySubmissionSession) -> None:
        if self._session is not None and self._session is not session:
            raise RuntimeError("Workday submission session is already bound")
        self._session = session

    def validate_review(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> None:
        if self._session is None:
            raise RuntimeError("Workday trusted fill session is unavailable")
        if not self._session.submission_review_is_visible():
            raise RuntimeError("Workday is not on the reviewed application page")
        _validate_live_review(
            application_id,
            manifest,
            self._session.capture_submission_review(application_id),
        )

    def submit_reviewed_application(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> WorkdaySubmissionCapture:
        self.validate_review(application_id, manifest)
        assert self._session is not None
        self._session.assert_pre_action_safe("submit")
        self._session.click_submission()
        return self._inspect_after_submit(application_id, manifest)

    def inspect_submission_evidence(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> WorkdaySubmissionCapture:
        """Read ATS/mailbox evidence without validating review or clicking Submit."""

        return self._inspect_after_submit(application_id, manifest)

    def _inspect_after_submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> WorkdaySubmissionCapture:
        if self._session is None:
            raise RuntimeError("Workday trusted fill session is unavailable")
        checked = []
        unavailable = []
        try:
            confirmation = self._session.capture_submission_confirmation()
            if not isinstance(confirmation, WorkdayConfirmationCapture):
                raise RuntimeError("Workday confirmation capture is invalid")
            checked.append(SubmissionInspectionSource.ATS.value)
        except Exception:
            confirmation = WorkdayConfirmationCapture(page_text="")
            unavailable.append(SubmissionInspectionSource.ATS.value)
        confirmation_text = str(confirmation.page_text or "").strip()
        confirmation_id = confirmation.confirmation_id
        receipt = None
        if self._mailbox is not None:
            try:
                receipt = self._mailbox.find_submission_receipt(
                    application_id, confirmation_id
                )
                checked.append(SubmissionInspectionSource.CAREER_MAILBOX.value)
            except Exception:
                unavailable.append(
                    SubmissionInspectionSource.CAREER_MAILBOX.value
                )
        else:
            unavailable.append(SubmissionInspectionSource.CAREER_MAILBOX.value)
        return WorkdaySubmissionCapture(
            application_id=application_id,
            manifest_version=manifest.version,
            captured_at=_timestamp(self._now()),
            confirmation_page=confirmation_text or None,
            confirmation_marker=confirmation.positive_marker,
            confirmation_id=confirmation_id,
            ats_application_id=confirmation.ats_application_id,
            ats_status=confirmation.ats_status,
            email_receipt_id=None if receipt is None else receipt.message_id,
            email_receipt_received_at=(
                None if receipt is None else receipt.received_at
            ),
            sources_checked=tuple(checked),
            sources_unavailable=tuple(unavailable),
            inspection_complete=not unavailable,
        )


class WorkdayApplicationAdapter:
    """Combine supported fill with one evidence-capturing external submit action."""

    def __init__(
        self, *, filler: WorkdayFiller, submission_browser: WorkdaySubmissionBrowser
    ) -> None:
        self._filler = filler
        self._submission_browser = submission_browser
        bind_session = getattr(submission_browser, "bind_session", None)
        session_methods = (
            "capture_submission_review",
            "submission_review_is_visible",
            "assert_pre_action_safe",
            "click_submission",
            "capture_submission_confirmation",
        )
        if callable(bind_session) and all(
            callable(getattr(filler, method, None)) for method in session_methods
        ):
            bind_session(filler)

    def fill(
        self, application_id: str, intent_id: str, artifacts: PreparedArtifacts
    ) -> FilledApplication:
        return self._filler.fill(application_id, intent_id, artifacts)

    def submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> SubmissionOutcome:
        capture = self._submission_browser.submit_reviewed_application(
            application_id, manifest
        )
        if (
            capture.application_id != application_id
            or capture.manifest_version != manifest.version
        ):
            raise RuntimeError("Workday submission evidence has the wrong scope")
        verified_by = _verification_kinds(capture)
        if not verified_by:
            return SubmissionOutcome(
                status="uncertain", recorded_at=capture.captured_at
            )
        evidence = SubmissionEvidence(
            captured_at=capture.captured_at,
            verified_by=verified_by,
            confirmation_page=capture.confirmation_page,
            confirmation_id=capture.confirmation_id,
            ats_application_id=capture.ats_application_id,
            ats_status=capture.ats_status,
            email_receipt_id=capture.email_receipt_id,
            email_receipt_received_at=capture.email_receipt_received_at,
        )
        return SubmissionOutcome(status="verified", evidence=evidence)

    def validate_submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> bool:
        self._filler.restore_submission(application_id, manifest)
        self._submission_browser.validate_review(application_id, manifest)
        return True

    def inspect_submission(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> SubmissionInspection:
        inspect = getattr(
            self._submission_browser, "inspect_submission_evidence", None
        )
        if not callable(inspect):
            raise RuntimeError("Workday submission inspection is unavailable")
        try:
            capture = inspect(application_id, manifest)
        except Exception:
            raise RuntimeError("Workday submission inspection failed safely") from None
        if (
            capture.application_id != application_id
            or capture.manifest_version != manifest.version
        ):
            raise RuntimeError("Workday inspection evidence has the wrong scope")
        required = {
            SubmissionInspectionSource.ATS,
            SubmissionInspectionSource.CAREER_MAILBOX,
        }
        checked = {
            SubmissionInspectionSource(
                source.value
                if isinstance(source, SubmissionInspectionSource)
                else str(source)
            )
            for source in capture.sources_checked
        }
        unavailable = {
            SubmissionInspectionSource(
                source.value
                if isinstance(source, SubmissionInspectionSource)
                else str(source)
            )
            for source in capture.sources_unavailable
        }
        unavailable.update(required - checked)
        complete = (
            capture.inspection_complete
            and required.issubset(checked)
            and not unavailable
        )
        verified_by = _verification_kinds(capture)
        if not verified_by:
            return SubmissionInspection(
                status=(
                    SubmissionInspectionStatus.NO_POSITIVE_EVIDENCE
                    if complete
                    else SubmissionInspectionStatus.INCOMPLETE
                ),
                checked_at=capture.captured_at,
                sources_checked=tuple(sorted(checked, key=lambda item: item.value)),
                sources_unavailable=tuple(
                    sorted(unavailable, key=lambda item: item.value)
                ),
            )
        evidence = SubmissionEvidence(
            captured_at=capture.captured_at,
            verified_by=verified_by,
            confirmation_page=capture.confirmation_page,
            confirmation_id=capture.confirmation_id,
            ats_application_id=capture.ats_application_id,
            ats_status=capture.ats_status,
            email_receipt_id=capture.email_receipt_id,
            email_receipt_received_at=capture.email_receipt_received_at,
        )
        return SubmissionInspection(
            status=SubmissionInspectionStatus.VERIFIED,
            checked_at=capture.captured_at,
            sources_checked=tuple(sorted(checked, key=lambda item: item.value)),
            sources_unavailable=tuple(
                sorted(unavailable, key=lambda item: item.value)
            ),
            evidence=evidence,
        )


def _verification_kinds(
    capture: WorkdaySubmissionCapture,
) -> tuple[SubmissionVerificationKind, ...]:
    values = []
    if (
        capture.confirmation_page
        and capture.confirmation_marker
        == WorkdayConfirmationMarker.APPLICATION_SUBMITTED
    ):
        values.append(SubmissionVerificationKind.CONFIRMATION_PAGE)
    if (
        capture.confirmation_id
        and capture.confirmation_marker
        == WorkdayConfirmationMarker.APPLICATION_SUBMITTED
    ):
        values.append(SubmissionVerificationKind.CONFIRMATION_ID)
    if (
        capture.ats_application_id
        and capture.ats_status
        and capture.ats_status.casefold()
        in {
            "application received",
            "application submitted",
            "received",
            "submitted",
        }
    ):
        values.append(SubmissionVerificationKind.ATS_SUBMITTED)
    if capture.email_receipt_id and capture.email_receipt_received_at:
        values.append(SubmissionVerificationKind.EMAIL_RECEIPT)
    return tuple(values)


def _validate_live_review(
    application_id: str,
    manifest: PreSubmitManifest,
    observed: ScopedWorkdayReview,
) -> None:
    if manifest.review_evidence is None:
        raise RuntimeError("Workday manifest has no exact review evidence")
    if observed.application_id != application_id:
        raise RuntimeError("Workday review application identity changed")
    if not observed.filled_url or observed.current_url != observed.filled_url:
        raise RuntimeError("Workday review page changed after the trusted fill")
    if dict(observed.answers) != manifest.review_evidence.form_snapshot:
        raise RuntimeError("Workday review answers changed before submit")
    if dict(observed.attachment_hashes) != manifest.review_evidence.attachment_hashes:
        raise RuntimeError("Workday review attachments changed before submit")
    if dict(observed.current_attachment_names) != dict(
        observed.filled_attachment_names
    ):
        raise RuntimeError("Workday review attachment names changed before submit")


def _timestamp(value) -> str:
    if not isinstance(value, datetime):
        raise RuntimeError("Workday submission clock is unavailable")
    return value.isoformat()


__all__ = [
    "CareerMailboxReceipt",
    "CareerMailboxReceiptReader",
    "LiveWorkdaySubmissionBrowser",
    "ScopedWorkdayReview",
    "WorkdayApplicationAdapter",
    "WorkdayConfirmationCapture",
    "WorkdayConfirmationMarker",
    "WorkdaySubmissionBrowser",
    "WorkdaySubmissionCapture",
    "WorkdaySubmissionSession",
]
