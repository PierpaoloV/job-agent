from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import (  # noqa: E402
    ApplicationSnapshot,
    CorrespondenceClassification,
    CorrespondenceEvent,
    LifecycleEvent,
    LifecycleState,
    PreparationCapacityException,
    PreparationCapacityExceptionKind,
    SubmissionEvidence,
    SubmissionVerificationKind,
)
from application_workflow import (  # noqa: E402
    ApplicationWorkflowCoordinator,
    FilledApplication,
    JsonApplicationStore,
    MarkdownApplicationReportWriter,
    OfficialVacancy,
    PreparedArtifacts,
    SubmissionOutcome,
    TelegramCommandHandler,
    WorkflowAction,
)
from hosted_tailoring import (  # noqa: E402
    HostedPreparationFailed,
    HostedPreparationResolution,
)


class AdjustableClock:
    def __init__(self):
        self.current = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


class FakeTailoring:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.artifacts_intact = True

    def prepare(self, application_id, intent_id, opportunity, official_vacancy):
        if intent_id in self.results:
            return self.results[intent_id]
        self.calls.append((application_id, intent_id, opportunity, official_vacancy))
        result = PreparedArtifacts(
            version="artifacts-v1",
            cv_path="applications/synthetic-001/cv.pdf",
            cover_letter_path="applications/synthetic-001/cover-letter.pdf",
            cv_hash="sha256:synthetic-cv",
            cover_letter_hash="sha256:synthetic-cover-letter",
        )
        self.results[intent_id] = result
        return result

    def verify_artifacts(self, artifacts):
        return self.artifacts_intact


class FakeAts:
    def __init__(self):
        self.fill_calls = []
        self.fill_results = {}
        self.submit_calls = []

    def fill(self, application_id, intent_id, artifacts):
        if intent_id in self.fill_results:
            return self.fill_results[intent_id]
        self.fill_calls.append((application_id, intent_id, artifacts))
        result = FilledApplication(
            answers={
                "work_authorization": "SYNTHETIC-WORK-AUTHORIZATION",
                "references": "Not provided",
            },
            artifact_version=artifacts.version,
        )
        self.fill_results[intent_id] = result
        return result

    def submit(self, application_id, manifest):
        self.submit_calls.append((application_id, manifest))
        return SubmissionOutcome(
            status="verified",
            evidence=SubmissionEvidence(
                captured_at="2026-07-16T10:30:00+00:00",
                verified_by=(SubmissionVerificationKind.CONFIRMATION_ID,),
                confirmation_id="synthetic-confirmation-001",
            ),
        )

    def validate_submit(self, application_id, manifest):
        return True


class FakeOfficialVacancies:
    def __init__(self):
        self.retrieve_calls = []
        self.revalidate_calls = []
        self.current = OfficialVacancy(
            version="vacancy-v1",
            fingerprint="sha256:synthetic-vacancy",
            freshness="2026-07-16T10:30:00+00:00",
            description="Build trustworthy computer-vision systems.",
        )

    def retrieve(self, opportunity):
        self.retrieve_calls.append(opportunity)
        return self.current

    def revalidate(self, opportunity, previous):
        self.revalidate_calls.append((opportunity, previous))
        return self.current


def build_coordinator(
    tmp_path,
    clock,
    tailoring,
    ats,
    official=None,
    *,
    active_preparation_limit=5,
):
    store = JsonApplicationStore(tmp_path / "state")
    reporter = MarkdownApplicationReportWriter(tmp_path / "reports")
    official = official or FakeOfficialVacancies()
    return ApplicationWorkflowCoordinator(
        store=store,
        tailoring=tailoring,
        ats=ats,
        report_writer=reporter,
        official_vacancies=official,
        clock=clock,
        token_factory=lambda: f"token-{len(store.list_authorizations()) + 1}",
        active_preparation_limit=active_preparation_limit,
    )


def synthetic_opportunity():
    return {
        "stable_id": "acme:research-42",
        "company": "Acme AI",
        "title": "Research Scientist",
        "location": "Zurich",
        "official_url": "https://jobs.example/42",
        "official_description": "Build trustworthy computer-vision systems.",
    }


def propose_and_prepare(coordinator):
    coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    command = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(command).status == "completed"


def fill_prepared_application(coordinator):
    command = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(command).status == "completed"


