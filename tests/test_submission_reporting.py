from datetime import datetime, timezone
import csv
from dataclasses import replace
import hashlib
import json
import pathlib
import stat
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import (  # noqa: E402
    AnswerDisclosure,
    AnswerVisibility,
    FilledApplication,
    OfficialVacancy,
    PreparationCapacityException,
    PreparationCapacityExceptionKind,
    PreparationReminder,
    PriorApplicationEvidence,
    PreparedArtifacts,
    ReviewEvidence,
    ReviewEvidencePage,
    SubmissionEvidence,
    SubmissionOutcome,
    SubmissionVerificationKind,
)
from application_interventions import (  # noqa: E402
    BrowserInterventionRequired,
    InterventionContinuation,
    InterventionContinuationKind,
    InterventionKind,
    InterventionRecord,
    SubmissionInspection,
    SubmissionInspectionStatus,
    UncertainSubmissionRecord,
)
from application_packages import LocalApplicationPackageWriter, _index_row  # noqa: E402
from application_telegram import TelegramCommandHandler  # noqa: E402
from application_workflow import (  # noqa: E402
    ApplicationWorkflowCoordinator,
    JsonApplicationStore,
    WorkflowAction,
)
from telegram_applications import (  # noqa: E402
    TelegramApplicationApi,
    TelegramApplicationConsumer,
)
from telegram_delivery import TelegramDeliveryLedger  # noqa: E402
from workday_submission import (  # noqa: E402
    CareerMailboxReceipt,
    LiveWorkdaySubmissionBrowser,
    ScopedWorkdayReview,
    WorkdayApplicationAdapter,
    WorkdayConfirmationCapture,
    WorkdayConfirmationMarker,
    WorkdaySubmissionCapture,
)


def file_hash(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


class FileTailoring:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def prepare(self, application_id, intent_id, opportunity, official_vacancy):
        return self.artifacts

    def reload_master_cv(self):
        return "evidence-v1"

    def verify_artifacts(self, artifacts):
        return file_hash(pathlib.Path(artifacts.cv_path)) == artifacts.cv_hash and file_hash(
            pathlib.Path(artifacts.cover_letter_path)
        ) == artifacts.cover_letter_hash


class FixedVacancy:
    def __init__(self):
        self.current = OfficialVacancy(
            version="vacancy-v1",
            fingerprint="sha256:verified-role",
            freshness="2026-07-16T10:30:00+00:00",
            description="Build reliable AI systems from the official vacancy.",
        )
        self.revalidate_calls = []

    def retrieve(self, opportunity):
        return self.current

    def revalidate(self, opportunity, previous):
        self.revalidate_calls.append((opportunity, previous))
        return self.current


class CapturingTelegram:
    def __init__(self):
        self.pre_submit = []

    def send_pre_submit(self, summary, command):
        self.pre_submit.append((summary, command))

    def send_status(self, message):
        pass


class QueueingTelegram(CapturingTelegram):
    def __init__(self):
        super().__init__()
        self.updates = []
        self.statuses = []
        self.acknowledged = []

    def send_status(self, message):
        self.statuses.append(message)

    def poll_updates(self, *, offset, timeout):
        updates, self.updates = self.updates, []
        return updates

    def acknowledge_callback(self, callback_query_id, text):
        self.acknowledged.append((callback_query_id, text))

    def is_authorized_callback(self, callback):
        return (
            callback.get("from", {}).get("id") == "user-42"
            and callback.get("message", {}).get("chat", {}).get("id")
            == "chat-42"
        )


class EvidenceAts:
    def __init__(self, store):
        self.store = store
        self.submit_calls = []
        self.intent_was_durable = False

    def fill(self, application_id, intent_id, artifacts):
        answers = {
            "work_authorization": "SYNTHETIC-WORK-AUTHORIZATION",
            "references": "Not provided",
            "private_disability_answer": "Yes",
        }
        return FilledApplication(
            answers=answers,
            artifact_version=artifacts.version,
            unresolved_warnings=("Confirm availability date during interview",),
            review_evidence=ReviewEvidence(
                page=ReviewEvidencePage.REVIEW,
                form_snapshot=dict(answers),
                attachment_hashes={
                    "cv": artifacts.cv_hash,
                    "cover_letter": artifacts.cover_letter_hash,
                },
            ),
            answer_disclosures=(
                AnswerDisclosure(
                    "work_authorization", AnswerVisibility.PUBLIC_SUMMARY
                ),
                AnswerDisclosure("references", AnswerVisibility.PUBLIC_SUMMARY),
                AnswerDisclosure(
                    "private_disability_answer", AnswerVisibility.LOCAL_ONLY
                ),
            ),
        )

    def validate_submit(self, application_id, manifest):
        return True

    def submit(self, application_id, manifest):
        persisted = self.store.load(application_id)
        self.intent_was_durable = (
            len(persisted.submission_intents) == 1
            and persisted.submission_intents[0].manifest_version == manifest.version
            and persisted.outcome is None
        )
        self.submit_calls.append((application_id, manifest))
        return SubmissionOutcome(
            status="verified",
            evidence=SubmissionEvidence(
                captured_at="2026-07-16T10:30:00+00:00",
                verified_by=(
                    SubmissionVerificationKind.CONFIRMATION_PAGE,
                    SubmissionVerificationKind.CONFIRMATION_ID,
                    SubmissionVerificationKind.ATS_SUBMITTED,
                    SubmissionVerificationKind.EMAIL_RECEIPT,
                ),
                confirmation_page="Application submitted successfully",
                confirmation_id="confirmation-001",
                ats_application_id="WORKDAY-42",
                ats_status="Submitted",
                email_receipt_id="gmail-message-99",
                email_receipt_received_at="2026-07-16T10:31:00+00:00",
            ),
        )


def artifacts(tmp_path):
    cv = tmp_path / "cv.pdf"
    cover = tmp_path / "cover-letter.pdf"
    cv.write_bytes(b"exact tailored cv")
    cover.write_bytes(b"exact tailored cover letter")
    return PreparedArtifacts(
        version="artifacts-v1",
        cv_path=str(cv),
        cover_letter_path=str(cover),
        cv_hash=file_hash(cv),
        cover_letter_hash=file_hash(cover),
    )


def build_ready_application(tmp_path):
    store = JsonApplicationStore(tmp_path / "state")
    package_writer = LocalApplicationPackageWriter(tmp_path / "private-applications")
    vacancy = FixedVacancy()
    ats = EvidenceAts(store)
    coordinator = ApplicationWorkflowCoordinator(
        store=store,
        tailoring=FileTailoring(artifacts(tmp_path)),
        ats=ats,
        report_writer=package_writer,
        official_vacancies=vacancy,
        clock=FixedClock(),
        token_factory=iter(("prepare-token", "fill-token", "submit-token")).__next__,
    )
    coordinator.propose(
        application_id="workday-001",
        opportunity={
            "company": "Example AI",
            "title": "AI Scientist",
            "location": "Zurich",
            "official_url": "https://jobs.example/workday-001",
            "evaluation_brief": {"fit": "strong", "risk": "low"},
        },
        version="opportunity-v1",
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "workday-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "workday-001", WorkflowAction.FILL, actor="Synthetic Owner"
        )
    )
    return coordinator, store, vacancy, ats, package_writer


