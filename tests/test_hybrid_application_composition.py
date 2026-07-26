import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application_composition import (  # noqa: E402
    HostedPreparationVacancyAdapter,
    HostedApplicationConfig,
    UnsupportedAtsAdapter,
    build_hosted_application_workflow_coordinator,
    build_hosted_tailoring_adapter,
)
from application_domain import (  # noqa: E402
    ArtifactFamily,
    OfficialVacancy,
    PreparedArtifacts,
    WorkflowAction,
)
from hosted_artifact_handoff import ArtifactHandoffIdentity  # noqa: E402
from hosted_tailoring import (  # noqa: E402
    HOSTED_TAILORING_STATE_VERSION,
    HostedTailoringAdapter,
    HostedTailoringStateStore,
)
from local_worker_main import build_production_runtime  # noqa: E402


def hosted_config() -> HostedApplicationConfig:
    return HostedApplicationConfig(
        repository="example-org/job-agent",
        branch="main",
        workflow="run.yml",
        github_token_keychain_service="job-agent.github",
        github_token_keychain_account="example-org/job-agent",
        handoff_key_keychain_service="job-agent.artifact-handoff",
        handoff_key_keychain_account="example-org/job-agent",
    )


def worker_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": "job-agent.local-worker-config.v1",
                "telegram": {
                    "actor_id": "42",
                    "chat_id": "99",
                    "token_keychain_service": "job-agent.telegram",
                    "token_keychain_account": "worker-bot",
                },
                "hosted_artifacts": {
                    "repository": "example-org/job-agent",
                    "branch": "main",
                    "workflow": "run.yml",
                    "github_token_keychain_service": "job-agent.github",
                    "github_token_keychain_account": "example-org/job-agent",
                    "handoff_key_keychain_service": "job-agent.artifact-handoff",
                    "handoff_key_keychain_account": "example-org/job-agent",
                },
            }
        ),
        encoding="utf-8",
    )


def completed_hosted_state(intent_id, identity, artifacts):
    timestamp = "2026-07-24T10:00:00+00:00"
    return {
        "version": HOSTED_TAILORING_STATE_VERSION,
        "intent_id": intent_id,
        "identity": identity.to_dict(),
        "phase": "completed",
        "transport_scope": {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        },
        "prepared_at": timestamp,
        "dispatch_started_at": timestamp,
        "dispatch_accepted_at": timestamp,
        "run_discovery_deadline": "2026-07-24T10:10:00+00:00",
        "baseline_captured": True,
        "prior_workflow_run_ids": [],
        "workflow_run_id": 101,
        "failure_reason": None,
        "artifacts": asdict(artifacts),
    }


class FixedClock:
    def now(self):
        return datetime(2026, 7, 24, 10, tzinfo=timezone.utc)


class FakeAts:
    def validate_submit(self, application_id, manifest):
        return True

    def fill(self, application_id, intent_id, artifacts):
        raise AssertionError("Fill cannot run before preparation succeeds")

    def submit(self, application_id, manifest):
        raise AssertionError("Submit cannot run before preparation succeeds")


class FakeVacancies:
    vacancy = OfficialVacancy(
        version="sha256:" + "a" * 64,
        fingerprint="sha256:" + "a" * 64,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build reliable AI systems.",
    )

    def retrieve(self, opportunity):
        return self.vacancy

    def revalidate(self, opportunity, previous):
        return self.vacancy


def test_local_preparation_dependencies_are_exact_and_fail_closed():
    vacancy = OfficialVacancy(
        version="sha256:" + "c" * 64,
        fingerprint="sha256:" + "c" * 64,
        freshness="2026-07-26T08:00:00+00:00",
        description="Build reliable AI systems.",
    )
    loaded = []
    inputs = SimpleNamespace(
        load=lambda application_id, version: (
            loaded.append((application_id, version))
            or SimpleNamespace(official_vacancy=vacancy)
        )
    )
    adapter = HostedPreparationVacancyAdapter(inputs)
    opportunity = {
        "application_id": "approved-1234567890abcdef",
        "official_vacancy_version": vacancy.version,
    }

    assert adapter.retrieve(opportunity) == vacancy
    assert adapter.revalidate(opportunity, vacancy) == vacancy
    assert loaded == [
        ("approved-1234567890abcdef", vacancy.version),
        ("approved-1234567890abcdef", vacancy.version),
    ]
    assert UnsupportedAtsAdapter().validate_submit("app", object()) is False


