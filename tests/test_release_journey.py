from datetime import datetime, timezone
import csv
import hashlib
import itertools
import json
from pathlib import Path
import socket
import stat
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import OfficialVacancy, PreparedArtifacts  # noqa: E402
from application_packages import LocalApplicationPackageWriter  # noqa: E402
from application_storage import JsonApplicationStore  # noqa: E402
from application_telegram import TelegramCommandHandler  # noqa: E402
from application_workflow import ApplicationWorkflowCoordinator  # noqa: E402
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
from career_correspondence import CareerCorrespondenceMonitor  # noqa: E402
from career_correspondence_domain import (  # noqa: E402
    CareerMailboxConnection,
    CareerMessage,
    MailboxPollStatus,
    SenderKind,
)
from career_correspondence_store import JsonCareerCorrespondenceStore  # noqa: E402
from pdf_artifact_renderer import LocalPdfArtifactRenderer  # noqa: E402
from workday_ats import (  # noqa: E402
    DedicatedCareerAccount,
    WorkdayAtsAdapter,
)
from workday_html_driver import OfflineWorkdayHtmlDriver  # noqa: E402
from workday_submission import (  # noqa: E402
    LiveWorkdaySubmissionBrowser,
    WorkdayApplicationAdapter,
    WorkdayConfirmationCapture,
    WorkdayConfirmationMarker,
)


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current


class FixedOfficialVacancy:
    def __init__(self) -> None:
        self.current = OfficialVacancy(
            version="vacancy-v1",
            fingerprint="sha256:official-release-role",
            freshness="2026-07-22T10:00:00+00:00",
            description="Build safe applied AI systems for Example Research.",
        )

    def retrieve(self, opportunity):
        return self.current

    def revalidate(self, opportunity, previous):
        return self.current


class LocallyRenderedTailoring:
    """Deterministic stand-in for the model boundary; rendering stays production."""

    def __init__(self, renderer: LocalPdfArtifactRenderer) -> None:
        self.renderer = renderer

    def prepare(self, application_id, intent_id, opportunity, official_vacancy):
        rendered = self.renderer.render(
            application_id=application_id,
            bundle_version="generation-v1",
            cv_text=(
                "Synthetic Candidate\n"
                "Applied AI researcher building reliable production systems."
            ),
            cover_letter_text=(
                "Dear hiring team,\n"
                "I am enthusiastic about the AI Scientist role in Zurich."
            ),
        )
        published = self.renderer.publish(
            application_id=application_id,
            bundle_version="bundle-v1",
            rendered=rendered,
        )
        return PreparedArtifacts(
            version="bundle-v1",
            cv_path=published.cv_path,
            cover_letter_path=published.cover_letter_path,
            cv_hash=published.cv_hash,
            cover_letter_hash=published.cover_letter_hash,
            evidence_source_version="master-cv-v1",
        )

    def reload_master_cv(self):
        return "master-cv-v1"

    def verify_artifacts(self, artifacts):
        return all(
            _file_hash(Path(path)) == expected
            for path, expected in (
                (artifacts.cv_path, artifacts.cv_hash),
                (artifacts.cover_letter_path, artifacts.cover_letter_hash),
            )
        )


class MemoryKeychain:
    def __init__(self) -> None:
        self.values = {}

    def get(self, service, account):
        return self.values.get((service, account))

    def store(self, service, account, password):
        self.values[(service, account)] = password


class UnexpectedAnswerRequest:
    def request(self, question):
        raise AssertionError(f"Synthetic journey lacked a local answer: {question}")


class ConfirmedOfflineWorkdayDriver(OfflineWorkdayHtmlDriver):
    """Local HTML target whose final click produces local typed evidence only."""

    def __init__(self, html_path: Path) -> None:
        super().__init__(html_path)
        self.local_submit_clicks = 0

    def click_submission(self) -> None:
        self.local_submit_clicks += 1

    def capture_submission_confirmation(self) -> WorkdayConfirmationCapture:
        if self.local_submit_clicks != 1:
            raise RuntimeError("Local confirmation is not scoped to one submit click")
        return WorkdayConfirmationCapture(
            page_text="Application submitted successfully (local fixture)",
            positive_marker=WorkdayConfirmationMarker.APPLICATION_SUBMITTED,
            confirmation_id="LOCAL-CONFIRMATION-42",
            ats_application_id="LOCAL-WORKDAY-42",
            ats_status="Submitted",
        )