def test_telegram_presents_exact_non_sensitive_manifest_immediately_before_invia(
    tmp_path,
):
    coordinator, _, _, ats, _ = build_ready_application(tmp_path)
    transport = CapturingTelegram()
    telegram = TelegramCommandHandler(
        coordinator,
        transport=transport,
    )

    command = telegram.create_callback(
        "workday-001", "Invia", actor="Synthetic Owner"
    )

    assert len(transport.pre_submit) == 1
    summary, shown_command = transport.pre_submit[0]
    assert shown_command == command
    assert summary.application_id == "workday-001"
    assert summary.manifest_version == command.scope.version
    assert summary.company == "Example AI"
    assert summary.title == "AI Scientist"
    assert summary.location == "Zurich"
    assert summary.official_vacancy_version == "vacancy-v1"
    assert summary.role_fingerprint == "sha256:verified-role"
    assert [(item.kind, item.path, item.sha256) for item in summary.attachments] == [
        ("cv", str(tmp_path / "cv.pdf"), file_hash(tmp_path / "cv.pdf")),
        (
            "cover_letter",
            str(tmp_path / "cover-letter.pdf"),
            file_hash(tmp_path / "cover-letter.pdf"),
        ),
    ]
    assert summary.principal_answers == (
        ("references", "Not provided"),
        ("work_authorization", "SYNTHETIC-WORK-AUTHORIZATION"),
    )
    assert "private_disability_answer" not in repr(summary)
    assert summary.unresolved_warnings == (
        "Confirm availability date during interview",
    )
    assert summary.freshness == "2026-07-16T10:30:00+00:00"
    assert ats.submit_calls == []


