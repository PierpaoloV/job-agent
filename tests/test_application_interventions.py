from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import (  # noqa: E402
    FilledApplication,
    PreparedArtifacts,
    SubmissionEvidence,
    SubmissionOutcome,
    SubmissionVerificationKind,
    WorkflowAction,
)
from application_interventions import (  # noqa: E402
    BrowserInterventionRequired,
    InterventionContinuation,
    InterventionContinuationKind,
    InterventionKind,
    InterventionRecord,
    SubmissionInspection,
    SubmissionInspectionStatus,
)
from application_storage import (  # noqa: E402
    JsonApplicationStore,
    MarkdownApplicationReportWriter,
)
from application_telegram import TelegramCommandHandler  # noqa: E402
from application_workflow import (  # noqa: E402
    ApplicationWorkflowCoordinator,
    OfficialVacancy,
)


class Clock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


class AdjustableClock:
    def __init__(self):
        self.current = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


class Tailoring:
    def prepare(self, application_id, intent_id, opportunity, official_vacancy):
        return PreparedArtifacts(
            version="artifacts-v1",
            cv_path="applications/app-1/cv.pdf",
            cover_letter_path="applications/app-1/cover-letter.pdf",
            cv_hash="sha256:cv",
            cover_letter_hash="sha256:cover-letter",
        )

    def verify_artifacts(self, artifacts):
        return True

    def reload_master_cv(self):
        return "evidence-v1"


class Vacancies:
    vacancy = OfficialVacancy(
        version="vacancy-v1",
        fingerprint="sha256:vacancy",
        freshness="2026-07-16T10:30:00+00:00",
        description="Research trustworthy vision models.",
    )

    def retrieve(self, opportunity):
        return self.vacancy

    def revalidate(self, opportunity, previous):
        return self.vacancy


class InterventionAts:
    def __init__(self, kind):
        self.kind = kind
        self.resolved = False
        self.fill_attempts = 0
        self.guarded_mutations = 0

    def fill(self, application_id, intent_id, artifacts):
        self.fill_attempts += 1
        if not self.resolved:
            raise BrowserInterventionRequired(
                kind=self.kind,
                explanation=f"Human action required for {self.kind.value}",
                browser_ready=True,
            )
        self.guarded_mutations += 1
        return FilledApplication(answers={}, artifact_version=artifacts.version)

    def intervention_is_resolved(self, application_id, intervention):
        return self.resolved

    def validate_submit(self, application_id, manifest):
        return True

    def submit(self, application_id, manifest):
        raise AssertionError("submit is outside this scenario")


class UncertainThenVerifiedAts:
    def __init__(self):
        self.submit_clicks = 0
        self.inspections = 0

    def fill(self, application_id, intent_id, artifacts):
        return FilledApplication(answers={}, artifact_version=artifacts.version)

    def validate_submit(self, application_id, manifest):
        return True

    def submit(self, application_id, manifest):
        self.submit_clicks += 1
        if self.submit_clicks == 1:
            raise TimeoutError("timeout after the one guarded click")
        return SubmissionOutcome(
            status="verified",
            evidence=SubmissionEvidence(
                captured_at="2026-07-16T10:30:00+00:00",
                verified_by=(SubmissionVerificationKind.CONFIRMATION_ID,),
                confirmation_id="confirmation-after-human-resolution",
            ),
        )

    def inspect_submission(self, application_id, manifest):
        self.inspections += 1
        return SubmissionInspection(
            status=SubmissionInspectionStatus.NO_POSITIVE_EVIDENCE,
            checked_at="2026-07-16T10:31:00+00:00",
            sources_checked=("ats", "career_mailbox"),
        )


class Transport:
    def __init__(self):
        self.statuses = []
        self.interventions = []
        self.uncertain = []

    def send_pre_submit(self, summary, command):
        pass

    def send_status(self, message):
        self.statuses.append(message)

    def send_intervention(self, intervention, command):
        self.interventions.append((intervention, command))

    def send_uncertain_submission(self, uncertain, command):
        self.uncertain.append((uncertain, command))


def build(tmp_path, ats, *, clock=None):
    store = JsonApplicationStore(tmp_path / "state")
    return ApplicationWorkflowCoordinator(
        store=store,
        tailoring=Tailoring(),
        ats=ats,
        report_writer=MarkdownApplicationReportWriter(tmp_path / "reports"),
        official_vacancies=Vacancies(),
        clock=clock or Clock(),
        token_factory=lambda: f"token-{len(store.list_authorizations()) + 1}",
    )


