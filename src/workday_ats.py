"""Local-only Workday fill adapter that can reach review but cannot submit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from application_domain import (
    AnswerDisclosure,
    AnswerVisibility,
    FilledApplication,
    PreSubmitManifest,
    PreparedArtifacts,
    ReviewEvidence,
    ReviewEvidencePage,
)
from application_interventions import (
    BrowserInterventionRequired,
    InterventionKind,
    InterventionRecord,
)
from ats_answer_service import (
    AnswerServiceError,
    ApplicationFieldReference,
    AtsQuestion,
    LocalAtsAnswerService,
    LocalFormAnswer,
    QuestionMeaning,
    TelegramAnswerQuestion,
)
from macos_keychain import generate_ats_password
from workday_submission import ScopedWorkdayReview, WorkdayConfirmationCapture


DEDICATED_BROWSER_PROFILE = "Job Applications"


class AtsFieldKind(str, Enum):
    CV = "cv"
    COVER_LETTER = "cover_letter"


class AtsControlKind(str, Enum):
    TEXT = "text"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    UNSUPPORTED = "unsupported"


class BrowserPage(str, Enum):
    APPLICATION = "application"
    REVIEW = "review"
    CONFIRMATION = "confirmation"


@dataclass(frozen=True)
class AtsField:
    field_id: str
    prompt: str
    mandatory: bool
    meaning: QuestionMeaning = QuestionMeaning.CUSTOM
    standardized_voluntary: bool = False
    control_kind: AtsControlKind = AtsControlKind.TEXT

    def __post_init__(self) -> None:
        object.__setattr__(self, "control_kind", AtsControlKind(self.control_kind))


@dataclass(frozen=True)
class AtsDocumentSlot:
    field_id: str
    kind: AtsFieldKind
    required: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", AtsFieldKind(self.kind))


@dataclass(frozen=True)
class BrowserReviewSnapshot:
    page: BrowserPage
    answers: dict[str, str]
    attachment_hashes: dict[str, str]
    attachment_names: dict[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", BrowserPage(self.page))
        object.__setattr__(
            self,
            "attachment_names",
            dict(self.attachment_names or {}),
        )


@dataclass(frozen=True)
class _TrustedFillSession:
    application_id: str
    filled_url: str
    artifact_hashes: dict[str, str]
    attachment_names: dict[str, str]
    field_kinds: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "filled_url": self.filled_url,
            "artifact_hashes": dict(self.artifact_hashes),
            "attachment_names": dict(self.attachment_names),
            "field_kinds": dict(self.field_kinds),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_TrustedFillSession":
        return cls(
            application_id=str(value["application_id"]),
            filled_url=str(value["filled_url"]),
            artifact_hashes=_string_mapping(value["artifact_hashes"]),
            attachment_names=_string_mapping(value["attachment_names"]),
            field_kinds=_string_mapping(value["field_kinds"]),
        )


@dataclass(frozen=True)
class WorkdayInspection:
    fields: tuple[AtsField, ...]
    document_slots: tuple[AtsDocumentSlot, ...]
    account_required: bool
    page: BrowserPage


@dataclass(frozen=True)
class ManualFieldIntervention:
    application_id: str
    field_id: str
    prompt: str
    reason: str = "unsupported mandatory ATS control"


@dataclass(frozen=True)
class PlannedUpload:
    field_id: str
    path: Path
    expected_hash: str


@dataclass(frozen=True)
class DedicatedCareerAccount:
    email: str
    keychain_service: str = "job-agent.workday"

    def __post_init__(self) -> None:
        normalized = self.email.strip().casefold()
        if (
            normalized.count("@") != 1
            or not normalized.endswith("@gmail.com")
            or any(character.isspace() for character in normalized)
        ):
            raise ValueError("Dedicated career Gmail must be configured explicitly")
        object.__setattr__(self, "email", normalized)
        if not self.keychain_service.strip():
            raise ValueError("A Keychain service name is required")


class WorkdayBrowser(Protocol):
    """Deliberately excludes a submit operation from the fill capability."""

    def inspect_application(
        self, application_id: str, profile: str
    ) -> WorkdayInspection: ...
    def ensure_account(self, email: str, password: str) -> None: ...
    def fill_field(self, field_id: str, value: str) -> None: ...
    def upload_document(self, field_id: str, path: Path) -> None: ...
    def advance_to_review(self) -> None: ...
    def capture_review(self) -> BrowserReviewSnapshot: ...
    def current_url(self) -> str: ...
    def detect_intervention(self, guarded_action: str) -> InterventionKind | None: ...
    def click_submission(self) -> None: ...
    def capture_submission_confirmation(self) -> WorkdayConfirmationCapture: ...


class CredentialStore(Protocol):
    def get(self, service: str, account: str) -> str | None: ...
    def store(self, service: str, account: str, password: str) -> None: ...


class AnswerRequestSink(Protocol):
    def request(
        self, question: TelegramAnswerQuestion | ManualFieldIntervention
    ) -> None: ...


class AtsFillReceiptStore(Protocol):
    def load(
        self, application_id: str, intent_id: str, artifact_version: str
    ) -> FilledApplication | None: ...
    def save(
        self,
        application_id: str,
        intent_id: str,
        result: FilledApplication,
    ) -> None: ...
    def load_submission_session(
        self, application_id: str, artifact_version: str
    ) -> dict[str, Any] | None: ...


class AtsAnswerRequired(RuntimeError):
    """Safe signal that local human answers must resolve before browser mutation."""


class WorkdayAtsAdapter:
    """Complete one deterministic Workday fill journey and stop at review."""

    def __init__(
        self,
        *,
        browser: WorkdayBrowser,
        answer_service: LocalAtsAnswerService,
        answer_requests: AnswerRequestSink,
        keychain: CredentialStore,
        account: DedicatedCareerAccount,
        receipts: AtsFillReceiptStore,
        password_factory: Callable[[], str] | None = None,
    ) -> None:
        self._browser = browser
        self._answer_service = answer_service
        self._answer_requests = answer_requests
        self._keychain = keychain
        self._account = account
        self._receipts = receipts
        self._password_factory = password_factory or generate_ats_password
        self._review_sessions: dict[str, _TrustedFillSession] = {}

    def fill(
        self, application_id: str, intent_id: str, artifacts: PreparedArtifacts
    ) -> FilledApplication:
        expected_artifacts = _verified_artifacts(artifacts)
        completed = self._receipts.load(
            application_id, intent_id, artifacts.version
        )
        if completed is not None:
            load_session = getattr(self._receipts, "load_session", None)
            if callable(load_session):
                saved_session = load_session(
                    application_id, intent_id, artifacts.version
                )
                if saved_session is not None:
                    session = _TrustedFillSession.from_dict(saved_session)
                    if session.application_id != application_id:
                        raise RuntimeError("Local ATS fill scope is unavailable")
                    self._review_sessions[application_id] = session
            return completed

        inspection = self._browser.inspect_application(
            application_id, DEDICATED_BROWSER_PROFILE
        )
        answers, one_use_fields, pending = self._resolve_fields(
            application_id, inspection.fields
        )
        if pending:
            for question in pending:
                self._answer_requests.request(question)
            manual = tuple(
                question
                for question in pending
                if isinstance(question, ManualFieldIntervention)
            )
            if manual:
                prompts = "; ".join(question.prompt for question in manual)
                raise BrowserInterventionRequired(
                    kind=InterventionKind.UNSUPPORTED_CONTROL,
                    explanation=(
                        "A mandatory ATS answer is required for an unsupported "
                        f"control: {prompts}"
                    ),
                    browser_ready=True,
                )
            raise AtsAnswerRequired("A mandatory ATS answer is required")

        uploads = _plan_uploads(
            inspection.document_slots, artifacts, expected_artifacts
        )
        if inspection.page != BrowserPage.REVIEW:
            self.assert_pre_action_safe("fill")
            if inspection.account_required:
                self.assert_pre_action_safe("fill")
                self._ensure_account()
            for field_id, value in answers.items():
                self.assert_pre_action_safe("fill")
                self._browser.fill_field(field_id, value)
            for upload in uploads:
                self.assert_pre_action_safe("fill")
                self._browser.upload_document(upload.field_id, upload.path)
            self.assert_pre_action_safe("fill")
            self._browser.advance_to_review()
            self.assert_pre_action_safe("fill")
        review = self._browser.capture_review()
        _validate_review(review, answers, uploads)

        current_url = getattr(self._browser, "current_url", None)
        filled_url = str(current_url()) if callable(current_url) else ""
        slot_kinds = {
            slot.field_id: slot.kind.value for slot in inspection.document_slots
        }
        uploaded_artifacts = {
            slot_kinds[upload.field_id]: upload.expected_hash for upload in uploads
        }
        session = _TrustedFillSession(
            application_id=application_id,
            filled_url=filled_url,
            artifact_hashes=uploaded_artifacts,
            attachment_names={
                slot_kinds[upload.field_id]: upload.path.name for upload in uploads
            },
            field_kinds=slot_kinds,
        )
        self._review_sessions[application_id] = session

        for field in one_use_fields:
            self._answer_service.mark_used(field)
        result = FilledApplication(
            answers=answers,
            artifact_version=artifacts.version,
            answer_disclosures=_answer_disclosures(inspection.fields, answers),
            review_evidence=ReviewEvidence(
                page=ReviewEvidencePage.REVIEW,
                form_snapshot=dict(review.answers),
                attachment_hashes=uploaded_artifacts,
            ),
        )
        save_with_session = getattr(self._receipts, "save_with_session", None)
        if callable(save_with_session):
            save_with_session(
                application_id,
                intent_id,
                result,
                session=session.to_dict(),
            )
        else:
            self._receipts.save(application_id, intent_id, result)
        return result

    def restore_submission(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> None:
        """Restore the durable trusted fill scope before fresh review validation."""

        session = self._review_sessions.get(application_id)
        if session is None:
            saved = self._receipts.load_submission_session(
                application_id, manifest.artifact_version
            )
            if saved is None:
                raise RuntimeError("Workday trusted fill session is unavailable")
            session = _TrustedFillSession.from_dict(saved)
            self._review_sessions[application_id] = session
        if (
            session.application_id != application_id
            or dict(session.artifact_hashes) != dict(manifest.artifact_hashes)
        ):
            self._review_sessions.pop(application_id, None)
            raise RuntimeError("Workday trusted fill session has the wrong scope")

    def capture_submission_review(
        self, application_id: str
    ) -> ScopedWorkdayReview:
        """Recapture the same filled browser session immediately before submit."""

        session = self._review_sessions.get(application_id)
        if session is None:
            raise RuntimeError("Workday trusted fill session is unavailable")
        current_url = getattr(self._browser, "current_url", None)
        if not callable(current_url):
            raise RuntimeError("Workday browser cannot prove its current page")
        review = self._browser.capture_review()
        if review.page != BrowserPage.REVIEW:
            raise RuntimeError("Workday is no longer on the review page")
        current_names = {
            session.field_kinds[field_id]: name
            for field_id, name in review.attachment_names.items()
            if field_id in session.field_kinds
        }
        current_hashes = {
            session.field_kinds[field_id]: value
            for field_id, value in review.attachment_hashes.items()
            if field_id in session.field_kinds
        }
        return ScopedWorkdayReview(
            application_id=application_id,
            filled_url=session.filled_url,
            current_url=str(current_url()),
            answers=dict(review.answers),
            attachment_hashes=current_hashes,
            filled_attachment_names=dict(session.attachment_names),
            current_attachment_names=current_names,
        )

    def submission_review_is_visible(self) -> bool:
        try:
            return self._browser.capture_review().page == BrowserPage.REVIEW
        except Exception:
            return False

    def click_submission(self) -> None:
        click = getattr(self._browser, "click_submission", None)
        if not callable(click):
            raise RuntimeError("Workday browser has no submit capability")
        click()

    def assert_pre_action_safe(self, guarded_action: str) -> None:
        probe = getattr(self._browser, "detect_intervention", None)
        if not callable(probe):
            raise RuntimeError("Workday pre-action safety probe is unavailable")
        kind = probe(guarded_action)
        if kind is None:
            return
        kind = InterventionKind(kind)
        explanations = {
            InterventionKind.CAPTCHA: "Solve the CAPTCHA in the dedicated browser",
            InterventionKind.NON_EMAIL_MFA: (
                "Complete non-email MFA in the dedicated browser"
            ),
            InterventionKind.UNUSUAL_CONSENT: (
                "Review the unusual consent in the dedicated browser"
            ),
            InterventionKind.SITE_RESTRICTION: (
                "Resolve the site restriction in the dedicated browser"
            ),
        }
        raise BrowserInterventionRequired(
            kind=kind,
            explanation=explanations.get(
                kind, "Resolve the guarded Workday action in the dedicated browser"
            ),
            browser_ready=True,
        )

    def capture_submission_confirmation(self) -> WorkdayConfirmationCapture:
        capture = getattr(self._browser, "capture_submission_confirmation", None)
        if not callable(capture):
            raise RuntimeError("Workday confirmation capture is unavailable")
        result = capture()
        if not isinstance(result, WorkdayConfirmationCapture):
            raise RuntimeError("Workday confirmation capture is invalid")
        return result

    def intervention_is_resolved(
        self, application_id: str, intervention: InterventionRecord
    ) -> bool:
        probe = getattr(self._browser, "intervention_is_resolved", None)
        if callable(probe):
            try:
                return probe(application_id, intervention.kind.value) is True
            except Exception:
                return False
        if intervention.kind != InterventionKind.UNSUPPORTED_CONTROL:
            return False
        try:
            inspection = self._browser.inspect_application(
                application_id, DEDICATED_BROWSER_PROFILE
            )
        except Exception:
            return False
        return not any(
            field.mandatory and field.control_kind == AtsControlKind.UNSUPPORTED
            for field in inspection.fields
        )

    def _resolve_fields(
        self, application_id: str, fields: tuple[AtsField, ...]
    ) -> tuple[
        dict[str, str],
        tuple[ApplicationFieldReference, ...],
        tuple[TelegramAnswerQuestion | ManualFieldIntervention, ...],
    ]:
        answers: dict[str, str] = {}
        one_use_fields: list[ApplicationFieldReference] = []
        pending: list[TelegramAnswerQuestion | ManualFieldIntervention] = []
        for field in fields:
            if field.control_kind == AtsControlKind.UNSUPPORTED:
                if field.mandatory:
                    pending.append(
                        ManualFieldIntervention(
                            application_id,
                            field.field_id,
                            str(
                                self._answer_service.redact_for_public_boundaries(
                                    field.prompt
                                )
                            ),
                        )
                    )
                continue
            reference = ApplicationFieldReference(application_id, field.field_id)
            question = AtsQuestion(
                field=reference,
                prompt=field.prompt,
                mandatory=field.mandatory,
                meaning=field.meaning,
                standardized_voluntary=field.standardized_voluntary,
            )
            try:
                resolved = self._answer_service.resolve(question)
            except AnswerServiceError:
                if not field.mandatory:
                    continue
                raise
            if isinstance(resolved, TelegramAnswerQuestion):
                pending.append(resolved)
                continue
            assert isinstance(resolved, LocalFormAnswer)
            answers[field.field_id] = resolved.value
            if resolved.source == "one_use":
                one_use_fields.append(reference)
        return answers, tuple(one_use_fields), tuple(pending)

    def _ensure_account(self) -> None:
        password = self._keychain.get(
            self._account.keychain_service, self._account.email
        )
        if password is None:
            password = self._password_factory()
            if not password:
                raise RuntimeError("A generated ATS credential is unavailable")
            self._keychain.store(
                self._account.keychain_service, self._account.email, password
            )
        self._browser.ensure_account(self._account.email, password)


def _verified_artifacts(artifacts: PreparedArtifacts) -> dict[str, str]:
    values = {
        "cv": (Path(artifacts.cv_path), artifacts.cv_hash),
        "cover_letter": (
            Path(artifacts.cover_letter_path),
            artifacts.cover_letter_hash,
        ),
    }
    for path, expected_hash in values.values():
        if not path.is_file() or _file_hash(path) != expected_hash:
            raise ValueError("Prepared application artifact hash mismatch")
    return {kind: expected_hash for kind, (_, expected_hash) in values.items()}


def _plan_uploads(
    slots: tuple[AtsDocumentSlot, ...],
    artifacts: PreparedArtifacts,
    expected_hashes: Mapping[str, str],
) -> tuple[PlannedUpload, ...]:
    paths = {
        AtsFieldKind.CV: Path(artifacts.cv_path),
        AtsFieldKind.COVER_LETTER: Path(artifacts.cover_letter_path),
    }
    hashes = {
        AtsFieldKind.CV: expected_hashes["cv"],
        AtsFieldKind.COVER_LETTER: expected_hashes["cover_letter"],
    }
    planned = tuple(
        PlannedUpload(slot.field_id, paths[slot.kind], hashes[slot.kind])
        for slot in slots
    )
    if not any(slot.kind == AtsFieldKind.CV for slot in slots):
        raise ValueError("Supported ATS journey has no CV upload slot")
    return planned


def _validate_review(
    review: BrowserReviewSnapshot,
    answers: Mapping[str, str],
    uploads: tuple[PlannedUpload, ...],
) -> None:
    if review.page != BrowserPage.REVIEW:
        raise RuntimeError("ATS did not stop on the review page")
    if any(review.answers.get(field_id) != value for field_id, value in answers.items()):
        raise RuntimeError("ATS review does not match the filled answers")
    expected_uploads = {
        upload.field_id: upload.expected_hash for upload in uploads
    }
    if review.attachment_hashes != expected_uploads:
        raise RuntimeError("ATS review does not match the prepared attachments")


_PUBLIC_SUMMARY_MEANINGS = {
    QuestionMeaning.WORK_AUTHORIZATION,
    QuestionMeaning.SPONSORSHIP,
    QuestionMeaning.START_DATE,
    QuestionMeaning.REFERENCES,
    QuestionMeaning.SALARY_EXPECTATION,
}


def _answer_disclosures(
    fields: tuple[AtsField, ...], answers: Mapping[str, str]
) -> tuple[AnswerDisclosure, ...]:
    meanings = {field.field_id: field.meaning for field in fields}
    return tuple(
        AnswerDisclosure(
            field_id,
            (
                AnswerVisibility.PUBLIC_SUMMARY
                if meanings.get(field_id) in _PUBLIC_SUMMARY_MEANINGS
                else AnswerVisibility.LOCAL_ONLY
            ),
        )
        for field_id in sorted(answers)
    )


def _file_hash(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("Trusted Workday fill scope is malformed")
    return {str(key): str(item) for key, item in value.items()}


__all__ = [
    "AtsAnswerRequired",
    "AtsControlKind",
    "AtsDocumentSlot",
    "AtsField",
    "AtsFieldKind",
    "BrowserPage",
    "BrowserReviewSnapshot",
    "DedicatedCareerAccount",
    "ManualFieldIntervention",
    "PlannedUpload",
    "WorkdayInspection",
    "WorkdayAtsAdapter",
]