def test_invia_submits_once_records_rich_evidence_and_writes_private_package(
    tmp_path,
):
    coordinator, _, vacancy, ats, package_writer = build_ready_application(tmp_path)
    telegram = TelegramCommandHandler(coordinator)
    command = telegram.create_callback(
        "workday-001", "Invia", actor="Synthetic Owner"
    )

    result = telegram.handle_callback_data(telegram.encode_callback(command))

    assert result.status == "completed"
    assert result.lifecycle_state == "inviata"
    assert ats.intent_was_durable is True
    assert len(ats.submit_calls) == 1
    assert len(vacancy.revalidate_calls) == 1
    assert telegram.handle_callback_data(telegram.encode_callback(command)).status == (
        "replayed"
    )
    assert len(ats.submit_calls) == 1

    submitted = coordinator.get("workday-001")
    evidence = submitted.outcome.evidence
    assert evidence.confirmation_page == "Application submitted successfully"
    assert evidence.confirmation_id == "confirmation-001"
    assert evidence.ats_application_id == "WORKDAY-42"
    assert evidence.ats_status == "Submitted"
    assert evidence.email_receipt_id == "gmail-message-99"
    assert evidence.email_receipt_received_at == "2026-07-16T10:31:00+00:00"

    package = package_writer.package_path("workday-001")
    assert (package / "official-vacancy.json").exists()
    assert (package / "brief.json").exists()
    assert (package / "answers.json").exists()
    assert (package / "audit.json").exists()
    assert (package / "submission-evidence.json").exists()
    assert (package / "report.md").exists()
    assert (package / "artifacts" / "cv.pdf").read_bytes() == b"exact tailored cv"
    assert (package / "artifacts" / "cover-letter.pdf").read_bytes() == (
        b"exact tailored cover letter"
    )
    assert stat.S_IMODE(package.stat().st_mode) == 0o700
    assert stat.S_IMODE((package / "answers.json").stat().st_mode) == 0o600
    assert json.loads((package / "official-vacancy.json").read_text())["description"] == (
        "Build reliable AI systems from the official vacancy."
    )
    assert json.loads((package / "answers.json").read_text())["answers"][
        "private_disability_answer"
    ] == "Yes"
    audit = json.loads((package / "audit.json").read_text())
    assert len(audit["approvals"]) == 3
    assert audit["history"][-1]["state"] == "inviata"

    markdown_index = package_writer.markdown_index_path.read_text()
    csv_rows = list(csv.DictReader(package_writer.csv_index_path.open()))
    assert package_writer.markdown_index_path.parent == (
        package_writer.csv_index_path.parent
    )
    assert "workday-001 | Example AI | AI Scientist | Zurich | inviata | verified" in (
        markdown_index
    )
    assert csv_rows == [
        {
            "application_id": "workday-001",
            "company": "Example AI",
            "title": "AI Scientist",
            "location": "Zurich",
            "lifecycle": "inviata",
            "submission_status": "verified",
            "updated_at": "2026-07-16T10:30:00+00:00",
        }
    ]
    for secret in (
        "private_disability_answer",
        "Yes",
        "confirmation-001",
        "WORKDAY-42",
        "gmail-message-99",
    ):
        assert secret not in markdown_index
        assert secret not in package_writer.csv_index_path.read_text()


def test_failed_package_build_leaves_previous_package_and_index_pair_visible(
    tmp_path,
):
    coordinator, _, _, _, package_writer = build_ready_application(tmp_path)
    telegram = TelegramCommandHandler(coordinator)
    telegram.handle_callback(
        telegram.create_callback("workday-001", "Invia", actor="Synthetic Owner")
    )
    submitted = coordinator.get("workday-001")
    previous_package = package_writer.package_path("workday-001")
    previous_markdown = package_writer.markdown_index_path
    previous_csv = package_writer.csv_index_path
    catalog_path = tmp_path / "private-applications" / "current.json"
    previous_catalog = catalog_path.read_bytes()

    pathlib.Path(submitted.artifacts.cover_letter_path).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="Exact application artifact"):
        package_writer.write(
            replace(submitted, authorization_version="new-package-version")
        )

    assert catalog_path.read_bytes() == previous_catalog
    assert package_writer.package_path("workday-001") == previous_package
    assert package_writer.markdown_index_path == previous_markdown
    assert package_writer.csv_index_path == previous_csv
    assert previous_markdown.is_file()
    assert previous_csv.is_file()


def test_closed_vacancy_invalidates_presented_invia_without_submit(tmp_path):
    coordinator, _, vacancy, ats, _ = build_ready_application(tmp_path)
    telegram = TelegramCommandHandler(coordinator)
    _, command = telegram.present_submit("workday-001", actor="Synthetic Owner")
    vacancy.current = replace(vacancy.current, available=False)

    result = telegram.handle_callback(command)

    assert result.status == "stale"
    assert result.next_action == WorkflowAction.PREPARE
    snapshot = coordinator.get("workday-001")
    assert snapshot.manifest is None
    assert snapshot.operational_status == "vacancy_changed"
    assert snapshot.submission_intents == ()
    assert ats.submit_calls == []


