from dataclasses import replace
from datetime import datetime, timezone
import csv
import hashlib
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_composition import build_application_workflow_coordinator  # noqa: E402
from application_domain import (  # noqa: E402
    FilledApplication,
    OfficialVacancy,
    PreparedArtifacts,
    SubmissionOutcome,
)
from application_packages import LocalApplicationPackageWriter, _index_row  # noqa: E402
from application_storage import JsonApplicationStore  # noqa: E402
from application_telegram import TelegramCommandHandler  # noqa: E402
from application_workflow import ApplicationWorkflowCoordinator, WorkflowAction  # noqa: E402
from telegram_applications import TelegramApplicationApi  # noqa: E402


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


class FileTailoring:
    def __init__(self, root: pathlib.Path):
        root.mkdir(parents=True, exist_ok=True)
        self.cv = root / "cv.pdf"
        self.cover = root / "cover.pdf"
        self.cv.write_bytes(b"cv")
        self.cover.write_bytes(b"cover")

    def prepare(self, application_id, intent_id, opportunity, official_vacancy):
        return PreparedArtifacts(
            version="artifacts-v1",
            cv_path=str(self.cv),
            cover_letter_path=str(self.cover),
            cv_hash=_file_hash(self.cv),
            cover_letter_hash=_file_hash(self.cover),
        )

    def reload_master_cv(self):
        return "evidence-v1"

    def verify_artifacts(self, artifacts):
        return True


class FakeAts:
    def __init__(self):
        self.submit_calls = []
        self.submit_validation = True

    def fill(self, application_id, intent_id, artifacts):
        return FilledApplication(
            answers={"references": "Not provided"},
            artifact_version=artifacts.version,
        )

    def validate_submit(self, application_id, manifest):
        if isinstance(self.submit_validation, BaseException):
            raise self.submit_validation
        return self.submit_validation

    def submit(self, application_id, manifest):
        self.submit_calls.append((application_id, manifest.version))
        return SubmissionOutcome("uncertain")


class FakeVacancies:
    def __init__(self):
        self.current = OfficialVacancy(
            version="vacancy-v1",
            fingerprint="sha256:role",
            freshness="2026-07-16T10:30:00+00:00",
            description="Build reliable AI systems.",
        )

    def retrieve(self, opportunity):
        return self.current

    def revalidate(self, opportunity, previous):
        return self.current


class RecordingWriter:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.fail_next = False
        self.writes = []

    def write(self, application):
        self.writes.append(application)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic package publication failure")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{application.application_id}.txt"
        path.write_text(application.authorization_version, encoding="utf-8")
        return path


def _coordinator(tmp_path, *, writer=None, store=None, ats=None, vacancies=None):
    store = store or JsonApplicationStore(tmp_path / "state")
    return ApplicationWorkflowCoordinator(
        store=store,
        tailoring=FileTailoring(tmp_path),
        ats=ats or FakeAts(),
        report_writer=writer or RecordingWriter(tmp_path / "reports"),
        official_vacancies=vacancies or FakeVacancies(),
        clock=FixedClock(),
        token_factory=iter(("prepare", "fill", "submit")).__next__,
    )


def _ready_application(tmp_path):
    store = JsonApplicationStore(tmp_path / "state")
    ats = FakeAts()
    vacancies = FakeVacancies()
    coordinator = _coordinator(
        tmp_path, store=store, ats=ats, vacancies=vacancies
    )
    coordinator.propose(
        application_id="app-1",
        opportunity={
            "company": "Example AI",
            "title": "AI Scientist",
            "location": "Zurich",
        },
        version="opportunity-v1",
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "app-1", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )
    )
    coordinator.handle(
        coordinator.issue_authorization(
            "app-1", WorkflowAction.FILL, actor="Synthetic Owner"
        )
    )
    return coordinator, store, ats, vacancies


@pytest.mark.parametrize("evidence", [None, {}])
def test_index_row_handles_uncertain_outcome_without_evidence(evidence):
    row = _index_row(
        {
            "application_id": "app-1",
            "opportunity": {"company": "Example"},
            "lifecycle_state": "pronta da inviare",
            "history": [{"occurred_at": "2026-07-16T10:30:00+00:00"}],
            "outcome": {"status": "uncertain", "evidence": evidence},
        }
    )

    assert row["submission_status"] == "uncertain"
    assert row["updated_at"] == "2026-07-16T10:30:00+00:00"


def test_authorization_mutation_is_staged_in_durable_package_outbox(tmp_path):
    store = JsonApplicationStore(tmp_path / "state")
    writer = RecordingWriter(tmp_path / "reports")
    coordinator = _coordinator(tmp_path, store=store, writer=writer)
    coordinator.propose(
        application_id="app-1",
        opportunity={"company": "Example", "title": "Researcher"},
        version="opportunity-v1",
    )
    writer.fail_next = True

    with pytest.raises(RuntimeError, match="package publication failure"):
        coordinator.issue_authorization(
            "app-1", WorkflowAction.PREPARE, actor="Synthetic Owner"
        )

    pending = store.load("app-1")
    assert pending.package_publication_pending is True
    assert len(pending.authorizations) == 1

    recovered = _coordinator(
        tmp_path,
        store=store,
        writer=RecordingWriter(tmp_path / "recovered-reports"),
    ).get("app-1")
    assert recovered.package_publication_pending is False
    assert len(recovered.authorizations) == 1


