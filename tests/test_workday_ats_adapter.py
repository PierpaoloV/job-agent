from datetime import datetime, timezone
import hashlib
import itertools
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import AnswerVisibility, PreSubmitManifest  # noqa: E402
from application_interventions import (  # noqa: E402
    BrowserInterventionRequired,
    InterventionKind,
)
from application_telegram import TelegramCommandHandler  # noqa: E402
from application_workflow import (  # noqa: E402
    ApplicationWorkflowCoordinator,
    JsonApplicationStore,
    MarkdownApplicationReportWriter,
    OfficialVacancy,
    PreparedArtifacts,
    WorkflowAction,
)
from ats_answer_service import (  # noqa: E402
    AnswerScope,
    ApplicationFieldReference,
    AtsQuestion,
    LocalAnswerProfile,
    LocalAtsAnswerService,
    QuestionMeaning,
)
from ats_answer_storage import LocalAnswerVault  # noqa: E402
from ats_fill_receipts import JsonAtsFillReceiptStore  # noqa: E402
from macos_keychain import MacOSKeychainCredentialStore  # noqa: E402
from workday_html_driver import OfflineWorkdayHtmlDriver  # noqa: E402
from workday_submission import (  # noqa: E402
    LiveWorkdaySubmissionBrowser,
    WorkdayApplicationAdapter,
    WorkdayConfirmationCapture,
    WorkdayConfirmationMarker,
)
from workday_ats import (  # noqa: E402
    AtsControlKind,
    AtsDocumentSlot,
    AtsField,
    AtsFieldKind,
    BrowserPage,
    BrowserReviewSnapshot,
    DedicatedCareerAccount,
    ManualFieldIntervention,
    WorkdayInspection,
    WorkdayAtsAdapter,
)


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


class FixtureWorkdayBrowser:
    """Deterministic browser contract fixture. It has deliberately no submit method."""

    def __init__(
        self,
        *,
        fields=(),
        slots=(),
        account_required=True,
        review_page=BrowserPage.REVIEW,
        inspection_page=BrowserPage.APPLICATION,
    ):
        self.fields = tuple(fields)
        self.slots = tuple(slots)
        self.account_required = account_required
        self.review_page = review_page
        self.current_page = inspection_page
        self.inspect_calls = []
        self.events = []
        self.answers = {}
        self.attachments = {}
        self.intervention_kind = None

    def inspect_application(self, application_id, profile):
        self.inspect_calls.append((application_id, profile))
        return WorkdayInspection(
            self.fields,
            self.slots,
            self.account_required,
            self.current_page,
        )

    def ensure_account(self, email, password):
        self.events.append(("account", email, password))

    def fill_field(self, field_id, value):
        self.events.append(("field", field_id, value))
        self.answers[field_id] = value

    def upload_document(self, field_id, path):
        self.events.append(("upload", field_id, str(path)))
        self.attachments[field_id] = pathlib.Path(path)

    def advance_to_review(self):
        self.events.append(("review",))
        self.current_page = self.review_page

    def capture_review(self):
        return BrowserReviewSnapshot(
            page=self.current_page,
            answers=dict(self.answers),
            attachment_hashes={
                field_id: sha256(path)
                for field_id, path in self.attachments.items()
            },
            attachment_names={
                field_id: path.name for field_id, path in self.attachments.items()
            },
        )

    def current_url(self):
        return "https://example.wd3.myworkdayjobs.com/apply/fixture"

    def detect_intervention(self, guarded_action):
        return self.intervention_kind


class MemoryKeychain:
    def __init__(self):
        self.values = {}
        self.stores = []

    def get(self, service, account):
        return self.values.get((service, account))

    def store(self, service, account, password):
        self.stores.append((service, account, password))
        self.values[(service, account)] = password


class CapturingAnswerRequests:
    def __init__(self):
        self.questions = []

    def request(self, question):
        self.questions.append(question)


class FileTailoring:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def prepare(self, application_id, intent_id, opportunity, official_vacancy):
        return self.artifacts

    def reload_master_cv(self):
        return "evidence-v1"

    def verify_artifacts(self, artifacts):
        return sha256(pathlib.Path(artifacts.cv_path)) == artifacts.cv_hash and sha256(
            pathlib.Path(artifacts.cover_letter_path)
        ) == artifacts.cover_letter_hash