def test_verified_outcome_cannot_be_claimed_without_evidence():
    with pytest.raises(ValueError, match="requires captured evidence"):
        SubmissionOutcome(status="verified")


def test_submission_evidence_requires_typed_positive_marker_and_timestamp():
    with pytest.raises(ValueError, match="timestamp is required"):
        SubmissionEvidence(captured_at="", verified_by=())

    with pytest.raises(ValueError, match="include a timezone"):
        SubmissionEvidence(
            captured_at="2026-07-16T10:30:00",
            verified_by=(),
        )

    with pytest.raises(ValueError, match="submitted status"):
        SubmissionEvidence(
            captured_at="2026-07-16T10:30:00+00:00",
            verified_by=(SubmissionVerificationKind.ATS_SUBMITTED,),
            ats_application_id="WORKDAY-42",
            ats_status="Draft",
        )

    with pytest.raises(ValueError, match="requires captured evidence"):
        SubmissionOutcome(
            status="verified",
            evidence=SubmissionEvidence(
                captured_at="2026-07-16T10:30:00+00:00",
                verified_by=(),
                confirmation_page="Any arbitrary non-empty string",
            ),
        )


def test_concrete_telegram_consumer_routes_invia_to_the_application_workflow(
    tmp_path,
):
    coordinator, _, _, ats, _ = build_ready_application(tmp_path)
    api = QueueingTelegram()
    consumer = TelegramApplicationConsumer(
        coordinator=coordinator,
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "telegram.sqlite"),
    )
    _, command = consumer.present_submit("workday-001", actor="Synthetic Owner")
    api.updates.append(
        {
            "update_id": 42,
            "callback_query": {
                "id": "callback-42",
                "data": TelegramCommandHandler.encode_callback(command),
                "from": {"id": "user-42"},
                "message": {"chat": {"id": "chat-42"}},
            },
        }
    )

    assert consumer.consume_once(timeout=0) == 1
    assert len(ats.submit_calls) == 1
    assert coordinator.get("workday-001").outcome.status == "verified"
    assert api.statuses == ["Candidatura inviata e verificata."]
    assert api.acknowledged == [("callback-42", "completed")]


def test_telegram_consumer_rejects_wrong_user_and_chat_before_consuming_invia(
    tmp_path,
):
    coordinator, _, _, ats, _ = build_ready_application(tmp_path)
    api = QueueingTelegram()
    consumer = TelegramApplicationConsumer(
        coordinator=coordinator,
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "telegram.sqlite"),
    )
    _, command = consumer.present_submit("workday-001", actor="Synthetic Owner")
    api.updates.append(
        {
            "update_id": 43,
            "callback_query": {
                "id": "callback-43",
                "data": TelegramCommandHandler.encode_callback(command),
                "from": {"id": "someone-else"},
                "message": {"chat": {"id": "another-chat"}},
            },
        }
    )

    assert consumer.consume_once(timeout=0) == 0
    assert ats.submit_calls == []
    assert coordinator.get("workday-001").outcome is None
    assert api.acknowledged == [
        ("callback-43", "Utente o chat non autorizzati")
    ]


def test_concrete_telegram_api_sends_exact_summary_with_invia_callback(tmp_path):
    class Response:
        ok = True

        def json(self):
            return {"ok": True, "result": {}}

    class Http:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    http = Http()
    api = TelegramApplicationApi(
        token="bot-token",
        chat_id="chat-42",
        user_id="user-42",
        callback_encoder=TelegramCommandHandler.encode_callback,
        http=http,
    )
    handler = TelegramCommandHandler(coordinator, transport=api)

    _, command = handler.present_submit("workday-001", actor="Synthetic Owner")

    document_posts = [item for item in http.posts if item[0].endswith("sendDocument")]
    assert [item[1]["files"]["document"][0] for item in document_posts] == [
        "cv.pdf",
        "cover_letter.pdf",
    ]
    payload = [item for item in http.posts if item[0].endswith("sendMessage")][0][1][
        "json"
    ]
    assert payload["chat_id"] == "chat-42"
    assert "AI Scientist" in payload["text"]
    assert "private_disability_answer" not in payload["text"]
    assert str(tmp_path) not in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "Invia",
        "callback_data": TelegramCommandHandler.encode_callback(command),
    }