class CapturingTelegram:
    def __init__(self) -> None:
        self.pre_submit = []
        self.statuses = []

    def send_pre_submit(self, summary, command):
        self.pre_submit.append((summary, command))

    def send_status(self, message):
        self.statuses.append(message)


class LocalReadOnlyCareerMailbox:
    """The release journey can read correspondence but has no send capability."""

    def __init__(self, messages) -> None:
        self.messages = tuple(messages)
        self.fetch_calls = []

    def fetch(self, *, account_address):
        self.fetch_calls.append(account_address)
        return self.messages


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _workday_fixture(path: Path) -> Path:
    path.write_text(
        """
        <main data-automation-id="workday-application" data-provider="workday">
          <section data-automation-id="account-creation"></section>
          <label data-automation-id="formField" data-field-id="work-auth"
                 data-prompt="Are you authorized to work in Switzerland?"
                 data-required="true" data-control-kind="radio"
                 data-meaning="eligibility.work_authorization"></label>
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
    return path


def _answer_service(root: Path, clock: MutableClock) -> LocalAtsAnswerService:
    vault = LocalAnswerVault(root / "answers.json")
    profile = LocalAnswerProfile(
        standardized_defaults={
            QuestionMeaning.DISABILITY_STATUS: "Prefer not to answer",
        },
        protected_terms=("SENSITIVE_HEALTH_MARKER",),
    )
    profile.save(vault)
    identifiers = itertools.count(1)
    service = LocalAtsAnswerService(
        vault=vault,
        profile=profile,
        now=clock.now,
        request_id_factory=lambda: f"answer-request-{next(identifiers)}",
    )
    seed = service.resolve(
        AtsQuestion(
            field=ApplicationFieldReference("profile", "work-auth"),
            prompt="Are you authorized to work in Switzerland?",
            mandatory=True,
            meaning=QuestionMeaning.WORK_AUTHORIZATION,
        )
    )
    service.answer(seed.request_id, "SYNTHETIC-WORK-AUTHORIZATION", AnswerScope.DEFAULT)
    return service


def test_complete_synthetic_release_journey_stays_local_and_auditable(
    tmp_path, monkeypatch
):
    network_attempts = []

    def forbid_network(*args, **kwargs):
        network_attempts.append((args, kwargs))
        raise AssertionError("Synthetic release journey attempted real network access")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(socket.socket, "connect", forbid_network)

    clock = MutableClock()
    application_id = "release-workday-001"
    driver = ConfirmedOfflineWorkdayDriver(
        _workday_fixture(tmp_path / "workday-application.html")
    )
    answers_root = tmp_path / "private" / "answers"
    filler = WorkdayAtsAdapter(
        browser=driver,
        answer_service=_answer_service(answers_root, clock),
        answer_requests=UnexpectedAnswerRequest(),
        keychain=MemoryKeychain(),
        account=DedicatedCareerAccount(email="alex.jobs@gmail.com"),
        receipts=JsonAtsFillReceiptStore(tmp_path / "private" / "fill-receipts"),
        password_factory=lambda: "fixture-password-never-leaves-this-test",
    )
    ats = WorkdayApplicationAdapter(
        filler=filler,
        submission_browser=LiveWorkdaySubmissionBrowser(now=clock.now),
    )
    package_writer = LocalApplicationPackageWriter(
        tmp_path / "private" / "applications"
    )
    coordinator = ApplicationWorkflowCoordinator(
        store=JsonApplicationStore(tmp_path / "private" / "state"),
        tailoring=LocallyRenderedTailoring(
            LocalPdfArtifactRenderer(tmp_path / "private" / "artifacts")
        ),
        ats=ats,
        report_writer=package_writer,
        official_vacancies=FixedOfficialVacancy(),
        clock=clock,
        token_factory=iter(
            ("prepare-release-token", "fill-release-token", "submit-release-token")
        ).__next__,
    )
    coordinator.propose(
        application_id=application_id,
        opportunity={
            "stable_id": "example:ai-scientist:req-42",
            "official_job_id": "REQ-42",
            "company": "Example Research",
            "title": "AI Scientist",
            "location": "Zurich",
            "official_url": "https://careers.example.com/jobs/REQ-42",
            "trusted_correspondence_domains": ["example.com"],
        },
        version="opportunity-v1",
    )
    transport = CapturingTelegram()
    telegram = TelegramCommandHandler(coordinator, transport=transport)

    prepare = telegram.create_callback(
        application_id, "Prepara candidatura", actor="Synthetic Owner"
    )
    assert telegram.handle_callback_data(telegram.encode_callback(prepare)).status == (
        "completed"
    )
    prepared = coordinator.get(application_id)
    assert Path(prepared.artifacts.cv_path).read_bytes().startswith(b"%PDF-")
    assert Path(prepared.artifacts.cover_letter_path).read_bytes().startswith(b"%PDF-")

    fill = telegram.create_callback(application_id, "Compila", actor="Synthetic Owner")
    assert telegram.handle_callback_data(telegram.encode_callback(fill)).status == (
        "completed"
    )
    assert driver.current_url().startswith("file://")

    submit = telegram.create_callback(application_id, "Invia", actor="Synthetic Owner")
    summary, displayed_command = transport.pre_submit[-1]
    assert displayed_command == submit
    assert summary.principal_answers == (("work-auth", "SYNTHETIC-WORK-AUTHORIZATION"),)
    assert "Prefer not to answer" not in repr(summary)
    assert driver.local_submit_clicks == 0

    assert telegram.handle_callback_data(telegram.encode_callback(submit)).status == (
        "completed"
    )
    submitted = coordinator.get(application_id)
    assert submitted.lifecycle_state == "inviata"
    assert submitted.outcome.evidence.confirmation_id == "LOCAL-CONFIRMATION-42"
    assert driver.local_submit_clicks == 1
    assert telegram.handle_callback_data(telegram.encode_callback(submit)).status == (
        "replayed"
    )
    assert driver.local_submit_clicks == 1

    mailbox = LocalReadOnlyCareerMailbox(
        (
            CareerMessage(
                message_id="local-mail-1",
                thread_id="local-thread-1",
                sender_address="talent@example.com",
                sender_name="Example Talent",
                sender_kind=SenderKind.UNKNOWN,
                authenticated_sender=True,
                authenticated_domain="example.com",
                subject="Interview invitation — AI Scientist REQ-42",
                body_text=(
                    "We would like to invite you to interview for the AI Scientist "
                    "role at Example Research. Reference REQ-42."
                ),
                received_at="2026-07-22T10:05:00+00:00",
            ),
        )
    )
    correspondence_store = JsonCareerCorrespondenceStore(
        tmp_path / "private" / "career-correspondence"
    )
    correspondence_store.connect(
        CareerMailboxConnection(
            address="alex.jobs@gmail.com",
            connected_at="2026-07-22T10:01:00+00:00",
        )
    )
    monitor = CareerCorrespondenceMonitor(
        mailbox=mailbox,
        store=correspondence_store,
        applications=coordinator,
        clock=clock,
        candidate_name="Alex Example",
    )

    poll = monitor.poll()

    assert poll.status == MailboxPollStatus.COMPLETED
    assert poll.processed == 1
    assert coordinator.get(application_id).lifecycle_state == "colloquio"
    assert mailbox.fetch_calls == ["alex.jobs@gmail.com"]
    assert not hasattr(mailbox, "send")
    assert correspondence_store.drafts() == ()
    assert correspondence_store.classification_requests() == ()

    package = package_writer.package_path(application_id)
    assert stat.S_IMODE(package.stat().st_mode) == 0o700
    assert stat.S_IMODE((package / "application.json").stat().st_mode) == 0o600
    assert (package / "artifacts" / "cv.pdf").read_bytes().startswith(b"%PDF-")
    assert (
        json.loads((package / "submission-evidence.json").read_text())["evidence"][
            "confirmation_id"
        ]
        == "LOCAL-CONFIRMATION-42"
    )
    assert (
        json.loads((package / "correspondence.json").read_text())[0]["message_id"]
        == "local-mail-1"
    )

    markdown_index = package_writer.markdown_index_path.read_text(encoding="utf-8")
    with package_writer.csv_index_path.open(encoding="utf-8", newline="") as source:
        csv_rows = list(csv.DictReader(source))
    assert "colloquio" in markdown_index
    assert "Prefer not to answer" not in markdown_index
    assert csv_rows == [
        {
            "application_id": application_id,
            "company": "Example Research",
            "title": "AI Scientist",
            "location": "Zurich",
            "lifecycle": "colloquio",
            "submission_status": "verified",
            "updated_at": "2026-07-22T10:00:00+00:00",
        }
    ]
    assert network_attempts == []