def test_failed_preparation_requires_fresh_scoped_human_retry(tmp_path):
    class ResolvingTailoring(FakeTailoring):
        safe = True
        resolution_checks = 0

        def prepare(
            self, application_id, intent_id, opportunity, official_vacancy
        ):
            if intent_id == "prepare:token-1":
                raise HostedPreparationFailed("dispatch rejected")
            return super().prepare(
                application_id,
                intent_id,
                opportunity,
                official_vacancy,
            )

        def preparation_resolution(
            self, application_id, intent_id, official_vacancy
        ):
            del application_id, official_vacancy
            self.resolution_checks += 1
            return HostedPreparationResolution(
                intent_id=intent_id,
                phase="failed",
                reason="dispatch rejected",
                retry_safe=self.safe,
            )

    clock = AdjustableClock()
    tailoring = ResolvingTailoring()
    coordinator = build_coordinator(
        tmp_path, clock, tailoring, FakeAts()
    )
    coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    first = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(first).status == "failed"

    with pytest.raises(ValueError, match="scoped retry resolution"):
        coordinator.issue_authorization(
            "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )

    retry = coordinator.issue_preparation_retry_authorization(
        "synthetic-001", actor="Synthetic Owner"
    )
    tailoring.safe = False
    blocked = coordinator.handle(retry)
    assert blocked.status == "reconciliation_required"
    snapshot = coordinator.get("synthetic-001")
    assert [item.intent_id for item in snapshot.operation_intents if item.is_pending] == [
        "prepare:token-1"
    ]

    tailoring.safe = True
    completed = coordinator.handle(retry)
    assert completed.status == "completed"
    snapshot = coordinator.get("synthetic-001")
    assert snapshot.operation_intents[0].cancelled_at is not None
    assert snapshot.operation_intents[1].intent_id == "prepare:token-2"
    assert snapshot.operation_intents[1].completed_at is not None
    assert coordinator.handle(retry).status == "replayed"
    assert [
        call[1] for call in tailoring.calls
    ] == ["prepare:token-2"]


def test_synthetic_application_survives_restart_at_every_gate_and_writes_report(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    proposed = coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )

    assert proposed.lifecycle_state == "proposta"
    assert proposed.next_action == WorkflowAction.PREPARE

    telegram = TelegramCommandHandler(coordinator)
    assert telegram.labels == ("Prepara candidatura", "Compila", "Invia")
    prepare = telegram.create_callback(
        "synthetic-001", "Prepara candidatura", actor="Synthetic Owner"
    )
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    prepared = TelegramCommandHandler(coordinator).handle_callback(prepare)

    assert prepared.status == "completed"
    assert prepared.lifecycle_state == "CV pronto"
    assert prepared.next_action == WorkflowAction.FILL
    assert len(tailoring.calls) == 1
    assert ats.fill_calls == []
    assert ats.submit_calls == []

    fill = TelegramCommandHandler(coordinator).create_callback(
        "synthetic-001", "Compila", actor="Synthetic Owner"
    )
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    filled = TelegramCommandHandler(coordinator).handle_callback(fill)

    assert filled.status == "completed"
    assert filled.lifecycle_state == "pronta da inviare"
    assert filled.next_action == WorkflowAction.SUBMIT
    assert len(ats.fill_calls) == 1
    assert ats.submit_calls == []

    submit = TelegramCommandHandler(coordinator).create_callback(
        "synthetic-001", "Invia", actor="Synthetic Owner"
    )
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    submitted = TelegramCommandHandler(coordinator).handle_callback(submit)

    assert submitted.status == "completed"
    assert submitted.lifecycle_state == "inviata"
    assert submitted.next_action is None
    assert len(ats.submit_calls) == 1

    restarted = build_coordinator(tmp_path, clock, tailoring, ats)
    snapshot = restarted.get("synthetic-001")
    assert snapshot.lifecycle_state == "inviata"
    assert len(snapshot.approvals) == 3
    assert [approval.scope.action for approval in snapshot.approvals] == [
        WorkflowAction.PREPARE,
        WorkflowAction.FILL,
        WorkflowAction.SUBMIT,
    ]
    assert len(snapshot.submission_intents) == 1
    assert snapshot.outcome.confirmation_id == "synthetic-confirmation-001"
    assert snapshot.manifest.application_id == "synthetic-001"
    assert snapshot.manifest.opportunity_version == "opportunity-v1"
    assert snapshot.manifest.artifact_hashes == {
        "cv": "sha256:synthetic-cv",
        "cover_letter": "sha256:synthetic-cover-letter",
    }
    assert snapshot.manifest.answer_hash.startswith("sha256:")
    assert snapshot.manifest.vacancy_freshness == "2026-07-16T10:30:00+00:00"

    report = (tmp_path / "reports" / "synthetic-001.md").read_text()
    assert "# Synthetic application report: synthetic-001" in report
    assert "proposta" in report
    assert "approvata" in report
    assert "CV pronto" in report
    assert "compilazione in corso" in report
    assert "pronta da inviare" in report
    assert "inviata" in report
    assert "artifacts-v1" in report
    assert "sha256:" in report
    assert "work_authorization" in report
    assert "SYNTHETIC-WORK-AUTHORIZATION" in report
    assert "synthetic-confirmation-001" in report


def test_invalid_commands_have_zero_external_effect(tmp_path):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    coordinator.propose(
        application_id="synthetic-002",
        opportunity={**synthetic_opportunity(), "stable_id": "acme:research-43"},
        version="opportunity-v1",
    )

    expired = coordinator.issue_authorization(
        "synthetic-001",
        WorkflowAction.PREPARE,
        actor="Synthetic Owner",
        ttl=timedelta(minutes=1),
    )
    clock.advance(minutes=2)
    assert coordinator.handle(expired).status == "expired"

    valid = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    cross_application = replace(
        valid, scope=replace(valid.scope, application_id="synthetic-002")
    )
    assert coordinator.handle(cross_application).status == ("mismatched")
    wrong_version = replace(
        valid, scope=replace(valid.scope, version="tampered-version")
    )
    assert coordinator.handle(wrong_version).status == ("mismatched")
    assert tailoring.calls == []

    assert coordinator.handle(valid).status == "completed"
    assert coordinator.handle(valid).status == "replayed"
    assert len(tailoring.calls) == 1

    stale = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    current = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(current).status == "completed"
    assert coordinator.handle(stale).status == "stale"

    assert len(ats.fill_calls) == 1
    assert ats.submit_calls == []


def test_submit_authorizes_one_exact_manifest_and_creates_one_intent(tmp_path):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
        )
    )
    submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    wrong_manifest = replace(submit, scope=replace(submit.scope, version="manifest-v2"))
    assert coordinator.handle(wrong_manifest).status == ("mismatched")
    assert ats.submit_calls == []
    assert coordinator.get("synthetic-001").submission_intents == ()

    assert coordinator.handle(submit).status == "completed"
    assert coordinator.handle(submit).status == "replayed"
    assert len(ats.submit_calls) == 1
    assert len(coordinator.get("synthetic-001").submission_intents) == 1