def test_callback_authorization_is_issued_before_any_telegram_attachment(tmp_path):
    class Http:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            raise AssertionError(
                "No Telegram request may precede callback authorization"
            )

    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    http = Http()
    api = TelegramApplicationApi(
        token="bot-token",
        chat_id="chat-42",
        user_id="user-42",
        callback_encoder=lambda command: (_ for _ in ()).throw(
            RuntimeError("worker callback authorization unavailable")
        ),
        http=http,
    )

    with pytest.raises(RuntimeError, match="authorization unavailable"):
        TelegramCommandHandler(coordinator, transport=api).present_submit(
            "workday-001", actor="Synthetic Owner"
        )

    assert http.posts == []


def test_workday_submission_adapter_captures_all_available_positive_evidence(
    tmp_path,
):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class Filler:
        def fill(self, application_id, intent_id, prepared):
            raise AssertionError("submit must not replay fill")

    class Browser:
        def __init__(self):
            self.calls = []

        def submit_reviewed_application(self, application_id, exact_manifest):
            self.calls.append((application_id, exact_manifest.version))
            return WorkdaySubmissionCapture(
                application_id=application_id,
                manifest_version=exact_manifest.version,
                captured_at="2026-07-16T10:30:00+00:00",
                confirmation_page="Application submitted successfully",
                confirmation_marker=WorkdayConfirmationMarker.APPLICATION_SUBMITTED,
                confirmation_id="confirmation-001",
                ats_application_id="WORKDAY-42",
                ats_status="Submitted",
                email_receipt_id="gmail-message-99",
                email_receipt_received_at="2026-07-16T10:31:00+00:00",
            )

    browser = Browser()
    adapter = WorkdayApplicationAdapter(filler=Filler(), submission_browser=browser)

    outcome = adapter.submit("workday-001", manifest)

    assert browser.calls == [("workday-001", manifest.version)]
    assert outcome.status == "verified"
    assert outcome.evidence.verified_by == (
        SubmissionVerificationKind.CONFIRMATION_PAGE,
        SubmissionVerificationKind.CONFIRMATION_ID,
        SubmissionVerificationKind.ATS_SUBMITTED,
        SubmissionVerificationKind.EMAIL_RECEIPT,
    )


def test_confirmation_identifier_does_not_invent_ats_status(tmp_path):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class Browser:
        def submit_reviewed_application(self, application_id, exact_manifest):
            return WorkdaySubmissionCapture(
                application_id=application_id,
                manifest_version=exact_manifest.version,
                captured_at="2026-07-16T10:30:00+00:00",
                confirmation_page="Application received",
                confirmation_marker=WorkdayConfirmationMarker.APPLICATION_SUBMITTED,
                confirmation_id="confirmation-001",
            )

    adapter = WorkdayApplicationAdapter(
        filler=object(), submission_browser=Browser()
    )
    outcome = adapter.submit("workday-001", manifest)

    assert outcome.evidence.ats_application_id is None
    assert outcome.evidence.ats_status is None
    assert SubmissionVerificationKind.ATS_SUBMITTED not in (
        outcome.evidence.verified_by
    )


def test_verified_state_recovers_pending_package_without_second_external_submit(
    tmp_path,
):
    coordinator, store, vacancy, ats, package_writer = build_ready_application(tmp_path)

    class FailOnceWriter:
        def __init__(self):
            self.failed = False

        def write(self, application):
            if application.outcome is not None and not self.failed:
                self.failed = True
                raise RuntimeError("simulated package publication failure")
            return package_writer.write(application)

    coordinator._report_writer = FailOnceWriter()
    telegram = TelegramCommandHandler(coordinator)
    command = telegram.create_callback("workday-001", "Invia", actor="Synthetic Owner")

    with pytest.raises(RuntimeError, match="package publication failure"):
        telegram.handle_callback(command)

    pending = store.load("workday-001")
    assert pending.outcome.status == "verified"
    assert pending.package_publication_pending is True
    assert len(ats.submit_calls) == 1

    restarted = ApplicationWorkflowCoordinator(
        store=store,
        tailoring=FileTailoring(artifacts(tmp_path)),
        ats=ats,
        report_writer=package_writer,
        official_vacancies=vacancy,
        clock=FixedClock(),
    )
    recovered = restarted.get("workday-001")

    assert recovered.package_publication_pending is False
    assert recovered.outcome.status == "verified"
    assert len(ats.submit_calls) == 1
    packaged = json.loads(
        (package_writer.package_path("workday-001") / "application.json").read_text()
    )
    assert packaged["package_publication_pending"] is False
    assert packaged["outcome"]["status"] == "verified"