class FixedVacancy:
    def __init__(self):
        self.current = OfficialVacancy(
            version="vacancy-v1",
            fingerprint="sha256:vacancy",
            freshness="2026-07-16T10:30:00+00:00",
            description="Build trustworthy AI systems.",
        )

    def retrieve(self, opportunity):
        return self.current

    def revalidate(self, opportunity, previous):
        return self.current


class NoopSubmitAts:
    """Makes the test fail loudly if it accidentally crosses the ticket boundary."""

    def __init__(self, filler):
        self.filler = filler

    def fill(self, application_id, intent_id, artifacts):
        return self.filler.fill(application_id, intent_id, artifacts)

    def submit(self, application_id, manifest):
        raise AssertionError("Compila must never activate final submit")


def build_answer_service(tmp_path, *, standardized_defaults=None):
    vault = LocalAnswerVault(tmp_path / "private" / "answers.json")
    profile = LocalAnswerProfile(
        standardized_defaults=standardized_defaults or {},
        protected_terms=("SENSITIVE_HEALTH_MARKER",),
    )
    profile.save(vault)
    request_ids = itertools.count(1)
    return LocalAtsAnswerService(
        vault=vault,
        profile=profile,
        now=FixedClock().now,
        request_id_factory=lambda: f"answer-request-{next(request_ids)}",
    )


def save_default(answer_service, *, prompt, value):
    question = AtsQuestion(
        field=ApplicationFieldReference("profile", "seed"),
        prompt=prompt,
        mandatory=True,
    )
    request = answer_service.resolve(question)
    answer_service.answer(request.request_id, value, AnswerScope.DEFAULT)


def receipt_store(tmp_path):
    return JsonAtsFillReceiptStore(tmp_path / "private" / "fill-receipts")


def workday_html_fixture(tmp_path):
    html = tmp_path / "workday-application.html"
    html.write_text(
        """
        <main data-automation-id="workday-application" data-provider="workday">
          <section data-automation-id="account-creation"></section>
          <label data-automation-id="formField" data-field-id="first-name"
                 data-prompt="First name" data-required="true"
                 data-control-kind="text"></label>
          <label data-automation-id="formField" data-field-id="disability"
                 data-prompt="Voluntary disability self-identification"
                 data-required="false" data-control-kind="radio"
                 data-meaning="demographic.disability_status"
                 data-standardized-voluntary="true"></label>
          <input data-automation-id="file-upload" data-field-id="resume"
                 data-document-kind="cv" data-required="true" />
          <input data-automation-id="file-upload" data-field-id="cover-letter"
                 data-document-kind="cover_letter" data-required="false" />
        </main>
        """,
        encoding="utf-8",
    )
    return html


def prepared_artifacts(tmp_path):
    cv = tmp_path / "cv-v1.pdf"
    cover = tmp_path / "cover-v1.pdf"
    cv.write_bytes(b"exact cv version one")
    cover.write_bytes(b"exact cover version one")
    return PreparedArtifacts(
        version="artifacts-v1",
        cv_path=str(cv),
        cover_letter_path=str(cover),
        cv_hash=sha256(cv),
        cover_letter_hash=sha256(cover),
    )