def test_submit_requires_verified_vacancy_before_claim(tmp_path):
    coordinator, store, ats, vacancies = _ready_application(tmp_path)
    command = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    vacancies.current = replace(vacancies.current, verified=False)

    result = coordinator.handle(command)

    assert result.status == "stale"
    assert store.load("app-1").submission_intents == ()
    assert ats.submit_calls == []


@pytest.mark.parametrize(
    "validation",
    [False, RuntimeError("trusted session unavailable")],
    ids=("rejected", "unavailable"),
)
def test_submit_requires_live_ats_review_before_claim(tmp_path, validation):
    coordinator, store, ats, _ = _ready_application(tmp_path)
    command = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    ats.submit_validation = validation

    result = coordinator.handle(command)

    snapshot = store.load("app-1")
    assert result.status == "stale"
    assert snapshot.submission_intents == ()
    assert ats.submit_calls == []

    if isinstance(validation, BaseException):
        assert snapshot.authorizations[-1].consumed_at is None
        assert snapshot.authorizations[-1].invalidated_at is not None
        assert snapshot.operational_status == "ats_review_reprepare_required"
        assert snapshot.manifest is None
        assert snapshot.next_action == WorkflowAction.FILL
        assert snapshot.authorization_version == snapshot.artifacts.version


def test_reprepare_never_reactivates_an_old_submit_token_for_identical_manifest(
    tmp_path,
):
    coordinator, store, ats, _ = _ready_application(tmp_path)
    old_submit = coordinator.issue_authorization(
        "app-1", WorkflowAction.SUBMIT, actor="Synthetic Owner"
    )
    ats.submit_validation = RuntimeError("browser session unavailable")
    assert coordinator.handle(old_submit).status == "stale"

    coordinator._token_factory = iter(("refill",)).__next__
    ats.submit_validation = True
    refill = coordinator.issue_authorization(
        "app-1", WorkflowAction.FILL, actor="Synthetic Owner"
    )
    assert coordinator.handle(refill).status == "completed"
    assert store.load("app-1").authorization_version == old_submit.scope.version

    assert coordinator.handle(old_submit).status == "stale"
    assert ats.submit_calls == []


def test_composition_rejects_ats_without_mandatory_submit_validation(tmp_path):
    class UnsafeAts:
        def fill(self, application_id, intent_id, artifacts):
            raise AssertionError("not reached")

        def submit(self, application_id, manifest):
            raise AssertionError("not reached")

    with pytest.raises(TypeError, match="validate_submit"):
        build_application_workflow_coordinator(
            repository_root=tmp_path,
            tailoring=FileTailoring(tmp_path),
            ats=UnsafeAts(),
            official_vacancies=FakeVacancies(),
            clock=FixedClock(),
        )


@pytest.mark.parametrize("operation", ["get", "post", "document", "json"])
def test_telegram_transport_errors_never_expose_token_or_url(tmp_path, operation):
    token = "very-secret-bot-token"
    leaked_url = f"https://api.telegram.org/bot{token}/sendMessage"

    class Response:
        ok = True

        def json(self):
            if operation == "json":
                raise ValueError(f"bad response from {leaked_url}")
            return {"ok": True, "result": []}

    class Http:
        def get(self, url, **kwargs):
            if operation == "get":
                raise ConnectionError(f"could not reach {url}")
            return Response()

        def post(self, url, **kwargs):
            if operation in {"post", "document"}:
                raise ConnectionError(f"could not reach {url}")
            return Response()

    api = TelegramApplicationApi(
        token=token,
        chat_id="chat",
        user_id="user",
        callback_encoder=TelegramCommandHandler.encode_callback,
        http=Http(),
    )
    if operation in {"get", "json"}:
        call = lambda: api.poll_updates(offset=None, timeout=0)
    elif operation == "post":
        call = lambda: api.send_status("status")
    else:
        document = tmp_path / "cv.pdf"
        document.write_bytes(b"cv")
        call = lambda: api._post_document(
            document, filename="cv.pdf", caption="cv"
        )

    with pytest.raises(RuntimeError) as error:
        call()

    assert token not in str(error.value)
    assert "api.telegram.org" not in str(error.value)


def test_production_composition_uses_real_store_and_package_indexes(tmp_path):
    repository = tmp_path / "repository"
    coordinator = build_application_workflow_coordinator(
        repository_root=repository,
        tailoring=FileTailoring(tmp_path),
        ats=FakeAts(),
        official_vacancies=FakeVacancies(),
        clock=FixedClock(),
        token_factory=lambda: "token",
    )

    coordinator.propose(
        application_id="app-1",
        opportunity={
            "company": "Example AI",
            "title": "AI Scientist",
            "location": "Zurich",
        },
        version="opportunity-v1",
    )

    persisted = JsonApplicationStore(
        repository / "data" / "private" / "application-state"
    ).load("app-1")
    packages = LocalApplicationPackageWriter.for_repository(repository)
    package = packages.package_path("app-1")
    rows = list(csv.DictReader(packages.csv_index_path.open()))
    assert persisted.application_id == "app-1"
    assert (package / "application.json").is_file()
    assert rows[0]["application_id"] == "app-1"
    assert packages.markdown_index_path.is_file()


def _file_hash(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