def test_submit_claim_package_is_recoverable_before_any_external_click(tmp_path):
    coordinator, store, _, ats, package_writer = build_ready_application(tmp_path)

    class FailClaimWriter:
        def write(self, application):
            if application.submission_intents and application.outcome is None:
                raise RuntimeError("simulated pre-submit package failure")
            return package_writer.write(application)

    coordinator._report_writer = FailClaimWriter()
    telegram = TelegramCommandHandler(coordinator)
    command = telegram.create_callback("workday-001", "Invia", actor="Synthetic Owner")

    with pytest.raises(RuntimeError, match="pre-submit package failure"):
        telegram.handle_callback(command)

    claimed = store.load("workday-001")
    assert claimed.package_publication_pending is True
    assert len(claimed.submission_intents) == 1
    assert ats.submit_calls == []

    restarted = ApplicationWorkflowCoordinator(
        store=store,
        tailoring=FileTailoring(artifacts(tmp_path)),
        ats=ats,
        report_writer=package_writer,
        official_vacancies=FixedVacancy(),
        clock=FixedClock(),
    )
    recovered = restarted.get("workday-001")

    assert recovered.package_publication_pending is False
    assert ats.submit_calls == []
    assert telegram.handle_callback(command).status == "replayed"


def test_live_workday_driver_clicks_submit_and_captures_confirmation_and_mail(
    tmp_path,
):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class SubmissionSession:
        def __init__(self):
            self.answers = dict(manifest.review_evidence.form_snapshot)
            self.attachment_hashes = dict(
                manifest.review_evidence.attachment_hashes
            )
            self.current_url = "https://example.wd3.myworkdayjobs.com/apply/42"
            self.clicks = 0
            self.intervention_kind = None

        def capture_submission_review(self, application_id):
            return ScopedWorkdayReview(
                application_id=application_id,
                filled_url="https://example.wd3.myworkdayjobs.com/apply/42",
                current_url=self.current_url,
                answers=dict(self.answers),
                attachment_hashes=dict(self.attachment_hashes),
                filled_attachment_names={
                    "cv": "cv.pdf",
                    "cover_letter": "cover-letter.pdf",
                },
                current_attachment_names={
                    "cv": "cv.pdf",
                    "cover_letter": "cover-letter.pdf",
                },
            )

        def submission_review_is_visible(self):
            return True

        def assert_pre_action_safe(self, guarded_action):
            assert guarded_action == "submit"
            if self.intervention_kind is not None:
                raise BrowserInterventionRequired(
                    kind=self.intervention_kind,
                    explanation="Resolve the guarded submit action",
                    browser_ready=True,
                )

        def click_submission(self):
            self.clicks += 1

        def capture_submission_confirmation(self):
            return WorkdayConfirmationCapture(
                page_text="Application submitted successfully",
                positive_marker=WorkdayConfirmationMarker.APPLICATION_SUBMITTED,
                confirmation_id="confirmation-001",
                ats_application_id="WORKDAY-42",
                ats_status="Submitted",
            )

    class Mailbox:
        def find_submission_receipt(self, application_id, confirmation_id):
            assert (application_id, confirmation_id) == (
                "workday-001",
                "confirmation-001",
            )
            return CareerMailboxReceipt(
                "gmail-message-99", "2026-07-16T10:31:00+00:00"
            )

    session = SubmissionSession()
    driver = LiveWorkdaySubmissionBrowser(
        now=FixedClock().now,
        mailbox=Mailbox(),
    )
    adapter = WorkdayApplicationAdapter(filler=session, submission_browser=driver)

    changed_field = next(iter(session.answers))
    expected_value = session.answers[changed_field]
    session.answers[changed_field] = "changed"
    with pytest.raises(RuntimeError, match="answers changed"):
        adapter.submit("workday-001", manifest)
    assert session.clicks == 0
    session.answers[changed_field] = expected_value

    changed_attachment = next(iter(session.attachment_hashes))
    expected_hash = session.attachment_hashes[changed_attachment]
    session.attachment_hashes[changed_attachment] = "sha256:changed"
    with pytest.raises(RuntimeError, match="attachments changed"):
        adapter.submit("workday-001", manifest)
    assert session.clicks == 0
    session.attachment_hashes[changed_attachment] = expected_hash

    session.current_url = "https://example.wd3.myworkdayjobs.com/apply/other"
    with pytest.raises(RuntimeError, match="page changed"):
        adapter.submit("workday-001", manifest)
    assert session.clicks == 0
    session.current_url = "https://example.wd3.myworkdayjobs.com/apply/42"

    session.intervention_kind = InterventionKind.CAPTCHA
    with pytest.raises(BrowserInterventionRequired):
        adapter.submit("workday-001", manifest)
    assert session.clicks == 0
    session.intervention_kind = None

    outcome = adapter.submit("workday-001", manifest)

    assert session.clicks == 1
    assert outcome.status == "verified"
    assert outcome.evidence.confirmation_id == "confirmation-001"
    assert outcome.evidence.ats_status == "Submitted"
    assert outcome.evidence.email_receipt_id == "gmail-message-99"