def test_compila_runs_one_supported_workday_journey_and_stops_at_review(tmp_path):
    artifacts = prepared_artifacts(tmp_path)
    driver = OfflineWorkdayHtmlDriver(workday_html_fixture(tmp_path))
    keychain = MemoryKeychain()
    answer_requests = CapturingAnswerRequests()
    answer_service = build_answer_service(
        tmp_path,
        standardized_defaults={QuestionMeaning.DISABILITY_STATUS: "Yes"},
    )
    save_default(answer_service, prompt="First name", value="Synthetic Owner")
    receipts = receipt_store(tmp_path)
    filler = WorkdayAtsAdapter(
        browser=driver,
        answer_service=answer_service,
        answer_requests=answer_requests,
        keychain=keychain,
        account=DedicatedCareerAccount(
            email="career.user@gmail.com",
            keychain_service="job-agent.workday",
        ),
        receipts=receipts,
        password_factory=lambda: "generated-test-password",
    )
    coordinator = ApplicationWorkflowCoordinator(
        store=JsonApplicationStore(tmp_path / "state"),
        tailoring=FileTailoring(artifacts),
        ats=NoopSubmitAts(filler),
        report_writer=MarkdownApplicationReportWriter(tmp_path / "reports"),
        official_vacancies=FixedVacancy(),
        clock=FixedClock(),
        token_factory=iter(("prepare-token", "fill-token")).__next__,
    )
    coordinator.propose(
        application_id="workday-001",
        opportunity={"company": "Example", "title": "AI Scientist"},
        version="opportunity-v1",
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "workday-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    )

    assert driver.opened_application is None

    result = coordinator.handle(
        coordinator.issue_authorization(
            "workday-001", WorkflowAction.FILL, actor="Synthetic Owner"
        )
    )

    assert result.status == "completed"
    assert driver.opened_application == ("workday-001", "Job Applications")
    assert driver.account_email == "career.user@gmail.com"
    review = driver.capture_review()
    assert review.page == BrowserPage.REVIEW
    assert review.answers == {"first-name": "Synthetic Owner", "disability": "Yes"}
    assert review.attachment_hashes == {
        "resume": artifacts.cv_hash,
        "cover-letter": artifacts.cover_letter_hash,
    }
    assert keychain.stores == [
        (
            "job-agent.workday",
            "career.user@gmail.com",
            "generated-test-password",
        )
    ]
    assert answer_requests.questions == []
    snapshot = coordinator.get("workday-001")
    assert snapshot.lifecycle_state == "pronta da inviare"
    assert snapshot.manifest.artifact_hashes == {
        "cv": artifacts.cv_hash,
        "cover_letter": artifacts.cover_letter_hash,
    }
    assert snapshot.manifest.form_snapshot_hash.startswith("sha256:")
    assert snapshot.manifest.review_page == "review"
    submission_review = filler.capture_submission_review("workday-001")
    assert submission_review.application_id == "workday-001"
    assert submission_review.current_url == submission_review.filled_url
    assert submission_review.answers == review.answers
    assert submission_review.attachment_hashes == snapshot.manifest.artifact_hashes
    assert submission_review.filled_attachment_names == {
        "cv": "cv-v1.pdf",
        "cover_letter": "cover-v1.pdf",
    }
    assert (
        submission_review.current_attachment_names
        == submission_review.filled_attachment_names
    )

    restarted = WorkdayAtsAdapter(
        browser=driver,
        answer_service=answer_service,
        answer_requests=answer_requests,
        keychain=keychain,
        account=DedicatedCareerAccount(
            email="career.user@gmail.com",
            keychain_service="job-agent.workday",
        ),
        receipts=receipts,
        password_factory=lambda: "unused-after-restart",
    )
    completed = restarted.fill(
        "workday-001", "fill:fill-token", artifacts
    )
    assert completed.artifact_version == artifacts.version
    assert restarted.capture_submission_review("workday-001") == submission_review


def test_unknown_mandatory_field_returns_to_local_answer_workflow_before_mutation(
    tmp_path,
):
    artifacts = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        fields=(AtsField("notice", "What is your notice period?", True),),
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
    )
    requests = CapturingAnswerRequests()
    answer_service = build_answer_service(tmp_path)
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=answer_service,
        answer_requests=requests,
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(
            email="career.user@gmail.com",
            keychain_service="job-agent.workday",
        ),
        receipts=receipt_store(tmp_path),
        password_factory=lambda: "unused-password",
    )

    with pytest.raises(RuntimeError, match="mandatory ATS answer is required"):
        filler.fill("workday-001", "fill:token", artifacts)

    assert len(requests.questions) == 1
    assert requests.questions[0].field.field_id == "notice"
    assert requests.questions[0].actions == (
        "Usa solo qui",
        "Salva come default",
    )
    assert browser.events == []

    answer_service.answer(
        requests.questions[0].request_id,
        "One month",
        AnswerScope.ONE_USE,
    )
    filled = filler.fill("workday-001", "fill:token", artifacts)

    assert filled.answers == {"notice": "One month"}
    assert browser.events[-1] == ("review",)
    assert len(requests.questions) == 1


def test_trusted_workday_meaning_marks_only_principal_answer_for_summary(tmp_path):
    artifacts = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        fields=(
            AtsField(
                "authorization",
                "Are you authorized to work here?",
                True,
                meaning=QuestionMeaning.WORK_AUTHORIZATION,
            ),
            AtsField(
                "health-detail",
                "Optional health detail",
                False,
                meaning=QuestionMeaning.DISABILITY_STATUS,
                standardized_voluntary=True,
            ),
        ),
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
    )
    requests = CapturingAnswerRequests()
    answer_service = build_answer_service(
        tmp_path,
        standardized_defaults={QuestionMeaning.DISABILITY_STATUS: "Prefer not to say"},
    )
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=answer_service,
        answer_requests=requests,
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(RuntimeError, match="mandatory ATS answer is required"):
        filler.fill("workday-001", "fill:token", artifacts)
    answer_service.answer(
        requests.questions[0].request_id,
        "SYNTHETIC-WORK-AUTHORIZATION",
        AnswerScope.ONE_USE,
    )

    filled = filler.fill("workday-001", "fill:token", artifacts)

    assert filled.answer_disclosures[0].field_id == "authorization"
    assert filled.answer_disclosures[0].visibility == AnswerVisibility.PUBLIC_SUMMARY
    assert filled.answer_disclosures[1].field_id == "health-detail"
    assert filled.answer_disclosures[1].visibility == AnswerVisibility.LOCAL_ONLY