def test_started_submission_is_durable_and_never_blindly_retried(tmp_path):
    class FailingAts(FakeAts):
        def submit(self, application_id, manifest):
            self.submit_calls.append((application_id, manifest))
            raise TimeoutError("synthetic timeout after click")

    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FailingAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
        )
    )
    submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    assert coordinator.handle(submit).status == "uncertain"
    restarted = build_coordinator(tmp_path, clock, tailoring, ats)
    snapshot = restarted.get("synthetic-001")
    assert snapshot.lifecycle_state == "pronta da inviare"
    assert snapshot.operational_status == "submission_outcome_uncertain"
    assert snapshot.next_action is None
    assert len(snapshot.submission_intents) == 1
    assert restarted.handle(submit).status == "replayed"
    assert len(ats.submit_calls) == 1

    try:
        restarted.issue_authorization(
            "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
        )
    except ValueError as error:
        assert "Next valid action is None" in str(error)
    else:
        raise AssertionError("uncertain submission incorrectly allowed a retry")


def test_prepared_application_expires_after_72_hours_and_requires_repreparation(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill = coordinator.issue_authorization(
        "synthetic-001",
        WorkflowAction.FILL,
        actor="Synthetic Owner",
        ttl=timedelta(hours=96),
    )

    clock.advance(hours=73)
    expired_snapshot = coordinator.get("synthetic-001")
    assert expired_snapshot.operational_status == "expired_preparation"
    assert expired_snapshot.next_action == WorkflowAction.PREPARE
    assert coordinator.handle(fill).status == "expired"
    restarted = build_coordinator(tmp_path, clock, tailoring, ats)
    snapshot = restarted.get("synthetic-001")
    assert snapshot.operational_status == "expired_preparation"
    assert snapshot.next_action == WorkflowAction.PREPARE
    assert ats.fill_calls == []

    prepare_again = restarted.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert prepare_again.scope.version == "opportunity-v1"


def test_submit_authorization_cannot_use_a_preparation_older_than_72_hours(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    submit = coordinator.issue_authorization(
        "synthetic-001",
        WorkflowAction.SUBMIT,
        actor="Synthetic Owner",
        ttl=timedelta(hours=96),
    )

    clock.advance(hours=73)
    result = coordinator.handle(submit)

    assert result.status == "expired"
    assert result.next_action == WorkflowAction.PREPARE
    assert ats.submit_calls == []
    assert coordinator.get("synthetic-001").manifest is None


def test_prepared_application_emits_one_deadline_aware_reminder_after_48_hours(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    coordinator.propose(
        application_id="synthetic-001",
        opportunity={
            **synthetic_opportunity(),
            "application_deadline": "2026-07-19T09:00:00+00:00",
        },
        version="opportunity-v1",
    )
    prepare = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(prepare).status == "completed"

    clock.advance(hours=47)
    assert coordinator.emit_due_preparation_reminders() == ()
    clock.advance(hours=2)

    class ReminderTransport:
        def __init__(self, *, fail=False):
            self.reminders = []
            self.fail = fail

        def send_preparation_reminder(self, reminder):
            self.reminders.append(reminder)
            if self.fail:
                raise RuntimeError("simulated Telegram delivery failure")

    failed_transport = ReminderTransport(fail=True)
    with pytest.raises(RuntimeError, match="Telegram delivery failure"):
        TelegramCommandHandler(
            coordinator, transport=failed_transport
        ).emit_due_preparation_reminders()
    assert (
        coordinator.get("synthetic-001").preparation_reminders[0].delivered_at is None
    )
    transport = ReminderTransport()
    reminders = TelegramCommandHandler(
        coordinator, transport=transport
    ).emit_due_preparation_reminders()

    assert len(reminders) == 1
    assert reminders[0].application_id == "synthetic-001"
    assert reminders[0].priority == "deadline"
    assert reminders[0].deadline_at == "2026-07-19T09:00:00+00:00"
    assert transport.reminders == list(reminders)
    assert reminders[0].reminder_id == failed_transport.reminders[0].reminder_id
    assert reminders[0].company == "Acme AI"
    assert reminders[0].title == "Research Scientist"
    assert coordinator.emit_due_preparation_reminders() == ()
    assert coordinator.get("synthetic-001").preparation_reminders[0].reminder_id == (
        reminders[0].reminder_id
    )
    assert coordinator.get("synthetic-001").preparation_reminders[0].delivered_at == (
        "2026-07-18T11:30:00+00:00"
    )
    report = (tmp_path / "reports" / "synthetic-001.md").read_text()
    assert "Preparation reminder: deadline" in report
    assert "2026-07-19T09:00:00+00:00" in report


def test_capacity_exception_is_typed_stored_and_shown_when_limit_is_exceeded(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(
        tmp_path,
        clock,
        tailoring,
        ats,
        active_preparation_limit=4,
    )
    for index in range(4):
        application_id = f"synthetic-{index}"
        coordinator.propose(
            application_id=application_id,
            opportunity={
                **synthetic_opportunity(),
                "stable_id": f"acme:research-{index}",
            },
            version=f"opportunity-v{index}",
        )
        prepare = coordinator.issue_authorization(
            application_id, WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
        assert coordinator.handle(prepare).status == "completed"

    coordinator.propose(
        application_id="synthetic-normal-overflow",
        opportunity={
            **synthetic_opportunity(),
            "stable_id": "acme:normal-overflow",
        },
        version="opportunity-overflow",
    )
    with pytest.raises(ValueError, match="capacity of 4"):
        coordinator.issue_authorization(
            "synthetic-normal-overflow",
            WorkflowAction.PREPARE,
            actor="Synthetic Owner",
        )

    exception = PreparationCapacityException(
        kind=PreparationCapacityExceptionKind.TOP_TIER,
        reason="Top-tier research fit with exceptional ownership upside",
    )
    coordinator.propose(
        application_id="synthetic-top-tier",
        opportunity={
            **synthetic_opportunity(),
            "stable_id": "acme:top-tier",
            "top_tier": {
                "value": True,
                "explanation": "Exceptional fit and portfolio upside",
            },
        },
        version="opportunity-top-tier",
        capacity_exception=exception,
    )
    prepare_exception = coordinator.issue_authorization(
        "synthetic-top-tier", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )

    assert coordinator.handle(prepare_exception).status == "completed"
    assert coordinator.get("synthetic-top-tier").capacity_exception == exception
    fill_exception = coordinator.issue_authorization(
        "synthetic-top-tier", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(fill_exception).status == "completed"
    summary, _ = TelegramCommandHandler(coordinator).present_submit(
        "synthetic-top-tier", actor="Synthetic Owner"
    )
    assert summary.capacity_exception == (
        "top_tier: Top-tier research fit with exceptional ownership upside"
    )
    report = (tmp_path / "reports" / "synthetic-top-tier.md").read_text()
    assert "Capacity exception: top_tier" in report
    assert exception.reason in report


def test_capacity_policy_accepts_only_configured_range_and_typed_exceptions(
    tmp_path,
):
    for invalid_limit in (3, 7):
        with pytest.raises(ValueError, match="between 4 and 6"):
            build_coordinator(
                tmp_path / str(invalid_limit),
                AdjustableClock(),
                FakeTailoring(),
                FakeAts(),
                active_preparation_limit=invalid_limit,
            )
    with pytest.raises(ValueError):
        PreparationCapacityException(kind="manual", reason="manual overflow")
    with pytest.raises(ValueError, match="requires a deadline"):
        PreparationCapacityException(
            kind=PreparationCapacityExceptionKind.DEADLINE,
            reason="Role closes before a normal slot can open",
        )
    deadline = PreparationCapacityException(
        kind=PreparationCapacityExceptionKind.DEADLINE,
        reason="Role closes before a normal slot can open",
        deadline_at="2026-07-18T12:00:00+00:00",
    )
    assert deadline.kind == "deadline"
    coordinator = build_coordinator(
        tmp_path / "policy",
        AdjustableClock(),
        FakeTailoring(),
        FakeAts(),
    )
    with pytest.raises(ValueError, match="top-tier/deadline policy"):
        coordinator.propose(
            application_id="synthetic-unqualified",
            opportunity=synthetic_opportunity(),
            version="opportunity-v1",
            capacity_exception=PreparationCapacityException(
                kind=PreparationCapacityExceptionKind.TOP_TIER,
                reason="Unverified exception",
            ),
        )
    admitted = coordinator.propose(
        application_id="synthetic-deadline",
        opportunity={
            **synthetic_opportunity(),
            "stable_id": "acme:deadline",
            "application_deadline": "2026-07-18T12:00:00+00:00",
        },
        version="opportunity-deadline",
        capacity_exception=deadline,
    )
    assert admitted.capacity_exception == deadline


def test_past_deadline_cannot_create_a_capacity_exception(tmp_path):
    clock = AdjustableClock()
    deadline_at = (clock.now() - timedelta(minutes=1)).isoformat()
    coordinator = build_coordinator(
        tmp_path, clock, FakeTailoring(), FakeAts(), active_preparation_limit=4
    )

    with pytest.raises(ValueError, match="does not match the top-tier/deadline policy"):
        coordinator.propose(
            application_id="synthetic-expired-deadline",
            opportunity={
                **synthetic_opportunity(),
                "stable_id": "acme:expired-deadline",
                "application_deadline": deadline_at,
            },
            version="opportunity-expired-deadline",
            capacity_exception=PreparationCapacityException(
                kind=PreparationCapacityExceptionKind.DEADLINE,
                reason="The deadline has already passed",
                deadline_at=deadline_at,
            ),
        )


def test_mismatched_deadline_exception_is_revoked_before_prepare(tmp_path):
    clock = AdjustableClock()
    opportunity_deadline = (clock.now() + timedelta(hours=24)).isoformat()
    exception_deadline = (clock.now() + timedelta(hours=48)).isoformat()
    JsonApplicationStore(tmp_path / "state").save(
        ApplicationSnapshot(
            application_id="synthetic-imported",
            opportunity={
                **synthetic_opportunity(),
                "application_deadline": opportunity_deadline,
            },
            opportunity_version="opportunity-imported",
            lifecycle_state=LifecycleState.PROPOSED,
            authorization_version="opportunity-imported",
            history=(
                LifecycleEvent(
                    LifecycleState.PROPOSED,
                    "2026-07-16T10:00:00+00:00",
                ),
            ),
            capacity_exception=PreparationCapacityException(
                kind=PreparationCapacityExceptionKind.DEADLINE,
                reason="Imported stale exception",
                deadline_at=exception_deadline,
            ),
        )
    )
    coordinator = build_coordinator(
        tmp_path, clock, FakeTailoring(), FakeAts(), active_preparation_limit=4
    )

    command = coordinator.issue_authorization(
        "synthetic-imported", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )

    assert command.scope.application_id == "synthetic-imported"
    assert coordinator.get("synthetic-imported").capacity_exception is None


def test_expired_deadline_exception_is_revoked_before_prepare_authorization(
    tmp_path,
):
    clock = AdjustableClock()
    coordinator = build_coordinator(
        tmp_path, clock, FakeTailoring(), FakeAts(), active_preparation_limit=4
    )
    deadline_at = (clock.now() + timedelta(hours=1)).isoformat()
    coordinator.propose(
        application_id="synthetic-deadline",
        opportunity={
            **synthetic_opportunity(),
            "stable_id": "acme:deadline",
            "application_deadline": deadline_at,
        },
        version="opportunity-deadline",
        capacity_exception=PreparationCapacityException(
            kind=PreparationCapacityExceptionKind.DEADLINE,
            reason="Role closes before a normal slot can open",
            deadline_at=deadline_at,
        ),
    )
    for index in range(4):
        application_id = f"active-{index}"
        coordinator.propose(
            application_id=application_id,
            opportunity={
                **synthetic_opportunity(),
                "stable_id": f"acme:active-{index}",
            },
            version=f"opportunity-active-{index}",
        )
        prepare = coordinator.issue_authorization(
            application_id, WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
        assert coordinator.handle(prepare).status == "completed"

    clock.advance(hours=2)

    with pytest.raises(ValueError, match="capacity of 4 reached"):
        coordinator.issue_authorization(
            "synthetic-deadline", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    assert coordinator.get("synthetic-deadline").capacity_exception is None


def test_deadline_exception_is_revalidated_when_prepare_is_claimed(tmp_path):
    clock = AdjustableClock()
    coordinator = build_coordinator(
        tmp_path, clock, FakeTailoring(), FakeAts(), active_preparation_limit=4
    )
    deadline_at = (clock.now() + timedelta(hours=1)).isoformat()
    coordinator.propose(
        application_id="synthetic-deadline",
        opportunity={
            **synthetic_opportunity(),
            "stable_id": "acme:deadline",
            "application_deadline": deadline_at,
        },
        version="opportunity-deadline",
        capacity_exception=PreparationCapacityException(
            kind=PreparationCapacityExceptionKind.DEADLINE,
            reason="Role closes before a normal slot can open",
            deadline_at=deadline_at,
        ),
    )
    for index in range(4):
        application_id = f"active-{index}"
        coordinator.propose(
            application_id=application_id,
            opportunity={
                **synthetic_opportunity(),
                "stable_id": f"acme:active-{index}",
            },
            version=f"opportunity-active-{index}",
        )
        prepare = coordinator.issue_authorization(
            application_id, WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
        assert coordinator.handle(prepare).status == "completed"
    deadline_prepare = coordinator.issue_authorization(
        "synthetic-deadline",
        WorkflowAction.PREPARE,
        actor="Synthetic Owner",
        ttl=timedelta(hours=3),
    )

    clock.advance(hours=2)
    result = coordinator.handle(deadline_prepare)

    assert result.status == "capacity_reached"
    assert coordinator.get("synthetic-deadline").capacity_exception is None


def test_concurrent_prepare_claims_cannot_cross_global_capacity(tmp_path):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(
        tmp_path,
        clock,
        tailoring,
        ats,
        active_preparation_limit=4,
    )
    for index in range(3):
        application_id = f"active-{index}"
        coordinator.propose(
            application_id=application_id,
            opportunity={
                **synthetic_opportunity(),
                "stable_id": f"acme:active-{index}",
            },
            version=f"opportunity-active-{index}",
        )
        prepare = coordinator.issue_authorization(
            application_id, WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
        assert coordinator.handle(prepare).status == "completed"
    commands = []
    for index in range(2):
        application_id = f"candidate-{index}"
        coordinator.propose(
            application_id=application_id,
            opportunity={
                **synthetic_opportunity(),
                "stable_id": f"acme:candidate-{index}",
            },
            version=f"opportunity-candidate-{index}",
        )
        commands.append(
            coordinator.issue_authorization(
                application_id, WorkflowAction.PREPARE, actor="Synthetic Owner"
            )
        )
    first = build_coordinator(
        tmp_path,
        clock,
        tailoring,
        ats,
        active_preparation_limit=4,
    )
    second = build_coordinator(
        tmp_path,
        clock,
        tailoring,
        ats,
        active_preparation_limit=4,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(worker.handle, command)
            for worker, command in zip((first, second), commands, strict=True)
        ]
        statuses = sorted(result.result().status for result in results)

    assert statuses == ["capacity_reached", "completed"]
    assert len(tailoring.calls) == 4


def test_legacy_application_snapshot_migrates_new_freshness_history_fields(
    tmp_path,
):
    coordinator = build_coordinator(
        tmp_path, AdjustableClock(), FakeTailoring(), FakeAts()
    )
    snapshot = coordinator.propose(
        application_id="synthetic-legacy",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    legacy = snapshot.to_dict()
    legacy.pop("capacity_exception")
    legacy.pop("preparation_reminders")
    legacy.pop("prior_applications")

    migrated = ApplicationSnapshot.from_dict(legacy)

    assert migrated.capacity_exception is None
    assert migrated.preparation_reminders == ()
    assert migrated.prior_applications == ()


def test_active_prior_application_is_shown_and_blocks_duplicate_submit_authorization(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    reopened_opportunity = {
        **synthetic_opportunity(),
        "official_description": "Build trustworthy multimodal systems.",
    }
    reopened = coordinator.propose(
        application_id="synthetic-002",
        opportunity=reopened_opportunity,
        version="opportunity-v2",
    )
    assert reopened.prior_applications[0].application_id == "synthetic-001"
    assert reopened.prior_applications[0].is_active is False
    first_submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    assert coordinator.handle(first_submit).status == "completed"
    assert coordinator.get("synthetic-002").prior_applications[0].is_active is True
    prepare = coordinator.issue_authorization(
        "synthetic-002", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(prepare).status == "completed"
    fill = coordinator.issue_authorization(
        "synthetic-002", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(fill).status == "completed"

    class StatusTransport:
        def __init__(self):
            self.statuses = []

        def send_status(self, value):
            self.statuses.append(value)

    transport = StatusTransport()
    telegram = TelegramCommandHandler(coordinator, transport=transport)
    with pytest.raises(ValueError, match="synthetic-001.*inviata"):
        telegram.present_submit("synthetic-002", actor="Synthetic Owner")

    assert "synthetic-001" in transport.statuses[0]
    assert "inviata" in transport.statuses[0]
    assert len(ats.submit_calls) == 1
    assert len(coordinator.get("synthetic-002").authorizations) == 2
    report = (tmp_path / "reports" / "synthetic-002.md").read_text()
    assert "Prior application: synthetic-001" in report
    assert "active ATS application" in report


def test_rejected_role_can_reopen_with_material_diff_and_new_submit_authorization(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    first_submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    assert coordinator.handle(first_submit).status == "completed"
    coordinator.record_correspondence(
        CorrespondenceEvent(
            event_id="rejection:synthetic-001",
            application_id="synthetic-001",
            message_id="gmail-rejection-1",
            thread_id="thread-1",
            classification=CorrespondenceClassification.REJECTION,
            sender="careers@acme.example",
            subject="Application update",
            received_at="2026-07-17T10:30:00+00:00",
            recorded_at="2026-07-17T10:30:00+00:00",
            summary="Acme did not proceed with the earlier role.",
        )
    )

    with pytest.raises(ValueError, match="no material changes"):
        coordinator.propose(
            application_id="synthetic-unchanged",
            opportunity=synthetic_opportunity(),
            version="opportunity-v1-reopened",
        )

    reopened = coordinator.propose(
        application_id="synthetic-reopened",
        opportunity={
            **synthetic_opportunity(),
            "location": "Basel",
            "official_description": "Lead trustworthy multimodal research.",
        },
        version="opportunity-v2",
    )
    prior = reopened.prior_applications[0]

    assert prior.application_id == "synthetic-001"
    assert prior.lifecycle_state == "rifiutata"
    assert prior.is_active is False
    assert prior.material_changes == ("location", "official_description")
    prepare = coordinator.issue_authorization(
        "synthetic-reopened", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(prepare).status == "completed"
    fill = coordinator.issue_authorization(
        "synthetic-reopened", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(fill).status == "completed"
    new_submit = coordinator.issue_authorization(
        "synthetic-reopened", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    assert new_submit.scope.application_id == "synthetic-reopened"
    summary, _ = TelegramCommandHandler(coordinator).present_submit(
        "synthetic-reopened", actor="Synthetic Owner"
    )
    assert summary.prior_applications == (
        "synthetic-001: rifiutata; changes: location, official_description",
    )
    report = (tmp_path / "reports" / "synthetic-reopened.md").read_text()
    assert "historical application" in report
    assert "changes: location, official_description" in report


def test_reopened_role_reports_all_material_role_changes(tmp_path):
    prior_opportunity = {
        **synthetic_opportunity(),
        "team": "Research",
        "seniority": "Senior",
        "requirements": ["Python", "Computer vision"],
        "ownership": {"headquarters": "Switzerland", "eligible": True},
        "sponsorship": {"status": "not stated", "verified_at": "2026-07-01"},
        "language": "English",
        "official_job_id": "JOB-42",
        "official_job_version": "2026-07-01",
    }
    store = JsonApplicationStore(tmp_path / "state")
    store.save(
        ApplicationSnapshot(
            application_id="synthetic-prior",
            opportunity=prior_opportunity,
            opportunity_version="opportunity-v1",
            lifecycle_state=LifecycleState.REJECTED,
            authorization_version="opportunity-v1",
            history=(
                LifecycleEvent(
                    LifecycleState.REJECTED,
                    "2026-07-10T10:30:00+00:00",
                ),
            ),
        )
    )
    coordinator = build_coordinator(
        tmp_path, AdjustableClock(), FakeTailoring(), FakeAts()
    )

    reopened = coordinator.propose(
        application_id="synthetic-reopened",
        opportunity={
            **prior_opportunity,
            "team": "Applied Research",
            "seniority": "Staff",
            "requirements": ["Python", "Multimodal systems"],
            "ownership": {"headquarters": "France", "eligible": True},
            "sponsorship": {"status": "available", "verified_at": "2026-07-16"},
            "language": "English and French",
            "official_job_id": "JOB-84",
            "official_job_version": "2026-07-16",
        },
        version="opportunity-v2",
    )

    assert reopened.prior_applications[0].material_changes == (
        "team",
        "seniority",
        "requirements",
        "ownership",
        "sponsorship",
        "language",
        "official_job_id",
        "official_job_version",
    )


def test_material_role_diff_normalizes_case_whitespace_and_mapping_order(
    tmp_path,
):
    prior_opportunity = {
        **synthetic_opportunity(),
        "requirements": ["Python", "Computer Vision"],
        "ownership": {"eligible": True, "headquarters": "Switzerland"},
    }
    JsonApplicationStore(tmp_path / "state").save(
        ApplicationSnapshot(
            application_id="synthetic-prior",
            opportunity=prior_opportunity,
            opportunity_version="opportunity-v1",
            lifecycle_state=LifecycleState.REJECTED,
            authorization_version="opportunity-v1",
            history=(
                LifecycleEvent(
                    LifecycleState.REJECTED,
                    "2026-07-10T10:30:00+00:00",
                ),
            ),
        )
    )
    coordinator = build_coordinator(
        tmp_path, AdjustableClock(), FakeTailoring(), FakeAts()
    )

    with pytest.raises(ValueError, match="no material changes"):
        coordinator.propose(
            application_id="synthetic-equivalent",
            opportunity={
                **prior_opportunity,
                "company": "  ACME   ai ",
                "requirements": [" python ", "computer   vision"],
                "ownership": {
                    "headquarters": " switzerland ",
                    "eligible": True,
                },
            },
            version="opportunity-v2",
        )


def test_imported_discard_history_does_not_permanently_block_changed_role(
    tmp_path,
):
    store = JsonApplicationStore(tmp_path / "state")
    store.save(
        ApplicationSnapshot(
            application_id="synthetic-discarded",
            opportunity=synthetic_opportunity(),
            opportunity_version="opportunity-v1",
            lifecycle_state=LifecycleState.DISCARDED,
            authorization_version="opportunity-v1",
            history=(
                LifecycleEvent(
                    LifecycleState.DISCARDED,
                    "2026-06-10T10:30:00+00:00",
                ),
            ),
        )
    )
    coordinator = build_coordinator(
        tmp_path, AdjustableClock(), FakeTailoring(), FakeAts()
    )

    reopened = coordinator.propose(
        application_id="synthetic-after-discard",
        opportunity={**synthetic_opportunity(), "location": "Basel"},
        version="opportunity-v2",
    )

    assert reopened.prior_applications[0].lifecycle_state == "scartata"
    assert reopened.prior_applications[0].is_active is False
    prepare = coordinator.issue_authorization(
        "synthetic-after-discard", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert prepare.scope.application_id == "synthetic-after-discard"


def test_changed_official_vacancy_invalidates_submit_authorization(tmp_path):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    official = FakeOfficialVacancies()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats, official)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    official.current = replace(
        official.current,
        fingerprint="sha256:changed-vacancy",
        freshness="2026-07-17T10:30:00+00:00",
    )

    assert coordinator.handle(submit).status == "stale"
    snapshot = coordinator.get("synthetic-001")
    assert snapshot.operational_status == "vacancy_changed"
    assert snapshot.manifest is None
    assert snapshot.next_action == WorkflowAction.PREPARE
    assert snapshot.submission_intents == ()
    assert ats.submit_calls == []
    assert len(official.retrieve_calls) == 1
    assert len(official.revalidate_calls) == 1


def test_concurrent_callback_deliveries_claim_one_fill(tmp_path):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    official = FakeOfficialVacancies()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats, official)
    propose_and_prepare(coordinator)
    fill = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    first = build_coordinator(tmp_path, clock, tailoring, ats, official)
    second = build_coordinator(tmp_path, clock, tailoring, ats, official)

    with ThreadPoolExecutor(max_workers=2) as executor:
        deliveries = [
            executor.submit(first.handle, fill),
            executor.submit(second.handle, fill),
        ]
        statuses = sorted(delivery.result().status for delivery in deliveries)

    assert statuses == ["completed", "replayed"]
    assert len(ats.fill_calls) == 1
    assert ats.submit_calls == []


def test_crash_after_submission_intent_reconciles_without_second_submit(tmp_path):
    class CrashingAts(FakeAts):
        def submit(self, application_id, manifest):
            self.submit_calls.append((application_id, manifest))
            raise KeyboardInterrupt(
                "synthetic process crash before outcome persistence"
            )

    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = CrashingAts()
    official = FakeOfficialVacancies()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats, official)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    try:
        coordinator.handle(submit)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("synthetic crash did not interrupt submission")

    restarted = build_coordinator(tmp_path, clock, tailoring, ats, official)
    started = restarted.get("synthetic-001")
    assert started.operational_status == "submission_started"
    assert len(started.submission_intents) == 1
    assert started.next_action is None

    reconciled = restarted.reconcile_submission(
        "synthetic-001",
        SubmissionOutcome(
            status="verified",
            evidence=SubmissionEvidence(
                captured_at="2026-07-16T10:30:00+00:00",
                verified_by=(SubmissionVerificationKind.CONFIRMATION_ID,),
                confirmation_id="reconciled-confirmation-001",
            ),
        ),
    )
    assert reconciled.status == "completed"
    assert restarted.get("synthetic-001").lifecycle_state == "inviata"
    assert len(ats.submit_calls) == 1


def test_prepare_intent_resumes_after_crash_before_adapter_side_effect(tmp_path):
    class CrashBeforeTailoring(FakeTailoring):
        def __init__(self):
            super().__init__()
            self.crashed = False

        def prepare(self, application_id, intent_id, opportunity, official_vacancy):
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt("synthetic crash before tailoring")
            return super().prepare(
                application_id, intent_id, opportunity, official_vacancy
            )

    clock = AdjustableClock()
    tailoring = CrashBeforeTailoring()
    ats = FakeAts()
    official = FakeOfficialVacancies()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats, official)
    coordinator.propose(
        application_id="synthetic-001",
        opportunity=synthetic_opportunity(),
        version="opportunity-v1",
    )
    prepare = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )

    try:
        coordinator.handle(prepare)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("synthetic prepare crash did not interrupt")

    restarted = build_coordinator(tmp_path, clock, tailoring, ats, official)
    assert restarted.get("synthetic-001").next_action is None
    assert restarted.resume_pending("synthetic-001").status == "completed"
    assert restarted.get("synthetic-001").lifecycle_state == "CV pronto"
    assert len(tailoring.calls) == 1


def test_fill_intent_reuses_idempotent_result_after_crash_side_effect(tmp_path):
    class CrashAfterFill(FakeAts):
        def __init__(self):
            super().__init__()
            self.crashed = False

        def fill(self, application_id, intent_id, artifacts):
            result = super().fill(application_id, intent_id, artifacts)
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt("synthetic crash after fill side effect")
            return result

    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = CrashAfterFill()
    official = FakeOfficialVacancies()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats, official)
    propose_and_prepare(coordinator)
    fill = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
    )

    try:
        coordinator.handle(fill)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("synthetic fill crash did not interrupt")

    restarted = build_coordinator(tmp_path, clock, tailoring, ats, official)
    assert restarted.get("synthetic-001").next_action is None
    assert restarted.resume_pending("synthetic-001").status == "completed"
    assert restarted.get("synthetic-001").lifecycle_state == "pronta da inviare"
    assert len(ats.fill_calls) == 1


def test_pending_fill_recovery_never_uses_expired_artifacts(tmp_path):
    class CrashBeforeFill(FakeAts):
        def __init__(self):
            super().__init__()
            self.crashed = False

        def fill(self, application_id, intent_id, artifacts):
            if not self.crashed:
                self.crashed = True
                raise KeyboardInterrupt("synthetic crash before expired fill")
            return super().fill(application_id, intent_id, artifacts)

    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = CrashBeforeFill()
    official = FakeOfficialVacancies()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats, official)
    propose_and_prepare(coordinator)
    fill = coordinator.issue_authorization(
        "synthetic-001",
        WorkflowAction.FILL,
        actor="Synthetic Owner",
        ttl=timedelta(hours=96),
    )
    try:
        coordinator.handle(fill)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("synthetic fill crash did not interrupt")

    clock.advance(hours=73)
    restarted = build_coordinator(tmp_path, clock, tailoring, ats, official)
    assert restarted.resume_pending("synthetic-001").status == "expired"
    snapshot = restarted.get("synthetic-001")
    assert snapshot.next_action == WorkflowAction.PREPARE
    assert snapshot.operation_intents[-1].cancelled_at is not None
    assert ats.fill_calls == []


def test_rileggi_cv_master_invalidates_downstream_approval_for_old_bundle(tmp_path):
    class ReloadingTailoring(FakeTailoring):
        def prepare(self, application_id, intent_id, opportunity, official_vacancy):
            result = super().prepare(
                application_id, intent_id, opportunity, official_vacancy
            )
            return replace(result, evidence_source_version="evidence-v1")

        def reload_master_cv(self):
            return "evidence-v2"

    clock = AdjustableClock()
    tailoring = ReloadingTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    assert coordinator.reload_master_cv() == "evidence-v2"

    snapshot = coordinator.get("synthetic-001")
    assert snapshot.artifacts is None
    assert snapshot.manifest is None
    assert snapshot.authorization_version == "opportunity-v1"
    assert snapshot.next_action == WorkflowAction.PREPARE
    fill_approval = next(
        approval
        for approval in snapshot.approvals
        if approval.scope.action == WorkflowAction.FILL
    )
    assert fill_approval.invalidated_at == "2026-07-16T10:30:00+00:00"
    assert fill_approval.invalidation_reason == "master CV or evidence bank reloaded"
    assert coordinator.handle(submit).status == "stale"
    assert ats.submit_calls == []


def test_telegram_exposes_rileggi_cv_master_as_an_explicit_command(tmp_path):
    class ReloadingTailoring(FakeTailoring):
        def reload_master_cv(self):
            return "evidence-v2"

    coordinator = build_coordinator(
        tmp_path, AdjustableClock(), ReloadingTailoring(), FakeAts()
    )
    telegram = TelegramCommandHandler(coordinator)

    assert telegram.command_labels == ("Rileggi CV master",)
    assert telegram.handle_command("Rileggi CV master") == "evidence-v2"

    with pytest.raises(ValueError, match="Unsupported Telegram command"):
        telegram.handle_command("Rileggi altro")


def test_changed_artifact_bundle_invalidates_fill_approval_for_prior_version(
    tmp_path,
):
    class VersionedTailoring(FakeTailoring):
        def prepare(self, application_id, intent_id, opportunity, official_vacancy):
            result = super().prepare(
                application_id, intent_id, opportunity, official_vacancy
            )
            version_number = len(self.calls)
            return replace(
                result,
                version=f"artifacts-v{version_number}",
                evidence_source_version=f"evidence-v{version_number}",
            )

    clock = AdjustableClock()
    tailoring = VersionedTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)

    clock.advance(hours=73)
    prepare_again = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )
    assert coordinator.handle(prepare_again).status == "completed"

    snapshot = coordinator.get("synthetic-001")
    assert snapshot.artifacts.version == "artifacts-v2"
    prior_fill = next(
        approval
        for approval in snapshot.approvals
        if approval.scope.action == WorkflowAction.FILL
    )
    assert prior_fill.invalidated_at == "2026-07-19T11:30:00+00:00"
    assert prior_fill.invalidation_reason == "application artifact bundle changed"


def test_modified_artifacts_after_fill_authorization_block_ats_side_effect(tmp_path):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.FILL, actor="Synthetic Owner"
    )

    tailoring.artifacts_intact = False

    assert coordinator.handle(fill).status == "stale"
    snapshot = coordinator.get("synthetic-001")
    assert snapshot.artifacts is None
    assert snapshot.next_action == WorkflowAction.PREPARE
    assert ats.fill_calls == []


def test_modified_artifacts_after_submit_authorization_invalidate_fill_approval(
    tmp_path,
):
    clock = AdjustableClock()
    tailoring = FakeTailoring()
    ats = FakeAts()
    coordinator = build_coordinator(tmp_path, clock, tailoring, ats)
    propose_and_prepare(coordinator)
    fill_prepared_application(coordinator)
    submit = coordinator.issue_authorization(
        "synthetic-001", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )

    tailoring.artifacts_intact = False

    assert coordinator.handle(submit).status == "stale"
    snapshot = coordinator.get("synthetic-001")
    assert snapshot.artifacts is None
    assert snapshot.manifest is None
    assert snapshot.next_action == WorkflowAction.PREPARE
    fill_approval = next(
        approval
        for approval in snapshot.approvals
        if approval.scope.action == WorkflowAction.FILL
    )
    assert fill_approval.invalidated_at == "2026-07-16T10:30:00+00:00"
    assert fill_approval.invalidation_reason == (
        "published application artifact bytes changed"
    )
    assert ats.submit_calls == []


def test_report_renders_legacy_matrix_with_canonical_rows_and_status(tmp_path):
    clock = AdjustableClock()
    coordinator = build_coordinator(tmp_path, clock, FakeTailoring(), FakeAts())
    opportunity = {
        **synthetic_opportunity(),
        "requirements_evidence_matrix": {
            "version": "grading-v1",
            "requirements": [
                {
                    "requirement_id": "req.python",
                    "requirement": "Python",
                    "importance": "required",
                    "status": "supported",
                    "evidence_ids": ["evidence-python"],
                    "explanation": "Approved Python evidence.",
                }
            ],
        },
    }

    coordinator.propose(
        application_id="synthetic-001",
        opportunity=opportunity,
        version="opportunity-v1",
    )

    report = (tmp_path / "reports" / "synthetic-001.md").read_text()
    assert "- req.python: Python [matched] (evidence: evidence-python)" in report