def test_arbitrary_confirmation_or_error_text_is_uncertain_without_typed_marker(
    tmp_path,
):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class Browser:
        def submit_reviewed_application(self, application_id, exact_manifest):
            return WorkdaySubmissionCapture(
                application_id=application_id,
                manifest_version=exact_manifest.version,
                captured_at="2026-07-16T10:45:00+00:00",
                confirmation_page="Error: your application was not submitted",
                confirmation_id="reference-visible-on-error-page",
                ats_application_id="WORKDAY-ERROR",
                ats_status="Error",
            )

        def validate_review(self, application_id, exact_manifest):
            return None

    outcome = WorkdayApplicationAdapter(
        filler=object(), submission_browser=Browser()
    ).submit("workday-001", manifest)

    assert outcome.status == "uncertain"
    assert outcome.recorded_at == "2026-07-16T10:45:00+00:00"
    assert outcome.evidence is None


def test_uncertain_submission_time_drives_indexes_instead_of_stale_lifecycle():
    row = _index_row(
        {
            "application_id": "app-1",
            "opportunity": {"company": "Example"},
            "lifecycle_state": "pronta da inviare",
            "history": [{"occurred_at": "2026-07-16T10:30:00+00:00"}],
            "outcome": {
                "status": "uncertain",
                "recorded_at": "2026-07-16T10:45:00+00:00",
                "evidence": None,
            },
        }
    )

    assert row["updated_at"] == "2026-07-16T10:45:00+00:00"


def test_production_package_report_exposes_intervention_and_uncertain_inspection(
    tmp_path,
):
    coordinator, _, _, _, writer = build_ready_application(tmp_path)
    ready = coordinator.get("workday-001")
    inspection = SubmissionInspection(
        status=SubmissionInspectionStatus.NO_POSITIVE_EVIDENCE,
        checked_at="2026-07-16T10:46:00+00:00",
        sources_checked=("ats", "career_mailbox"),
    )
    uncertain = replace(
        ready,
        outcome=SubmissionOutcome(
            "uncertain", recorded_at="2026-07-16T10:45:00+00:00"
        ),
        uncertain_submission=UncertainSubmissionRecord(
            version="uncertain-v1",
            manifest_version=ready.manifest.version,
            submission_intent_id="submit:token",
            inspection=inspection,
            resolution_token="resolution-token",
            actor="Synthetic Owner",
        ),
    )
    writer.write(uncertain)

    uncertain_report = (writer.package_path("workday-001") / "report.md").read_text()
    assert "- Status: uncertain" in uncertain_report
    assert "- Inspection: no_positive_evidence" in uncertain_report
    assert "- Sources checked: ats, career_mailbox" in uncertain_report
    assert "- Sources unavailable: none" in uncertain_report
    assert "- Automatic retry: forbidden" in uncertain_report
    assert "- Pending" not in uncertain_report

    intervention = replace(
        ready,
        application_id="intervention-app",
        intervention=InterventionRecord(
            intervention_id="intervention-v1",
            kind=InterventionKind.CAPTCHA,
            action=WorkflowAction.SUBMIT.value,
            explanation="Solve the CAPTCHA",
            detected_at="2026-07-16T10:47:00+00:00",
            browser_ready=True,
            resume_token="resume-token",
            actor="Synthetic Owner",
            continuation=InterventionContinuation(
                kind=InterventionContinuationKind.PENDING_AUTHORIZATION,
                reference="submit-token",
            ),
        ),
    )
    writer.write(intervention)

    intervention_report = (
        writer.package_path("intervention-app") / "report.md"
    ).read_text()
    assert "- Intervention: captcha" in intervention_report
    assert "- Guarded action: Invia" in intervention_report
    assert "- Browser ready: True" in intervention_report
    assert "- Explanation: Solve the CAPTCHA" in intervention_report