def test_same_fill_intent_reuses_completed_review_without_browser_replay(tmp_path):
    artifacts = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        fields=(AtsField("first-name", "First name", True),),
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
        account_required=False,
    )
    answer_service = build_answer_service(tmp_path)
    save_default(answer_service, prompt="First name", value="Synthetic Owner")
    receipts = receipt_store(tmp_path)
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=answer_service,
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipts,
    )

    first = filler.fill("workday-001", "fill:token", artifacts)
    events = list(browser.events)
    restarted = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipts,
    )
    second = restarted.fill("workday-001", "fill:token", artifacts)

    assert second == first
    assert browser.events == events


def test_cv_only_review_and_manifest_do_not_fabricate_cover_letter_attachment(
    tmp_path,
):
    prepared = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
        account_required=False,
    )
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    filled = filler.fill("workday-001", "fill:token", prepared)
    manifest = PreSubmitManifest.build(
        application_id="workday-001",
        opportunity_version="opportunity-v1",
        official_vacancy=FixedVacancy().current,
        artifacts=prepared,
        filled=filled,
    )

    assert filled.review_evidence.attachment_hashes == {"cv": prepared.cv_hash}
    assert manifest.artifact_hashes == {"cv": prepared.cv_hash}
    assert filler.capture_submission_review("workday-001").attachment_hashes == {
        "cv": prepared.cv_hash
    }


def test_ready_workday_application_restores_durable_fill_scope_after_restart(
    tmp_path,
):
    prepared = prepared_artifacts(tmp_path)
    receipts_root = tmp_path / "private" / "fill-receipts"

    class RestartableBrowser(FixtureWorkdayBrowser):
        def __init__(self, *, on_review=False):
            super().__init__(
                slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
                account_required=False,
                inspection_page=(
                    BrowserPage.REVIEW if on_review else BrowserPage.APPLICATION
                ),
            )
            if on_review:
                self.attachments = {"resume": pathlib.Path(prepared.cv_path)}
            self.clicks = 0

        def click_submission(self):
            self.clicks += 1

        def capture_submission_confirmation(self):
            return WorkdayConfirmationCapture(
                page_text="Application submitted successfully",
                positive_marker=WorkdayConfirmationMarker.APPLICATION_SUBMITTED,
                confirmation_id="confirmation-after-restart",
            )

    def application_adapter(browser):
        filler = WorkdayAtsAdapter(
            browser=browser,
            answer_service=build_answer_service(tmp_path),
            answer_requests=CapturingAnswerRequests(),
            keychain=MemoryKeychain(),
            account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
            receipts=JsonAtsFillReceiptStore(receipts_root),
        )
        return WorkdayApplicationAdapter(
            filler=filler,
            submission_browser=LiveWorkdaySubmissionBrowser(now=FixedClock().now),
        )

    state_root = tmp_path / "state"
    first_browser = RestartableBrowser()
    first = ApplicationWorkflowCoordinator(
        store=JsonApplicationStore(state_root),
        tailoring=FileTailoring(prepared),
        ats=application_adapter(first_browser),
        report_writer=MarkdownApplicationReportWriter(tmp_path / "reports"),
        official_vacancies=FixedVacancy(),
        clock=FixedClock(),
        token_factory=iter(("prepare", "fill", "submit")).__next__,
    )
    first.propose(
        application_id="workday-001",
        opportunity={"company": "Example", "title": "Researcher"},
        version="opportunity-v1",
    )
    first.handle(
        first.issue_authorization(
            "workday-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    )
    first.handle(
        first.issue_authorization(
            "workday-001", WorkflowAction.FILL, actor="Synthetic Owner"
        )
    )
    summary, submit = TelegramCommandHandler(first).present_submit(
        "workday-001", actor="Synthetic Owner"
    )
    assert [(item.kind, item.sha256) for item in summary.attachments] == [
        ("cv", prepared.cv_hash)
    ]

    restarted_browser = RestartableBrowser(on_review=True)
    restarted = ApplicationWorkflowCoordinator(
        store=JsonApplicationStore(state_root),
        tailoring=FileTailoring(prepared),
        ats=application_adapter(restarted_browser),
        report_writer=MarkdownApplicationReportWriter(tmp_path / "reports"),
        official_vacancies=FixedVacancy(),
        clock=FixedClock(),
    )

    result = restarted.handle(submit)

    assert result.status == "completed"
    assert restarted_browser.clicks == 1
    snapshot = restarted.get("workday-001")
    assert snapshot.lifecycle_state == "inviata"
    assert snapshot.manifest.artifact_hashes == {"cv": prepared.cv_hash}


def test_fill_receipt_fsyncs_directory_after_atomic_replace(tmp_path, monkeypatch):
    import os
    import stat
    import ats_fill_receipts

    prepared = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
        account_required=False,
    )
    store = receipt_store(tmp_path)
    directory_fsyncs = []
    real_fsync = ats_fill_receipts.os.fsync

    def recording_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr(ats_fill_receipts.os, "fsync", recording_fsync)
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=store,
    )

    filler.fill("workday-001", "fill:token", prepared)

    assert directory_fsyncs