def test_real_hosted_tailoring_composition_keeps_secret_values_out_of_config(
    tmp_path,
):
    github_token = "github-secret-value"
    handoff_secret = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")

    adapter = build_hosted_tailoring_adapter(
        repository_root=tmp_path,
        config=hosted_config(),
        github_token=github_token,
        handoff_key=handoff_secret,
        source_version_loader=lambda: "hosted-source-v1",
    )

    assert isinstance(adapter, HostedTailoringAdapter)
    assert github_token not in repr(adapter)
    assert handoff_secret not in repr(adapter)
    assert github_token not in repr(hosted_config())
    assert handoff_secret not in repr(hosted_config())


def test_hosted_fill_gate_revalidates_the_completed_persisted_identity(
    tmp_path,
):
    vacancy_version = "sha256:" + "a" * 64
    correct_identity = ArtifactHandoffIdentity(
        application_id="application-001",
        official_vacancy_version=vacancy_version,
    )
    wrong_identity = ArtifactHandoffIdentity(
        application_id="application-002",
        official_vacancy_version=vacancy_version,
    )
    artifact_root = (
        tmp_path
        / "data"
        / "private"
        / "application-artifacts"
        / correct_identity.application_id
        / ("sha256:" + "b" * 64)
    )
    artifact_root.mkdir(parents=True, mode=0o700)
    for directory in (
        tmp_path / "data" / "private" / "application-artifacts",
        artifact_root.parent,
        artifact_root,
    ):
        directory.chmod(0o700)
    cv = artifact_root / "cv.pdf"
    cover = artifact_root / "cover-letter.pdf"
    cv.write_bytes(b"%PDF- cv")
    cover.write_bytes(b"%PDF- cover")
    cv.chmod(0o600)
    cover.chmod(0o600)
    import hashlib

    artifacts = PreparedArtifacts(
        version="sha256:" + "b" * 64,
        cv_path=str(cv),
        cover_letter_path=str(cover),
        cv_hash="sha256:" + hashlib.sha256(cv.read_bytes()).hexdigest(),
        cover_letter_hash="sha256:"
        + hashlib.sha256(cover.read_bytes()).hexdigest(),
        evidence_source_version="sha256:" + "c" * 64,
        matrix_version="job-agent.requirements-evidence.v1",
        family=ArtifactFamily.RESEARCH,
    )
    store = HostedTailoringStateStore(
        tmp_path / "data" / "private" / "hosted-tailoring-state"
    )
    store.save(
        completed_hosted_state("prepare:one", wrong_identity, artifacts),
        wrong_identity,
    )
    adapter = build_hosted_tailoring_adapter(
        repository_root=tmp_path,
        config=hosted_config(),
        github_token="github-secret",
        handoff_key=base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        source_version_loader=lambda: "hosted-source-v1",
        hosted_client=SimpleNamespace(),
    )

    assert adapter.verify_artifacts(artifacts) is False

    for path in (
        tmp_path / "data" / "private" / "hosted-tailoring-state"
    ).glob("*.json"):
        path.unlink()
    correct_store = HostedTailoringStateStore(
        tmp_path / "data" / "private" / "hosted-tailoring-state"
    )
    correct_store.save(
        completed_hosted_state("prepare:two", correct_identity, artifacts),
        correct_identity,
    )

    assert adapter.verify_artifacts(artifacts) is True

    state_file = next(
        (
            tmp_path / "data" / "private" / "hosted-tailoring-state"
        ).glob("*.json")
    )
    state_file.chmod(0o640)
    assert adapter.verify_artifacts(artifacts) is False


def test_pending_hosted_recovery_returns_without_exposing_compila(tmp_path):
    class UnavailableClient:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }

        def workflow_run_ids(self, identity):
            return frozenset()

        def dispatch(self, identity):
            return None

        def workflow_runs(self, identity, *, exclude_run_ids):
            return ()

    coordinator = build_hosted_application_workflow_coordinator(
        repository_root=tmp_path,
        config=hosted_config(),
        github_token="github-secret",
        handoff_key=base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        source_version_loader=lambda: "hosted-source-v1",
        ats=FakeAts(),
        official_vacancies=FakeVacancies(),
        clock=FixedClock(),
        hosted_client=UnavailableClient(),
        token_factory=lambda: "prepare-token",
    )
    coordinator.propose(
        application_id="application-001",
        opportunity={"company": "Example", "title": "AI Engineer"},
        version="opportunity-v1",
    )
    prepare = coordinator.issue_authorization(
        "application-001", WorkflowAction.PREPARE, actor="Synthetic Owner"
    )

    result = coordinator.handle(prepare)
    snapshot = coordinator.get("application-001")

    assert result.status == "accepted"
    assert snapshot.lifecycle_state == "approvata"
    assert snapshot.artifacts is None
    assert snapshot.next_action is None