def propose_and_prepare(coordinator):
    coordinator.propose(
        application_id="app-1",
        opportunity={
            "company": "Acme",
            "title": "Research Scientist",
            "location": "Zurich",
        },
        version="opportunity-v1",
    )
    prepare = coordinator.issue_authorization(
        "app-1", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(prepare).status == "completed"


def fill_ready(coordinator):
    fill = coordinator.issue_authorization(
        "app-1", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(fill).status == "completed"


def test_legacy_intervention_continuation_migrates_to_typed_variant():
    intervention = InterventionRecord.from_dict(
        {
            "intervention_id": "intervention-legacy",
            "kind": "non_email_mfa",
            "action": "Invia",
            "explanation": "Approve MFA",
            "detected_at": "2026-07-16T10:30:00+00:00",
            "browser_ready": True,
            "resume_token": "resume-token",
            "actor": "Synthetic Owner",
            "pending_authorization_token": None,
            "operation_intent_id": None,
            "submission_intent_id": "submit:token",
        }
    )

    assert intervention.continuation == InterventionContinuation(
        kind=InterventionContinuationKind.SUBMISSION_INTENT,
        reference="submit:token",
    )


@pytest.mark.parametrize(
    "kind",
    (
        InterventionKind.CAPTCHA,
        InterventionKind.NON_EMAIL_MFA,
        InterventionKind.UNUSUAL_CONSENT,
        InterventionKind.SITE_RESTRICTION,
        InterventionKind.UNSUPPORTED_CONTROL,
    ),
)
def test_browser_intervention_is_durable_and_only_riprendi_can_continue(
    tmp_path, kind
):
    ats = InterventionAts(kind)
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    transport = Transport()
    handler = TelegramCommandHandler(coordinator, transport=transport)
    fill = handler.create_callback("app-1", "Compila", actor="Synthetic Owner")

    result = handler.handle_callback(fill)

    assert result.status == "intervention_required"
    paused = coordinator.get("app-1")
    assert paused.intervention.kind == kind
    assert paused.intervention.action == WorkflowAction.FILL
    assert paused.intervention.browser_ready is True
    assert paused.next_action is None
    assert ats.guarded_mutations == 0
    assert transport.interventions[0][0].explanation.endswith(kind.value)
    report = (tmp_path / "reports" / "app-1.md").read_text(encoding="utf-8")
    assert f"Intervention: {kind.value}" in report
    assert "Browser ready: True" in report
    resume = transport.interventions[0][1]
    assert resume.scope.action == WorkflowAction.RESUME
    assert handler.encode_callback(resume).startswith("app:")

    restarted = build(tmp_path, ats)
    with pytest.raises(ValueError, match="Riprendi"):
        restarted.resume_pending("app-1")
    assert restarted.handle(fill).status == "replayed"
    assert ats.guarded_mutations == 0

    # Riprendi is harmless until the browser probe confirms the human solved it.
    assert restarted.handle(resume).status == "intervention_required"
    assert ats.fill_attempts == 1
    ats.resolved = True
    resumed = restarted.handle(resume)

    assert resumed.status == "completed"
    assert restarted.get("app-1").intervention is None
    assert restarted.get("app-1").lifecycle_state == "pronta da inviare"
    assert ats.fill_attempts == 2
    assert ats.guarded_mutations == 1
    assert restarted.handle(resume).status == "replayed"


def test_expired_riprendi_is_reissued_once_while_intervention_stays_pending(
    tmp_path,
):
    clock = AdjustableClock()
    ats = InterventionAts(InterventionKind.CAPTCHA)
    coordinator = build(tmp_path, ats, clock=clock)
    propose_and_prepare(coordinator)
    transport = Transport()
    handler = TelegramCommandHandler(coordinator, transport=transport)
    fill = handler.create_callback("app-1", "Compila", actor="Synthetic Owner")
    assert handler.handle_callback(fill).status == "intervention_required"
    original = transport.interventions[-1][1]
    intervention_id = coordinator.get("app-1").intervention.intervention_id

    clock.advance(minutes=31)
    result = handler.handle_callback(original)

    assert result.status == "intervention_required"
    pending = coordinator.get("app-1")
    replacement = transport.interventions[-1][1]
    assert pending.intervention.intervention_id == intervention_id
    assert replacement.token != original.token
    assert pending.intervention.resume_token == replacement.token
    assert ats.guarded_mutations == 0

    ats.resolved = True
    assert handler.handle_callback(replacement).status == "completed"
    assert handler.handle_callback(replacement).status == "replayed"
    assert ats.guarded_mutations == 1


def test_uncertain_submit_is_inspected_then_requires_resolution_and_new_invia(
    tmp_path,
):
    ats = UncertainThenVerifiedAts()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    transport = Transport()
    handler = TelegramCommandHandler(coordinator, transport=transport)
    submit = handler.create_callback("app-1", "Invia", actor="Synthetic Owner")

    result = handler.handle_callback(submit)

    assert result.status == "uncertain"
    assert ats.submit_clicks == 1
    assert ats.inspections == 1
    uncertain = coordinator.get("app-1").uncertain_submission
    assert uncertain.inspection.sources_checked == ("ats", "career_mailbox")
    assert uncertain.inspection.status == "no_positive_evidence"
    report = (tmp_path / "reports" / "app-1.md").read_text(encoding="utf-8")
    assert "Sources checked: ats, career_mailbox" in report
    assert "Sources unavailable: none" in report
    assert "Automatic retry: forbidden" in report
    resolution = transport.uncertain[0][1]
    assert resolution.scope.action == WorkflowAction.RESOLVE_NOT_SUBMITTED

    restarted = build(tmp_path, ats)
    assert restarted.get("app-1").operational_status == (
        "submission_outcome_uncertain"
    )
    assert restarted.handle(submit).status == "replayed"
    assert ats.submit_clicks == 1
    with pytest.raises(ValueError, match="Next valid action is None"):
        restarted.issue_authorization(
            "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
        )

    resolved = restarted.handle(resolution)
    assert resolved.status == "resolved"
    assert restarted.get("app-1").uncertain_submission is None
    assert restarted.get("app-1").outcome is None
    assert restarted.get("app-1").submission_intents == ()
    assert restarted.get("app-1").next_action == WorkflowAction.SUBMIT
    assert ats.submit_clicks == 1

    new_submit = restarted.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    assert new_submit.token != submit.token
    assert restarted.handle(new_submit).status == "completed"
    assert ats.submit_clicks == 2
    assert restarted.get("app-1").lifecycle_state == "inviata"


def test_positive_evidence_found_during_inspection_finishes_without_retry(tmp_path):
    class EvidenceFound(UncertainThenVerifiedAts):
        def inspect_submission(self, application_id, manifest):
            self.inspections += 1
            return SubmissionInspection(
                status=SubmissionInspectionStatus.VERIFIED,
                checked_at="2026-07-16T10:31:00+00:00",
                sources_checked=("ats", "career_mailbox"),
                evidence=SubmissionEvidence(
                    captured_at="2026-07-16T10:31:00+00:00",
                    verified_by=(SubmissionVerificationKind.EMAIL_RECEIPT,),
                    email_receipt_id="gmail-42",
                    email_receipt_received_at="2026-07-16T10:31:00+00:00",
                ),
            )

    ats = EvidenceFound()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    result = coordinator.handle(submit)

    assert result.status == "completed"
    assert ats.submit_clicks == 1
    assert ats.inspections == 1
    assert coordinator.get("app-1").lifecycle_state == "inviata"
    assert coordinator.get("app-1").outcome.evidence.email_receipt_id == "gmail-42"
    assert coordinator.get("app-1").uncertain_submission is None


@pytest.mark.parametrize("blocked_during", ("validation", "submit"))
def test_submit_intervention_stays_pre_action_and_reuses_the_durable_continuation(
    tmp_path, blocked_during
):
    class SubmitIntervention:
        def __init__(self):
            self.resolved = False
            self.submit_clicks = 0

        def fill(self, application_id, intent_id, artifacts):
            return FilledApplication(answers={}, artifact_version=artifacts.version)

        def validate_submit(self, application_id, manifest):
            if blocked_during == "validation" and not self.resolved:
                raise BrowserInterventionRequired(
                    kind=InterventionKind.CAPTCHA,
                    explanation="Solve CAPTCHA in the dedicated browser",
                    browser_ready=True,
                )
            return True

        def submit(self, application_id, manifest):
            if blocked_during == "submit" and not self.resolved:
                raise BrowserInterventionRequired(
                    kind=InterventionKind.NON_EMAIL_MFA,
                    explanation="Approve MFA on the registered device",
                    browser_ready=True,
                )
            self.submit_clicks += 1
            return SubmissionOutcome(
                status="verified",
                evidence=SubmissionEvidence(
                    captured_at="2026-07-16T10:30:00+00:00",
                    verified_by=(SubmissionVerificationKind.CONFIRMATION_ID,),
                    confirmation_id="confirmed-once",
                ),
            )

        def intervention_is_resolved(self, application_id, intervention):
            return self.resolved

    ats = SubmitIntervention()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    transport = Transport()
    handler = TelegramCommandHandler(coordinator, transport=transport)
    submit = handler.create_callback("app-1", "Invia", actor="Synthetic Owner")

    blocked = handler.handle_callback(submit)

    assert blocked.status == "intervention_required"
    assert ats.submit_clicks == 0
    paused = coordinator.get("app-1")
    expected_intents = 0 if blocked_during == "validation" else 1
    assert len(paused.submission_intents) == expected_intents
    resume = transport.interventions[0][1]

    restarted = build(tmp_path, ats)
    ats.resolved = True
    completed = restarted.handle(resume)

    assert completed.status == "completed"
    assert ats.submit_clicks == 1
    assert restarted.get("app-1").lifecycle_state == "inviata"
    assert restarted.handle(resume).status == "replayed"
    assert ats.submit_clicks == 1


def test_incomplete_evidence_inspection_keeps_retry_resolution_locked(tmp_path):
    class IncompleteInspection(UncertainThenVerifiedAts):
        def inspect_submission(self, application_id, manifest):
            self.inspections += 1
            return SubmissionInspection(
                status=SubmissionInspectionStatus.INCOMPLETE,
                checked_at="2026-07-16T10:31:00+00:00",
                sources_checked=("ats", "career_mailbox"),
            )

    ats = IncompleteInspection()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    transport = Transport()
    handler = TelegramCommandHandler(coordinator, transport=transport)
    submit = handler.create_callback("app-1", "Invia", actor="Synthetic Owner")

    assert handler.handle_callback(submit).status == "uncertain"

    uncertain, resolution = transport.uncertain[0]
    assert uncertain.inspection.status == "incomplete"
    assert uncertain.resolution_token is None
    assert resolution is None
    assert coordinator.get("app-1").next_action is None
    assert ats.submit_clicks == 1


def test_restart_after_crash_after_click_inspects_before_any_resolution(tmp_path):
    class CrashAfterClick(UncertainThenVerifiedAts):
        def submit(self, application_id, manifest):
            self.submit_clicks += 1
            raise KeyboardInterrupt("process stopped after the guarded click")

    ats = CrashAfterClick()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    with pytest.raises(KeyboardInterrupt):
        coordinator.handle(submit)

    restarted = build(tmp_path, ats)
    recovered = restarted.resume_pending("app-1")

    assert recovered.status == "uncertain"
    assert ats.submit_clicks == 1
    assert ats.inspections == 1
    uncertain = restarted.get("app-1").uncertain_submission
    assert uncertain.inspection.status == "no_positive_evidence"
    assert uncertain.inspection.sources_checked == ("ats", "career_mailbox")
    assert uncertain.resolution_token is not None


def test_crash_after_riprendi_claim_preserves_submit_validation_continuation(
    tmp_path, monkeypatch
):
    class ValidationIntervention(UncertainThenVerifiedAts):
        def __init__(self):
            super().__init__()
            self.resolved = False

        def validate_submit(self, application_id, manifest):
            if not self.resolved:
                raise BrowserInterventionRequired(
                    kind=InterventionKind.CAPTCHA,
                    explanation="Solve CAPTCHA",
                    browser_ready=True,
                )
            return True

        def intervention_is_resolved(self, application_id, intervention):
            return self.resolved

        def submit(self, application_id, manifest):
            self.submit_clicks += 1
            return SubmissionOutcome(
                status="verified",
                evidence=SubmissionEvidence(
                    captured_at="2026-07-16T10:30:00+00:00",
                    verified_by=(SubmissionVerificationKind.CONFIRMATION_ID,),
                    confirmation_id="confirmed-after-resume",
                ),
            )

    ats = ValidationIntervention()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    assert coordinator.handle(submit).status == "intervention_required"
    resume = coordinator.command_for_token(
        coordinator.get("app-1").intervention.resume_token
    )
    ats.resolved = True

    def crash_after_claim(*args):
        raise KeyboardInterrupt("crash after Riprendi claim")

    monkeypatch.setattr(coordinator, "_continue_intervention", crash_after_claim)
    with pytest.raises(KeyboardInterrupt):
        coordinator.handle(resume)

    paused = coordinator.get("app-1")
    assert paused.intervention is not None
    assert paused.submission_intents == ()
    restarted = build(tmp_path, ats)
    recovered = restarted.resume_pending("app-1")

    assert recovered.status == "completed"
    assert ats.submit_clicks == 1
    assert restarted.get("app-1").lifecycle_state == "inviata"


def test_restart_after_crash_during_resumed_submit_inspects_without_clicking_again(
    tmp_path,
):
    class CrashAfterResumedClick(UncertainThenVerifiedAts):
        def __init__(self):
            super().__init__()
            self.resolved = False

        def submit(self, application_id, manifest):
            if not self.resolved:
                raise BrowserInterventionRequired(
                    kind=InterventionKind.NON_EMAIL_MFA,
                    explanation="Approve MFA on the registered device",
                    browser_ready=True,
                )
            self.submit_clicks += 1
            raise KeyboardInterrupt("process stopped after the resumed submit click")

        def intervention_is_resolved(self, application_id, intervention):
            return self.resolved

    ats = CrashAfterResumedClick()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    assert coordinator.handle(submit).status == "intervention_required"
    resume = coordinator.command_for_token(
        coordinator.get("app-1").intervention.resume_token
    )
    ats.resolved = True

    with pytest.raises(KeyboardInterrupt):
        coordinator.handle(resume)

    restarted = build(tmp_path, ats)
    recovered = restarted.resume_pending("app-1")

    assert recovered.status == "uncertain"
    assert ats.submit_clicks == 1
    assert ats.inspections == 1
    assert restarted.get("app-1").intervention is None


def test_expired_uncertain_resolution_rotates_without_unlocking_submit(tmp_path):
    clock = AdjustableClock()
    ats = UncertainThenVerifiedAts()
    coordinator = build(tmp_path, ats, clock=clock)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    assert coordinator.handle(submit).status == "uncertain"
    original = coordinator.command_for_token(
        coordinator.get("app-1").uncertain_submission.resolution_token
    )

    clock.advance(minutes=31)
    result = coordinator.handle(original)
    pending = coordinator.get("app-1")
    replacement = coordinator.command_for_token(
        pending.uncertain_submission.resolution_token
    )

    assert result.status == "uncertain"
    assert replacement.token != original.token
    assert pending.submission_intents
    assert pending.next_action is None
    assert ats.submit_clicks == 1
    assert coordinator.handle(original).status in {"stale", "expired"}


def test_failed_inspection_never_claims_sources_or_offers_resolution(tmp_path):
    class InspectionFailure(UncertainThenVerifiedAts):
        def inspect_submission(self, application_id, manifest):
            raise RuntimeError("mailbox unavailable")

    ats = InspectionFailure()
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill_ready(coordinator)
    submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    assert coordinator.handle(submit).status == "uncertain"
    uncertain = coordinator.get("app-1").uncertain_submission

    assert uncertain.inspection.sources_checked == ()
    assert set(uncertain.inspection.sources_unavailable) == {
        "ats",
        "career_mailbox",
    }
    assert uncertain.resolution_token is None


def test_concurrent_riprendi_deliveries_claim_one_continuation(tmp_path):
    ats = InterventionAts(InterventionKind.CAPTCHA)
    coordinator = build(tmp_path, ats)
    propose_and_prepare(coordinator)
    fill = coordinator.issue_authorization(
        "app-1", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(fill).status == "intervention_required"
    paused = coordinator.get("app-1")
    resume = coordinator.command_for_token(paused.intervention.resume_token)
    ats.resolved = True
    first = build(tmp_path, ats)
    second = build(tmp_path, ats)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(first.handle, resume),
            executor.submit(second.handle, resume),
        ]
        statuses = sorted(result.result().status for result in results)

    assert statuses == ["completed", "replayed"]
    assert ats.guarded_mutations == 1