def test_restart_on_existing_review_captures_receipt_without_replaying_fill(tmp_path):
    artifacts = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        fields=(AtsField("first-name", "First name", True),),
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
        account_required=False,
        inspection_page=BrowserPage.REVIEW,
    )
    browser.answers = {"first-name": "Synthetic Owner"}
    browser.attachments = {"resume": pathlib.Path(artifacts.cv_path)}
    answer_service = build_answer_service(tmp_path)
    save_default(answer_service, prompt="First name", value="Synthetic Owner")
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=answer_service,
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    result = filler.fill("workday-001", "fill:token", artifacts)

    assert result.review_evidence.page == "review"
    assert browser.events == []


def test_exact_artifact_hash_mismatch_prevents_upload_and_account_creation(tmp_path):
    artifacts = prepared_artifacts(tmp_path)
    pathlib.Path(artifacts.cv_path).write_bytes(b"tampered after preparation")
    browser = FixtureWorkdayBrowser(
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),)
    )
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(
            email="career.user@gmail.com",
            keychain_service="job-agent.workday",
        ),
        receipts=receipt_store(tmp_path),
        password_factory=lambda: "unused-password",
    )

    with pytest.raises(ValueError, match="artifact hash mismatch"):
        filler.fill("workday-001", "fill:token", artifacts)

    assert browser.events == []


def test_non_review_page_is_rejected_without_producing_a_fill_candidate(tmp_path):
    artifacts = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
        account_required=False,
        review_page=BrowserPage.APPLICATION,
    )
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(RuntimeError, match="did not stop on the review page"):
        filler.fill("workday-001", "fill:token", artifacts)


def test_unsupported_mandatory_control_routes_to_intervention_before_mutation(
    tmp_path,
):
    artifacts = prepared_artifacts(tmp_path)
    browser = FixtureWorkdayBrowser(
        fields=(
            AtsField(
                "custom-widget",
                "Complete the employer-specific declaration",
                True,
                control_kind=AtsControlKind.UNSUPPORTED,
            ),
        ),
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, required=True),),
    )
    requests = CapturingAnswerRequests()
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=requests,
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(BrowserInterventionRequired) as blocked:
        filler.fill("workday-001", "fill:token", artifacts)

    assert blocked.value.kind == InterventionKind.UNSUPPORTED_CONTROL
    assert blocked.value.browser_ready is True
    assert blocked.value.guarded_action_started is False
    assert filler.intervention_is_resolved("workday-001", blocked.value) is False
    browser.fields = ()
    assert filler.intervention_is_resolved("workday-001", blocked.value) is True
    assert requests.questions == [
        ManualFieldIntervention(
            application_id="workday-001",
            field_id="custom-widget",
            prompt="Complete the employer-specific declaration",
        )
    ]
    assert browser.events == []