def test_runtime_reads_hybrid_secrets_and_injects_hosted_tailoring(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "worker-config.json"
    worker_config(config_path)
    reads = []
    composed = {}
    hosted_marker = object()
    coordinator = SimpleNamespace(
        command_for_token=lambda token: None,
        handle=lambda command: None,
    )

    class Secrets:
        values = {
            ("job-agent.telegram", "worker-bot"): "telegram-secret",
            ("job-agent.github", "example-org/job-agent"): "github-secret",
            (
                "job-agent.artifact-handoff",
                "example-org/job-agent",
            ): base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        }

        def get(self, service, account):
            reads.append((service, account))
            return self.values.get((service, account))

    def build_tailoring(**kwargs):
        composed["tailoring"] = kwargs
        return hosted_marker

    def build_coordinator(**kwargs):
        composed["coordinator"] = kwargs
        return coordinator

    monkeypatch.setattr(
        "local_worker_main.build_hosted_tailoring_adapter", build_tailoring
    )
    monkeypatch.setattr(
        "local_worker_main.build_application_workflow_coordinator",
        build_coordinator,
    )

    runtime = build_production_runtime(
        state_path=tmp_path / "worker-state.json",
        config_path=config_path,
        repository_root=tmp_path,
        secret_store=Secrets(),
        api_factory=lambda **kwargs: SimpleNamespace(poll_updates=lambda **kwargs: []),
        application_api_factory=lambda **kwargs: SimpleNamespace(),
        telegram_poll_timeout=0,
    )

    assert runtime.status()["health"] != "disabled"
    assert reads == [
        ("job-agent.telegram", "worker-bot"),
        ("job-agent.github", "example-org/job-agent"),
        ("job-agent.artifact-handoff", "example-org/job-agent"),
    ]
    assert composed["tailoring"]["github_token"] == "github-secret"
    assert composed["coordinator"]["tailoring"] is hosted_marker
    assert isinstance(composed["coordinator"]["ats"], UnsupportedAtsAdapter)
    assert isinstance(
        composed["coordinator"]["official_vacancies"],
        HostedPreparationVacancyAdapter,
    )


def test_runtime_fails_disabled_when_github_keychain_secret_is_missing(tmp_path):
    config_path = tmp_path / "worker-config.json"
    worker_config(config_path)

    class Secrets:
        def get(self, service, account):
            if service == "job-agent.telegram":
                return "telegram-secret"
            if service == "job-agent.artifact-handoff":
                return base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
            return None

    runtime = build_production_runtime(
        state_path=tmp_path / "worker-state.json",
        config_path=config_path,
        repository_root=tmp_path,
        secret_store=Secrets(),
        application_ats=FakeAts(),
        official_vacancies=FakeVacancies(),
        application_clock=FixedClock(),
        hosted_source_version_loader=lambda: "hosted-source-v1",
    )

    status = runtime.status()
    assert status["health"] == "disabled"
    assert status["reason"] == "github_secret_missing"


def test_runtime_fails_disabled_when_handoff_keychain_secret_is_missing(tmp_path):
    config_path = tmp_path / "worker-config.json"
    worker_config(config_path)

    class Secrets:
        def get(self, service, account):
            if service == "job-agent.telegram":
                return "telegram-secret"
            if service == "job-agent.github":
                return "github-secret"
            return None

    runtime = build_production_runtime(
        state_path=tmp_path / "worker-state.json",
        config_path=config_path,
        repository_root=tmp_path,
        secret_store=Secrets(),
        application_ats=FakeAts(),
        official_vacancies=FakeVacancies(),
        application_clock=FixedClock(),
        hosted_source_version_loader=lambda: "hosted-source-v1",
    )

    status = runtime.status()
    assert status["health"] == "disabled"
    assert status["reason"] == "artifact_handoff_secret_missing"
    assert "github-secret" not in repr(status)