def test_production_package_preserves_freshness_capacity_and_prior_history(
    tmp_path,
):
    coordinator, _, _, _, writer = build_ready_application(tmp_path)
    ready = coordinator.get("workday-001")
    enriched = replace(
        ready,
        capacity_exception=PreparationCapacityException(
            kind=PreparationCapacityExceptionKind.DEADLINE,
            reason="Application closes before a normal slot opens",
            deadline_at="2026-07-18T12:00:00+00:00",
        ),
        preparation_reminders=(
            PreparationReminder(
                reminder_id="preparation-reminder:workday-001:expiry",
                application_id="workday-001",
                emitted_at="2026-07-18T10:30:00+00:00",
                preparation_expires_at="2026-07-19T10:30:00+00:00",
                priority="deadline",
                deadline_at="2026-07-18T12:00:00+00:00",
            ),
        ),
        prior_applications=(
            PriorApplicationEvidence(
                application_id="workday-prior",
                lifecycle_state="rifiutata",
                opportunity_version="opportunity-prior",
                recorded_at="2026-06-10T10:30:00+00:00",
                material_changes=("official_description",),
            ),
        ),
    )

    writer.write(enriched)

    package = writer.package_path("workday-001")
    report = (package / "report.md").read_text()
    audit = json.loads((package / "audit.json").read_text())
    assert "Capacity exception: deadline" in report
    assert "Preparation reminder: deadline" in report
    assert "Prior application: workday-prior" in report
    assert audit["capacity_exception"]["kind"] == "deadline"
    assert audit["preparation_reminders"][0]["priority"] == "deadline"
    assert audit["prior_applications"][0]["material_changes"] == [
        "official_description"
    ]


def test_workday_read_only_inspection_checks_ats_and_mailbox_without_clicking(
    tmp_path,
):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class Browser:
        def __init__(self):
            self.clicks = 0

        def inspect_submission_evidence(self, application_id, exact_manifest):
            return WorkdaySubmissionCapture(
                application_id=application_id,
                manifest_version=exact_manifest.version,
                captured_at="2026-07-16T10:46:00+00:00",
                sources_checked=("ats", "career_mailbox"),
                inspection_complete=True,
            )

        def submit_reviewed_application(self, application_id, exact_manifest):
            self.clicks += 1
            raise AssertionError("inspection must never submit")

        def validate_review(self, application_id, exact_manifest):
            return None

    browser = Browser()
    inspection = WorkdayApplicationAdapter(
        filler=object(), submission_browser=browser
    ).inspect_submission("workday-001", manifest)

    assert inspection.status == SubmissionInspectionStatus.NO_POSITIVE_EVIDENCE
    assert inspection.sources_checked == ("ats", "career_mailbox")
    assert inspection.evidence is None
    assert browser.clicks == 0


def test_workday_inspection_without_career_mailbox_cannot_unlock_retry(tmp_path):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class Browser:
        def inspect_submission_evidence(self, application_id, exact_manifest):
            return WorkdaySubmissionCapture(
                application_id=application_id,
                manifest_version=exact_manifest.version,
                captured_at="2026-07-16T10:46:00+00:00",
                sources_checked=("ats",),
                sources_unavailable=("career_mailbox",),
                inspection_complete=False,
            )

        def submit_reviewed_application(self, application_id, exact_manifest):
            raise AssertionError("inspection must never submit")

        def validate_review(self, application_id, exact_manifest):
            return None

    inspection = WorkdayApplicationAdapter(
        filler=object(), submission_browser=Browser()
    ).inspect_submission("workday-001", manifest)

    assert inspection.status == SubmissionInspectionStatus.INCOMPLETE
    assert inspection.sources_checked == ("ats",)
    assert inspection.sources_unavailable == ("career_mailbox",)
    assert inspection.permits_human_resolution is False


def test_workday_read_only_inspection_promotes_only_typed_positive_evidence(
    tmp_path,
):
    coordinator, _, _, _, _ = build_ready_application(tmp_path)
    manifest = coordinator.get("workday-001").manifest

    class Browser:
        def inspect_submission_evidence(self, application_id, exact_manifest):
            return WorkdaySubmissionCapture(
                application_id=application_id,
                manifest_version=exact_manifest.version,
                captured_at="2026-07-16T10:46:00+00:00",
                email_receipt_id="gmail-receipt-42",
                email_receipt_received_at="2026-07-16T10:45:00+00:00",
                sources_checked=("ats", "career_mailbox"),
                inspection_complete=True,
            )

        def submit_reviewed_application(self, application_id, exact_manifest):
            raise AssertionError("inspection must never submit")

        def validate_review(self, application_id, exact_manifest):
            return None

    inspection = WorkdayApplicationAdapter(
        filler=object(), submission_browser=Browser()
    ).inspect_submission("workday-001", manifest)

    assert inspection.status == SubmissionInspectionStatus.VERIFIED
    assert inspection.evidence.email_receipt_id == "gmail-receipt-42"