def test_unknown_html_control_kind_routes_to_local_intervention(tmp_path):
    html = tmp_path / "unknown-workday-control.html"
    html.write_text(
        """
        <main data-automation-id="workday-application" data-provider="workday">
          <label data-automation-id="formField" data-field-id="availability"
                 data-prompt="Choose exact availability dates"
                 data-required="true" data-control-kind="date-range-picker"></label>
          <input data-automation-id="file-upload" data-field-id="resume"
                 data-document-kind="cv" data-required="true" />
        </main>
        """,
        encoding="utf-8",
    )
    browser = OfflineWorkdayHtmlDriver(html)
    requests = CapturingAnswerRequests()
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=requests,
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(BrowserInterventionRequired) as blocked:
        filler.fill("workday-001", "fill:token", prepared_artifacts(tmp_path))

    assert blocked.value.kind == InterventionKind.UNSUPPORTED_CONTROL
    assert requests.questions == [
        ManualFieldIntervention(
            application_id="workday-001",
            field_id="availability",
            prompt="Choose exact availability dates",
        )
    ]


@pytest.mark.parametrize(
    ("automation_id", "kind"),
    (
        ("captcha", InterventionKind.CAPTCHA),
        ("nonEmailMfa", InterventionKind.NON_EMAIL_MFA),
        ("unusualConsent", InterventionKind.UNUSUAL_CONSENT),
        ("siteRestriction", InterventionKind.SITE_RESTRICTION),
    ),
)
def test_concrete_workday_guards_pause_before_any_fill_mutation(
    tmp_path, automation_id, kind
):
    html = tmp_path / f"guarded-{automation_id}.html"
    html.write_text(
        f"""
        <main data-automation-id="workday-application" data-provider="workday">
          <section data-automation-id="account-creation"></section>
          <section data-automation-id="{automation_id}"></section>
          <input data-automation-id="file-upload" data-field-id="resume"
                 data-document-kind="cv" data-required="true" />
        </main>
        """,
        encoding="utf-8",
    )
    browser = OfflineWorkdayHtmlDriver(html)
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(BrowserInterventionRequired) as blocked:
        filler.fill("workday-001", "fill:token", prepared_artifacts(tmp_path))

    assert blocked.value.kind == kind
    assert blocked.value.guarded_action_started is False
    assert browser.account_email is None
    assert browser.capture_review().attachment_hashes == {}


def test_workday_reprobes_before_each_consequential_fill_mutation(tmp_path):
    class GuardAppearsAfterInitialProbe(FixtureWorkdayBrowser):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.probes = 0

        def detect_intervention(self, guarded_action):
            self.probes += 1
            return (
                None
                if self.probes == 1
                else InterventionKind.CAPTCHA
            )

    browser = GuardAppearsAfterInitialProbe(
        fields=(),
        slots=(AtsDocumentSlot("resume", AtsFieldKind.CV, True),),
        account_required=True,
    )
    filler = WorkdayAtsAdapter(
        browser=browser,
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(BrowserInterventionRequired) as blocked:
        filler.fill("workday-001", "fill:token", prepared_artifacts(tmp_path))

    assert blocked.value.kind == InterventionKind.CAPTCHA
    assert browser.probes == 2
    assert browser.events == []


def test_workday_site_restriction_page_without_application_form_is_an_intervention(
    tmp_path,
):
    html = tmp_path / "site-restricted.html"
    html.write_text(
        '<main data-automation-id="siteRestriction">Applications unavailable</main>',
        encoding="utf-8",
    )
    filler = WorkdayAtsAdapter(
        browser=OfflineWorkdayHtmlDriver(html),
        answer_service=build_answer_service(tmp_path),
        answer_requests=CapturingAnswerRequests(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=receipt_store(tmp_path),
    )

    with pytest.raises(BrowserInterventionRequired) as blocked:
        filler.fill("workday-001", "fill:token", prepared_artifacts(tmp_path))

    assert blocked.value.kind == InterventionKind.SITE_RESTRICTION
    assert blocked.value.guarded_action_started is False


def test_macos_keychain_adapter_uses_argument_vector_and_never_shell(tmp_path):
    calls = []

    def runner(arguments):
        calls.append(tuple(arguments))
        if "find-generic-password" in arguments:
            return None
        return ""

    keychain = MacOSKeychainCredentialStore(command_runner=runner)

    assert keychain.get("job-agent.workday", "career@example.com") is None
    keychain.store("job-agent.workday", "career@example.com", "p@ss word")

    assert calls == [
        (
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "job-agent.workday",
            "-a",
            "career@example.com",
            "-w",
        ),
        (
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            "job-agent.workday",
            "-a",
            "career@example.com",
            "-w",
            "p@ss word",
        ),
    ]
