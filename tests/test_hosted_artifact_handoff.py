import base64
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import os
import pathlib
import shutil
import stat
import sys
from types import SimpleNamespace
import zipfile

import pytest
from reportlab.pdfgen.canvas import Canvas
import requests
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import (  # noqa: E402
    ArtifactClaimTrace,
    ArtifactDocument,
    ArtifactFamily,
    EvidenceKind,
    PreparedArtifacts,
)
from application_identity import approved_application_id  # noqa: E402
from hosted_artifact_handoff import (  # noqa: E402
    ArtifactHandoffIdentity,
    ArtifactHandoffKey,
    HostedArtifactHandoff,
    LocalArtifactHandoff,
)
from hosted_artifact_github import (  # noqa: E402
    GitHubHostedArtifactClient,
    HostedDispatchAmbiguous,
    HostedDispatchRejected,
    HostedWorkflowRun,
    hosted_workflow_run_name,
)
from hosted_artifact_preparation import (  # noqa: E402
    HostedArtifactPreparationService,
    HostedPreparationInput,
    HostedPreparationInputStore,
)
from hosted_tailoring import (  # noqa: E402
    HOSTED_TAILORING_STATE_VERSION,
    HostedPreparationFailed,
    HostedPreparationPending,
    HostedPreparationResolutionRequired,
    HostedTailoringAdapter,
    HostedTailoringStateStore,
)
from application_domain import OfficialVacancy  # noqa: E402


def sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def handoff_key() -> ArtifactHandoffKey:
    return ArtifactHandoffKey.from_base64(
        base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    )


def prepared_artifacts(tmp_path: pathlib.Path) -> PreparedArtifacts:
    source = tmp_path / "hosted-source"
    source.mkdir()
    cv = source / "cv.pdf"
    cover = source / "cover-letter.pdf"
    cv.write_bytes(b"%PDF- hosted cv")
    cover.write_bytes(b"%PDF- hosted cover")
    return PreparedArtifacts(
        version=sha256(b"artifact-version"),
        cv_path=str(cv),
        cover_letter_path=str(cover),
        cv_hash=sha256(cv.read_bytes()),
        cover_letter_hash=sha256(cover.read_bytes()),
        evidence_source_version=sha256(b"evidence"),
        matrix_version="job-agent.requirements-evidence.v1",
        family=ArtifactFamily.RESEARCH,
        claims=(
            ArtifactClaimTrace(
                statement="Uses Python for reproducible ML research.",
                kind=EvidenceKind.SKILL,
                evidence_ids=("skill-python",),
                appears_in=(ArtifactDocument.CV, ArtifactDocument.COVER_LETTER),
            ),
        ),
    )


def identity() -> ArtifactHandoffIdentity:
    vacancy_version = sha256(b"official vacancy")
    return ArtifactHandoffIdentity(
        application_id=approved_application_id(
            "example:research-role",
            vacancy_version,
        ),
        official_vacancy_version=vacancy_version,
    )


def authority() -> dict[str, str]:
    return {
        "repository": "example-org/job-agent",
        "workflow": "run.yml",
        "branch": "main",
    }


def async_client(encrypted_path: pathlib.Path):
    class Client:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }

        def __init__(self):
            self.dispatched = []
            self.runs = ()
            self.packages = {}

        def workflow_run_ids(self, value):
            assert value == identity()
            return frozenset(run.run_id for run in self.runs)

        def dispatch(self, value):
            self.dispatched.append(value)

        def workflow_runs(self, value, *, exclude_run_ids):
            assert value == identity()
            return tuple(
                run for run in self.runs if run.run_id not in exclude_run_ids
            )

        def package_for_run(self, value, *, workflow_run_id):
            assert value == identity()
            return self.packages.get(workflow_run_id)

    return Client()


def fixed_now():
    return datetime(2026, 7, 24, 10, tzinfo=timezone.utc)


def actions_archive(name, value):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, value)
    return output.getvalue()


def test_hosted_bundle_round_trips_privately_into_verified_local_artifacts(tmp_path):
    encrypted = tmp_path / "artifact-package.enc"
    original = prepared_artifacts(tmp_path)
    exported = HostedArtifactHandoff(
        key=handoff_key(),
        authority=authority(),
    ).export(
        identity=identity(),
        artifacts=original,
        destination=encrypted,
    )

    assert exported.path == encrypted
    assert exported.package_hash == sha256(encrypted.read_bytes())
    assert b"%PDF-" not in encrypted.read_bytes()
    assert b"Uses Python" not in encrypted.read_bytes()

    installed = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=tmp_path / "local-private-artifacts",
    ).install(encrypted, expected_identity=identity())

    assert pathlib.Path(installed.cv_path).read_bytes() == b"%PDF- hosted cv"
    assert pathlib.Path(installed.cover_letter_path).read_bytes() == (
        b"%PDF- hosted cover"
    )
    assert installed.version == original.version
    assert installed.claims == original.claims
    assert pathlib.Path(installed.cv_path).parent == (
        tmp_path
        / "local-private-artifacts"
        / identity().application_id
        / original.version
    )
    private_root = tmp_path / "local-private-artifacts"
    application_root = private_root / identity().application_id
    artifact_root = pathlib.Path(installed.cv_path).parent
    assert stat.S_IMODE(private_root.lstat().st_mode) == 0o700
    assert stat.S_IMODE(application_root.lstat().st_mode) == 0o700
    assert stat.S_IMODE(artifact_root.lstat().st_mode) == 0o700
    assert stat.S_IMODE(pathlib.Path(installed.cv_path).lstat().st_mode) == 0o600
    assert (
        stat.S_IMODE(pathlib.Path(installed.cover_letter_path).lstat().st_mode)
        == 0o600
    )


@pytest.mark.parametrize(
    "drift",
    (
        "root-mode",
        "application-mode",
        "artifact-mode",
        "cv-mode",
        "cover-mode",
        "cv-bytes",
        "cover-bytes",
        "cv-symlink",
        "relocated",
    ),
)
def test_installed_verification_rejects_permission_symlink_and_relocation_drift(
    tmp_path,
    drift,
):
    encrypted = tmp_path / "artifact-package.enc"
    original = prepared_artifacts(tmp_path)
    HostedArtifactHandoff(
        key=handoff_key(),
        authority=authority(),
    ).export(
        identity=identity(),
        artifacts=original,
        destination=encrypted,
    )
    private_root = tmp_path / "local-private-artifacts"
    local = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=private_root,
    )
    installed = local.install(encrypted, expected_identity=identity())
    cv = pathlib.Path(installed.cv_path)
    cover = pathlib.Path(installed.cover_letter_path)
    artifact_root = cv.parent
    application_root = artifact_root.parent
    candidate = installed

    if drift == "root-mode":
        private_root.chmod(0o750)
    elif drift == "application-mode":
        application_root.chmod(0o750)
    elif drift == "artifact-mode":
        artifact_root.chmod(0o750)
    elif drift == "cv-mode":
        cv.chmod(0o640)
    elif drift == "cover-mode":
        cover.chmod(0o640)
    elif drift == "cv-bytes":
        cv.write_bytes(b"%PDF- tampered cv")
    elif drift == "cover-bytes":
        cover.write_bytes(b"%PDF- tampered cover")
    elif drift == "cv-symlink":
        original_bytes = cv.read_bytes()
        target = tmp_path / "same-cv.pdf"
        target.write_bytes(original_bytes)
        cv.unlink()
        cv.symlink_to(target)
    elif drift == "relocated":
        relocated = tmp_path / "relocated"
        relocated.mkdir(mode=0o700)
        relocated_cv = relocated / "cv.pdf"
        relocated_cover = relocated / "cover-letter.pdf"
        shutil.copyfile(cv, relocated_cv)
        shutil.copyfile(cover, relocated_cover)
        relocated_cv.chmod(0o600)
        relocated_cover.chmod(0o600)
        candidate = PreparedArtifacts(
            **{
                **asdict(installed),
                "cv_path": str(relocated_cv),
                "cover_letter_path": str(relocated_cover),
            }
        )

    assert local.verify_installed(identity(), candidate) is False


def test_installed_verification_rejects_wrong_identity_and_owner(
    tmp_path, monkeypatch
):
    encrypted = tmp_path / "artifact-package.enc"
    HostedArtifactHandoff(
        key=handoff_key(),
        authority=authority(),
    ).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted,
    )
    local = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=tmp_path / "local-private-artifacts",
    )
    installed = local.install(encrypted, expected_identity=identity())
    wrong_identity = ArtifactHandoffIdentity(
        application_id=approved_application_id(
            "example:different-role",
            identity().official_vacancy_version,
        ),
        official_vacancy_version=identity().official_vacancy_version,
    )

    assert local.verify_installed(wrong_identity, installed) is False

    actual_uid = os.getuid()
    monkeypatch.setattr(
        "hosted_artifact_handoff.os.getuid",
        lambda: actual_uid + 1,
    )
    assert local.verify_installed(identity(), installed) is False


def test_installed_verification_rejects_a_symlinked_configured_private_root(
    tmp_path,
):
    encrypted = tmp_path / "artifact-package.enc"
    HostedArtifactHandoff(
        key=handoff_key(),
        authority=authority(),
    ).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted,
    )
    real_root = tmp_path / "real-private-root"
    linked_root = tmp_path / "configured-private-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    local = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=linked_root,
    )

    with pytest.raises(ValueError, match="install directory is invalid"):
        local.install(encrypted, expected_identity=identity())


def test_local_install_rejects_a_package_for_another_application_version(tmp_path):
    encrypted = tmp_path / "artifact-package.enc"
    HostedArtifactHandoff(
        key=handoff_key(),
        authority=authority(),
    ).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted,
    )
    wrong_identity = ArtifactHandoffIdentity(
        application_id=identity().application_id,
        official_vacancy_version=sha256(b"changed official vacancy"),
    )
    destination = tmp_path / "local-private-artifacts"

    with pytest.raises(ValueError, match="identity mismatch"):
        LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=destination,
        ).install(encrypted, expected_identity=wrong_identity)

    assert not destination.exists()


def test_local_install_rejects_tampered_ciphertext_and_wrong_authority(tmp_path):
    encrypted = tmp_path / "artifact-package.enc"
    HostedArtifactHandoff(
        key=handoff_key(),
        authority={
            **authority(),
            "workflow": "untrusted.yml",
        },
    ).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted,
    )
    local = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=tmp_path / "local-private-artifacts",
    )

    with pytest.raises(ValueError, match="authority mismatch"):
        local.install(encrypted, expected_identity=identity())

    value = bytearray(encrypted.read_bytes())
    value[-1] ^= 1
    encrypted.write_bytes(value)
    with pytest.raises(ValueError, match="authentication failed"):
        local.install(encrypted, expected_identity=identity())


def test_handoff_identity_rejects_noncanonical_application_ids():
    with pytest.raises(ValueError, match="canonical"):
        ArtifactHandoffIdentity(
            application_id=" application-001 ",
            official_vacancy_version=sha256(b"official vacancy"),
        )


class RecordingProvider:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return self.response


def test_hosted_preparation_uses_authoritative_input_and_exports_one_package(
    tmp_path,
):
    evidence_path = tmp_path / "evidence.yaml"
    evidence_path.write_text(
        yaml.safe_dump(
            {
                "highlights": [],
                "skill_evidence": [
                    {
                        "id": "skill-python",
                        "kind": "skill",
                        "claim": "Uses Python for reproducible ML research.",
                        "evidence": "master-cv:skills",
                        "suitable_for": ["research"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canvas = Canvas(str(canonical_cv))
    y = 800
    for line in (
        "Synthetic Candidate",
        "Applied AI Researcher",
        "synthetic@example.com",
        "Applied AI researcher building reproducible systems.",
        "I validate machine-learning systems against real requirements.",
        "Machine Learning Researcher",
        "Example Institute",
        "Amsterdam",
        "2022 - Present",
        "Built reproducible research systems.",
        "PhD in Artificial Intelligence",
        "Example University",
        "2018 - 2022",
    ):
        canvas.drawString(50, y, line)
        y -= 18
    canvas.save()
    vacancy_version = identity().official_vacancy_version
    store = HostedPreparationInputStore(tmp_path / "authoritative-inputs")
    store.save(
        HostedPreparationInput(
            stable_id="example:research-role",
            official_vacancy=OfficialVacancy(
                version=vacancy_version,
                fingerprint=vacancy_version,
                freshness="2026-07-24T10:00:00+00:00",
                description="Build reproducible computer-vision research systems.",
            ),
            opportunity={
                "artifact_family": "research",
                "requirements_evidence_matrix": {
                    "version": "job-agent.requirements-evidence.v1",
                    "official_vacancy_version": vacancy_version,
                    "rows": [
                        {
                            "id": "req-python",
                            "requirement": "Python",
                            "importance": "required",
                            "status": "matched",
                            "evidence_ids": ["skill-python"],
                            "explanation": "Approved Python evidence is present.",
                        }
                    ],
                },
            },
        )
    )
    provider = RecordingProvider(
        {
            "headline": "Applied AI Researcher",
            "contacts": ["synthetic@example.com"],
            "summary": ["Applied AI researcher building reproducible systems."],
            "experience": [
                {
                    "role": "Machine Learning Researcher",
                    "organization": "Example Institute",
                    "location": "Amsterdam",
                    "dates": "2022 - Present",
                    "bullets": ["Built reproducible research systems."],
                }
            ],
            "education": [
                {
                    "degree": "PhD in Artificial Intelligence",
                    "institution": "Example University",
                    "location": "Amsterdam",
                    "dates": "2018 - 2022",
                }
            ],
            "selected_publications": [],
            "selected_evidence_ids": ["skill-python"],
            "target_requirement_ids": ["req-python"],
            "target_role": "computer-vision research systems",
            "cover_letter_source_paragraphs": [
                "Applied AI researcher building reproducible systems.",
                "I validate machine-learning systems against real requirements.",
            ],
        }
    )
    destination = tmp_path / "out" / "application-artifacts.enc"

    exported = HostedArtifactPreparationService(
        repository_root=tmp_path / "hosted-job-agent",
        inputs=store,
        evidence_path=evidence_path,
        canonical_cv_path=canonical_cv,
        candidate_name="Synthetic Candidate",
        provider=provider,
        key=handoff_key(),
        authority=authority(),
    ).prepare(identity=identity(), destination=destination)

    assert exported.path == destination
    assert pathlib.Path(exported.artifacts.cv_path).name == "cv.pdf"
    assert pathlib.Path(exported.artifacts.cover_letter_path).name == "cover-letter.pdf"
    assert exported.artifacts.cv_hash.startswith("sha256:")
    assert exported.artifacts.cover_letter_hash.startswith("sha256:")
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request["official_vacancy"]["version"] == vacancy_version
    assert request["requirements_evidence_matrix"]["rows"][0]["id"] == (
        "req-python"
    )
    installed = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=tmp_path / "installed",
    ).install(destination, expected_identity=identity())
    assert installed.matrix_version == "job-agent.requirements-evidence.v1"


def test_deep_grade_captures_only_candidate_safe_hosted_preparation_input(tmp_path):
    vacancy_version = sha256(b"captured vacancy")
    store = HostedPreparationInputStore(tmp_path / "authoritative-inputs")
    graded_job = {
        "stable_id": "example:research-role",
        "title": "Research Scientist",
        "official_description": "Build trustworthy computer-vision systems.",
        "official_vacancy_version": vacancy_version,
        "verification_status": "verified",
        "health": "must never enter hosted preparation input",
        "demographic": "must never enter hosted preparation input",
        "portfolio_evaluation": {
            "opportunity_id": "example:research-role",
            "vacancy_retrieved_at": "2026-07-24T10:00:00+00:00"
        },
        "requirements_evidence_matrix": {
            "version": "job-agent.requirements-evidence.v1",
            "official_vacancy_version": vacancy_version,
            "rows": [
                {
                    "id": "req-python",
                    "requirement": "Python",
                    "importance": "required",
                    "status": "matched",
                    "evidence_ids": ["skill-python"],
                    "explanation": "Approved Python evidence is present.",
                }
            ],
        },
    }

    captured = store.capture_graded((graded_job,))

    assert captured == (vacancy_version,)
    loaded = store.load(vacancy_version)
    assert loaded.stable_id == "example:research-role"
    assert loaded.opportunity["artifact_family"] == "research"
    assert loaded.official_vacancy.description.startswith("Build trustworthy")
    serialized = str(loaded.to_dict())
    assert "must never enter" not in serialized
    assert "health" not in serialized
    assert "demographic" not in serialized


def test_github_client_dispatches_only_the_exact_artifact_identity(monkeypatch):
    calls = []

    class Response:
        ok = True
        status_code = 204

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("hosted_artifact_github.requests.post", fake_post)
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    client.dispatch(identity())

    assert calls[0][0].endswith("/repos/example-org/job-agent/dispatches")
    assert calls[0][1]["json"] == {
        "event_type": "prepare-application",
        "client_payload": identity().to_dict(),
    }


@pytest.mark.parametrize("status_code", (400, 401, 403, 404, 422, 429))
def test_github_client_classifies_only_4xx_dispatch_as_rejected(
    monkeypatch,
    status_code,
):
    monkeypatch.setattr(
        "hosted_artifact_github.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            ok=False,
            status_code=status_code,
        ),
    )
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    with pytest.raises(HostedDispatchRejected):
        client.dispatch(identity())


@pytest.mark.parametrize("status_code", (None, True, 99, 300, 500, 502, 503, 600))
def test_github_client_keeps_non_4xx_dispatch_failures_ambiguous(
    monkeypatch,
    status_code,
):
    monkeypatch.setattr(
        "hosted_artifact_github.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            ok=False,
            status_code=status_code,
        ),
    )
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    with pytest.raises(HostedDispatchAmbiguous):
        client.dispatch(identity())


def test_github_client_keeps_transport_dispatch_failures_ambiguous(monkeypatch):
    def fail_transport(*args, **kwargs):
        raise requests.ConnectionError("connection lost after possible dispatch")

    monkeypatch.setattr("hosted_artifact_github.requests.post", fail_transport)
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    with pytest.raises(HostedDispatchAmbiguous):
        client.dispatch(identity())


def test_github_client_rejects_nested_and_plaintext_actions_archives(
    monkeypatch,
):
    listings = SimpleNamespace(
        ok=True,
        json=lambda: {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 21,
                    "name": identity().artifact_name,
                    "expired": False,
                    "archive_download_url": (
                        "https://api.github.com/repos/example-org/job-agent/"
                        "actions/artifacts/21/zip"
                    ),
                    "workflow_run": {"id": 90, "head_branch": "main"},
                }
            ],
        },
    )
    downloads = iter(
        (
            SimpleNamespace(
                ok=True,
                content=actions_archive(
                    "nested/application-artifacts.enc",
                    b"not an encrypted package",
                ),
            ),
            SimpleNamespace(
                ok=True,
                content=actions_archive(
                    "application-artifacts.enc",
                    b"not an encrypted package",
                ),
            ),
        )
    )

    def fake_get(url, **kwargs):
        if url.endswith("/actions/artifacts"):
            return listings
        return next(downloads)

    monkeypatch.setattr("hosted_artifact_github.requests.get", fake_get)
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    with pytest.raises(ValueError, match="archive is invalid"):
        client.package_for_run(identity(), workflow_run_id=90)
    with pytest.raises(ValueError, match="encrypted package"):
        client.package_for_run(identity(), workflow_run_id=90)


def test_github_client_correlates_workflow_scope_and_downloads_only_bound_run(
    monkeypatch, tmp_path
):
    encrypted_path = tmp_path / "hosted.enc"
    HostedArtifactHandoff(key=handoff_key(), authority=authority()).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted_path,
    )
    requests_seen = []

    def artifact(artifact_id, run_id):
        return {
            "id": artifact_id,
            "name": identity().artifact_name,
            "expired": False,
            "archive_download_url": (
                "https://api.github.com/repos/example-org/job-agent/"
                f"actions/artifacts/{artifact_id}/zip"
            ),
            "workflow_run": {"id": run_id, "head_branch": "main"},
        }

    other_identity = ArtifactHandoffIdentity(
        application_id="application:another-role",
        official_vacancy_version=sha256(b"another official vacancy"),
    )

    def fake_get(url, **kwargs):
        requests_seen.append((url, kwargs.get("params")))
        if url.endswith("/actions/workflows/run.yml/runs"):
            assert kwargs["params"]["branch"] == "main"
            assert kwargs["params"]["event"] == "repository_dispatch"
            return SimpleNamespace(
                ok=True,
                json=lambda: {
                    "total_count": 4,
                    "workflow_runs": [
                        {
                            "id": 81,
                            "event": "repository_dispatch",
                            "head_branch": "main",
                            "display_title": hosted_workflow_run_name(identity()),
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "id": 82,
                            "event": "repository_dispatch",
                            "head_branch": "main",
                            "display_title": hosted_workflow_run_name(identity()),
                            "status": "in_progress",
                            "conclusion": None,
                        },
                        {
                            "id": 83,
                            "event": "repository_dispatch",
                            "head_branch": "main",
                            "display_title": hosted_workflow_run_name(other_identity),
                            "status": "in_progress",
                            "conclusion": None,
                        },
                        {
                            "id": 84,
                            "event": "workflow_dispatch",
                            "head_branch": "main",
                            "display_title": hosted_workflow_run_name(identity()),
                            "status": "in_progress",
                            "conclusion": None,
                        },
                    ],
                },
            )
        if url.endswith("/actions/artifacts"):
            return SimpleNamespace(
                ok=True,
                json=lambda: {
                    "total_count": 2,
                    "artifacts": [artifact(701, 81), artifact(702, 82)],
                },
            )
        if url.endswith("/actions/artifacts/702/zip"):
            return SimpleNamespace(
                ok=True,
                content=actions_archive(
                    "application-artifacts.enc",
                    encrypted_path.read_bytes(),
                ),
            )
        raise AssertionError(f"unexpected or unbound download: {url}")

    monkeypatch.setattr("hosted_artifact_github.requests.get", fake_get)
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
        workflow="run.yml",
    )

    assert client.workflow_runs(
        identity(), exclude_run_ids=frozenset({81})
    ) == (HostedWorkflowRun(82, "in_progress", None),)
    assert client.package_for_run(
        identity(), workflow_run_id=82
    ) == encrypted_path.read_bytes()
    assert not any(url.endswith("/actions/artifacts/701/zip") for url, _ in requests_seen)


def test_same_identity_runs_remain_ambiguous_after_baseline(monkeypatch):
    def fake_get(url, **kwargs):
        assert url.endswith("/actions/workflows/run.yml/runs")
        return SimpleNamespace(
            ok=True,
            json=lambda: {
                "total_count": 3,
                "workflow_runs": [
                    {
                        "id": run_id,
                        "event": "repository_dispatch",
                        "head_branch": "main",
                        "display_title": hosted_workflow_run_name(identity()),
                        "status": "queued",
                        "conclusion": None,
                    }
                    for run_id in (31, 32, 33)
                ],
            },
        )

    monkeypatch.setattr("hosted_artifact_github.requests.get", fake_get)
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    assert client.workflow_runs(
        identity(),
        exclude_run_ids=frozenset({31}),
    ) == (
        HostedWorkflowRun(32, "queued", None),
        HostedWorkflowRun(33, "queued", None),
    )


@pytest.mark.parametrize("status", ("requested", "pending"))
def test_github_requested_and_pending_workflow_runs_remain_active(
    monkeypatch, status
):
    def fake_get(url, **kwargs):
        assert url.endswith("/actions/workflows/run.yml/runs")
        return SimpleNamespace(
            ok=True,
            json=lambda: {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 34,
                        "event": "repository_dispatch",
                        "head_branch": "main",
                        "display_title": hosted_workflow_run_name(identity()),
                        "status": status,
                        "conclusion": None,
                    }
                ],
            },
        )

    monkeypatch.setattr("hosted_artifact_github.requests.get", fake_get)
    client = GitHubHostedArtifactClient(
        repository="example-org/job-agent",
        token="github-token",
        branch="main",
    )

    assert client.workflow_runs(identity()) == (
        HostedWorkflowRun(34, status, None),
    )


def test_deep_grade_persists_the_exact_hosted_generation_snapshot(
    monkeypatch, tmp_path
):
    import discovery_jobs

    graded_job = {
        "stable_id": "example:research-role",
        "title": "Research Scientist",
        "official_description": "Build trustworthy computer-vision systems.",
        "official_vacancy_version": sha256(b"captured vacancy from deep grade"),
        "verification_status": "verified",
        "portfolio_evaluation": {
            "opportunity_id": "example:research-role",
            "vacancy_retrieved_at": "2026-07-24T10:00:00+00:00"
        },
        "requirements_evidence_matrix": {
            "version": "job-agent.requirements-evidence.v1",
            "official_vacancy_version": sha256(
                b"captured vacancy from deep grade"
            ),
            "rows": [
                {
                    "id": "req-python",
                    "requirement": "Python",
                    "importance": "required",
                    "status": "gap",
                    "evidence_ids": [],
                    "explanation": "No approved evidence selected.",
                }
            ],
        },
    }

    class Grader:
        def __init__(self, *, portfolio_policy):
            assert portfolio_policy == "configured-policy"

        def rank(self, jobs, limit):
            assert jobs == [{"stable_id": "example:research-role"}]
            return [graded_job]

    monkeypatch.setitem(
        sys.modules,
        "main",
        SimpleNamespace(
            ProductionPortfolioGrader=Grader,
            _load_portfolio_policy=lambda: "configured-policy",
        ),
    )
    monkeypatch.setattr(
        discovery_jobs.ShortlistArtifact,
        "read",
        lambda path: SimpleNamespace(),
    )
    monkeypatch.setattr(
        discovery_jobs,
        "_verified_shortlisted",
        lambda artifact: (
            SimpleNamespace(
                as_grading_job=lambda: {"stable_id": "example:research-role"}
            ),
        ),
    )
    store = HostedPreparationInputStore(tmp_path / "inputs")

    result = discovery_jobs.deep_grade(
        tmp_path / "shortlist.json",
        preparation_store=store,
    )

    assert result == [graded_job]
    assert store.load(graded_job["official_vacancy_version"]).stable_id == (
        "example:research-role"
    )


def test_local_tailoring_dispatches_and_reconciles_one_bound_run_across_calls(
    tmp_path,
):
    encrypted_path = tmp_path / "hosted.enc"
    HostedArtifactHandoff(key=handoff_key(), authority=authority()).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted_path,
    )

    client = async_client(encrypted_path)
    client.runs = (HostedWorkflowRun(17, "completed", "success"),)
    adapter = HostedTailoringAdapter(
        client=client,
        handoff=LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=tmp_path / "private-artifacts",
        ),
        transfer_root=tmp_path / "private-transfers",
        state_store=HostedTailoringStateStore(tmp_path / "handoff-state"),
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    )
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )

    with pytest.raises(HostedPreparationPending, match="dispatched"):
        adapter.prepare(
            identity().application_id,
            "prepare:intent",
            {"requirements_evidence_matrix": {"version": "unused by local"}},
            official,
        )

    assert client.dispatched == [identity()]
    client.runs = (
        HostedWorkflowRun(17, "completed", "success"),
        HostedWorkflowRun(18, "in_progress", None),
    )
    with pytest.raises(HostedPreparationPending, match="still active"):
        adapter.prepare(identity().application_id, "prepare:intent", {}, official)

    client.runs = (
        HostedWorkflowRun(17, "completed", "success"),
        HostedWorkflowRun(18, "completed", "success"),
    )
    client.packages[18] = encrypted_path.read_bytes()
    artifacts = adapter.prepare(
        identity().application_id, "prepare:intent", {}, official
    )

    assert adapter.verify_artifacts(artifacts) is True
    assert adapter.reload_master_cv() == "hosted-source-v1"
    assert list((tmp_path / "private-transfers").glob("*.enc")) == []


def test_local_tailoring_does_not_redispatch_after_an_ambiguous_crash(tmp_path):
    encrypted_path = tmp_path / "hosted.enc"
    HostedArtifactHandoff(key=handoff_key(), authority=authority()).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted_path,
    )
    calls = []

    class AmbiguousClient:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }

        def workflow_run_ids(self, value):
            return frozenset({11})

        def dispatch(self, value):
            calls.append("dispatch")
            raise RuntimeError("connection lost after possible dispatch")

        def workflow_runs(self, value, *, exclude_run_ids):
            raise AssertionError("first call must stop at ambiguous dispatch")

    state = HostedTailoringStateStore(tmp_path / "handoff-state")
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )
    first = HostedTailoringAdapter(
        client=AmbiguousClient(),
        handoff=LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=tmp_path / "private-artifacts",
        ),
        transfer_root=tmp_path / "private-transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    )

    with pytest.raises(HostedPreparationPending, match="ambiguous"):
        first.prepare(
            identity().application_id,
            "prepare:durable-intent",
            {},
            official,
        )

    class RecoveryClient:
        transport_scope = AmbiguousClient.transport_scope

        def dispatch(self, value):
            raise AssertionError("ambiguous dispatch must not be repeated")

        def workflow_runs(self, value, *, exclude_run_ids):
            assert exclude_run_ids == frozenset({11})
            return (HostedWorkflowRun(12, "completed", "success"),)

        def package_for_run(self, value, *, workflow_run_id):
            assert workflow_run_id == 12
            return encrypted_path.read_bytes()

    recovered = HostedTailoringAdapter(
        client=RecoveryClient(),
        handoff=LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=tmp_path / "private-artifacts",
        ),
        transfer_root=tmp_path / "private-transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    ).prepare(
        identity().application_id,
        "prepare:durable-intent",
        {},
        official,
    )

    assert calls == ["dispatch"]
    assert pathlib.Path(recovered.cv_path).is_file()


def test_pre_dispatch_state_is_private_and_durable_before_dispatch(tmp_path):
    state = HostedTailoringStateStore(tmp_path / "handoff-state")
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )

    class InspectingClient:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }

        def workflow_run_ids(self, value):
            return frozenset({31, 32})

        def dispatch(self, value):
            record = state.load("prepare:durable-before-dispatch", identity())
            assert record["phase"] == "dispatching"
            assert record["prior_workflow_run_ids"] == [31, 32]
            assert record["transport_scope"] == self.transport_scope
            assert record["dispatch_started_at"] is not None
            state_file = next((tmp_path / "handoff-state").glob("*.json"))
            assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
            raise RuntimeError("stop after durable observation")

    adapter = HostedTailoringAdapter(
        client=InspectingClient(),
        handoff=LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=tmp_path / "private-artifacts",
        ),
        transfer_root=tmp_path / "private-transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    )

    with pytest.raises(HostedPreparationPending, match="ambiguous"):
        adapter.prepare(
            identity().application_id,
            "prepare:durable-before-dispatch",
            {},
            official,
        )

    assert state.load("prepare:durable-before-dispatch", identity())["phase"] == (
        "ambiguous"
    )


def test_crash_after_dispatch_204_before_marker_only_reconciles_on_restart(
    tmp_path,
):
    encrypted_path = tmp_path / "hosted.enc"
    HostedArtifactHandoff(key=handoff_key(), authority=authority()).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted_path,
    )
    calls = []

    class CrashAfterAcceptedStore(HostedTailoringStateStore):
        def __init__(self, root):
            super().__init__(root)
            self.crash_once = True

        def save(self, value, expected_identity):
            if value.get("phase") == "dispatched" and self.crash_once:
                self.crash_once = False
                raise OSError("process died after GitHub returned 204")
            return super().save(value, expected_identity)

    class DispatchClient:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }

        def workflow_run_ids(self, value):
            return frozenset({51})

        def dispatch(self, value):
            calls.append("dispatch")

    state = CrashAfterAcceptedStore(tmp_path / "handoff-state")
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )
    adapter = HostedTailoringAdapter(
        client=DispatchClient(),
        handoff=LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=tmp_path / "private-artifacts",
        ),
        transfer_root=tmp_path / "private-transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    )

    with pytest.raises(OSError, match="returned 204"):
        adapter.prepare(
            identity().application_id,
            "prepare:crash-after-204",
            {},
            official,
        )
    assert state.load("prepare:crash-after-204", identity())["phase"] == (
        "dispatching"
    )

    class RecoveryClient:
        transport_scope = DispatchClient.transport_scope

        def dispatch(self, value):
            raise AssertionError("dispatching state must never redispatch")

        def workflow_runs(self, value, *, exclude_run_ids):
            assert exclude_run_ids == frozenset({51})
            return (HostedWorkflowRun(52, "completed", "success"),)

        def package_for_run(self, value, *, workflow_run_id):
            assert workflow_run_id == 52
            return encrypted_path.read_bytes()

    recovered = HostedTailoringAdapter(
        client=RecoveryClient(),
        handoff=LocalArtifactHandoff(
            key=handoff_key(),
            expected_authority=authority(),
            root=tmp_path / "private-artifacts",
        ),
        transfer_root=tmp_path / "private-transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    ).prepare(
        identity().application_id,
        "prepare:crash-after-204",
        {},
        official,
    )

    assert calls == ["dispatch"]
    assert pathlib.Path(recovered.cv_path).is_file()


def test_crash_before_dispatch_marker_keeps_prepared_state_safe_to_dispatch(
    tmp_path,
):
    class BaselineUnavailable:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }

        def workflow_run_ids(self, value):
            raise RuntimeError("read-only baseline unavailable")

    state = HostedTailoringStateStore(tmp_path / "handoff-state")
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )
    with pytest.raises(HostedPreparationPending, match="baseline"):
        HostedTailoringAdapter(
            client=BaselineUnavailable(),
            handoff=SimpleNamespace(),
            transfer_root=tmp_path / "private-transfers",
            state_store=state,
            source_version_loader=lambda: "hosted-source-v1",
            now=fixed_now,
        ).prepare(
            identity().application_id,
            "prepare:before-marker",
            {},
            official,
        )
    record = state.load("prepare:before-marker", identity())
    assert record["phase"] == "prepared"
    assert record["baseline_captured"] is False


def test_definitively_rejected_dispatch_is_failed_without_automatic_retry(
    tmp_path,
):
    class RejectedClient:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }
        dispatches = 0

        def workflow_run_ids(self, value):
            return frozenset()

        def dispatch(self, value):
            self.dispatches += 1
            raise HostedDispatchRejected("GitHub rejected dispatch")

        def workflow_runs(self, value, *, exclude_run_ids):
            del value
            assert exclude_run_ids == frozenset()
            return ()

    client = RejectedClient()
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )
    adapter = HostedTailoringAdapter(
        client=client,
        handoff=SimpleNamespace(),
        transfer_root=tmp_path / "private-transfers",
        state_store=HostedTailoringStateStore(tmp_path / "handoff-state"),
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    )

    with pytest.raises(HostedPreparationFailed, match="rejected"):
        adapter.prepare(
            identity().application_id,
            "prepare:rejected",
            {},
            official,
        )
    with pytest.raises(HostedPreparationFailed, match="rejected"):
        adapter.prepare(
            identity().application_id,
            "prepare:rejected",
            {},
            official,
        )
    resolution = adapter.preparation_resolution(
        identity().application_id,
        "prepare:rejected",
        official,
    )
    assert resolution is not None
    assert resolution.retry_safe is True
    assert resolution.reason == "hosted dispatch was definitively rejected"
    assert client.dispatches == 1


def test_hosted_tailoring_rejects_noncanonical_intent_ids(tmp_path):
    state = HostedTailoringStateStore(tmp_path / "handoff-state")
    with pytest.raises(ValueError, match="canonical"):
        state.load(" prepare:intent ", identity())


def test_completed_intent_rejects_artifacts_outside_the_private_install_root(
    tmp_path,
):
    encrypted_path = tmp_path / "hosted.enc"
    HostedArtifactHandoff(key=handoff_key(), authority=authority()).export(
        identity=identity(),
        artifacts=prepared_artifacts(tmp_path),
        destination=encrypted_path,
    )
    handoff = LocalArtifactHandoff(
        key=handoff_key(),
        expected_authority=authority(),
        root=tmp_path / "private-artifacts",
    )
    installed = handoff.install(encrypted_path, expected_identity=identity())
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_cv = outside / "cv.pdf"
    outside_cover = outside / "cover-letter.pdf"
    outside_cv.write_bytes(pathlib.Path(installed.cv_path).read_bytes())
    outside_cover.write_bytes(pathlib.Path(installed.cover_letter_path).read_bytes())
    state = HostedTailoringStateStore(tmp_path / "handoff-state")
    state.save(
        {
            "version": HOSTED_TAILORING_STATE_VERSION,
            "intent_id": "prepare:completed",
            "identity": identity().to_dict(),
            "phase": "completed",
            "transport_scope": {
                "workflow": "run.yml",
                "branch": "main",
                "event": "repository_dispatch",
            },
            "prepared_at": fixed_now().isoformat(),
            "dispatch_started_at": fixed_now().isoformat(),
            "dispatch_accepted_at": fixed_now().isoformat(),
            "run_discovery_deadline": (
                fixed_now() + timedelta(minutes=10)
            ).isoformat(),
            "baseline_captured": True,
            "prior_workflow_run_ids": [17],
            "workflow_run_id": 18,
            "failure_reason": None,
            "artifacts": {
                **asdict(installed),
                "cv_path": str(outside_cv),
                "cover_letter_path": str(outside_cover),
            },
        },
        identity(),
    )
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )

    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"completed intent must not call {name}")

    adapter = HostedTailoringAdapter(
        client=NoCalls(),
        handoff=handoff,
        transfer_root=tmp_path / "private-transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
    )

    with pytest.raises(RuntimeError, match="no longer intact"):
        adapter.prepare(
            identity().application_id,
            "prepare:completed",
            {},
            official,
        )


def test_zero_or_multiple_post_dispatch_runs_require_explicit_resolution(tmp_path):
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )

    for suffix, runs, reason in (
        ("zero", (), "no workflow run"),
        (
            "many",
            (
                HostedWorkflowRun(41, "queued", None),
                HostedWorkflowRun(42, "queued", None),
            ),
            "multiple workflow runs",
        ),
    ):
        root = tmp_path / suffix
        root.mkdir()
        current = [fixed_now()]
        client = async_client(root / "unused.enc")
        adapter = HostedTailoringAdapter(
            client=client,
            handoff=LocalArtifactHandoff(
                key=handoff_key(),
                expected_authority=authority(),
                root=root / "private-artifacts",
            ),
            transfer_root=root / "private-transfers",
            state_store=HostedTailoringStateStore(root / "handoff-state"),
            source_version_loader=lambda: "hosted-source-v1",
            now=lambda: current[0],
            run_discovery_timeout=timedelta(seconds=1),
        )
        with pytest.raises(HostedPreparationPending):
            adapter.prepare(
                identity().application_id,
                f"prepare:{suffix}",
                {},
                official,
            )
        client.runs = runs
        if suffix == "zero":
            current[0] += timedelta(seconds=2)
        with pytest.raises(HostedPreparationResolutionRequired, match=reason):
            adapter.prepare(
                identity().application_id,
                f"prepare:{suffix}",
                {},
                official,
            )
        with pytest.raises(HostedPreparationResolutionRequired, match=reason):
            adapter.prepare(
                identity().application_id,
                f"prepare:{suffix}",
                {},
                official,
            )
        resolution = adapter.preparation_resolution(
            identity().application_id,
            f"prepare:{suffix}",
            official,
        )
        assert resolution is not None
        assert resolution.retry_safe is (suffix == "zero")
        assert len(client.dispatched) == 1


def test_retry_evidence_fails_closed_for_active_success_or_ambiguous_bound_runs(
    tmp_path,
):
    official = OfficialVacancy(
        version=identity().official_vacancy_version,
        fingerprint=identity().official_vacancy_version,
        freshness="2026-07-24T10:00:00+00:00",
        description="Build trustworthy computer-vision systems.",
    )
    state = HostedTailoringStateStore(tmp_path / "handoff-state")
    state.save(
        {
            "version": HOSTED_TAILORING_STATE_VERSION,
            "intent_id": "prepare:bound",
            "identity": identity().to_dict(),
            "phase": "failed",
            "transport_scope": {
                "workflow": "run.yml",
                "branch": "main",
                "event": "repository_dispatch",
            },
            "prepared_at": fixed_now().isoformat(),
            "dispatch_started_at": fixed_now().isoformat(),
            "dispatch_accepted_at": fixed_now().isoformat(),
            "run_discovery_deadline": (
                fixed_now() + timedelta(minutes=10)
            ).isoformat(),
            "baseline_captured": True,
            "prior_workflow_run_ids": [80],
            "workflow_run_id": 81,
            "failure_reason": "bound workflow run concluded as failure",
            "artifacts": None,
        },
        identity(),
    )

    class Client:
        transport_scope = {
            "workflow": "run.yml",
            "branch": "main",
            "event": "repository_dispatch",
        }
        run = HostedWorkflowRun(81, "in_progress", None)
        package = None

        def workflow_runs(self, value, *, exclude_run_ids):
            del value
            assert exclude_run_ids == frozenset({80})
            return (self.run,)

        def package_for_run(self, value, *, workflow_run_id):
            del value
            assert workflow_run_id == 81
            return self.package

    client = Client()
    adapter = HostedTailoringAdapter(
        client=client,
        handoff=SimpleNamespace(),
        transfer_root=tmp_path / "transfers",
        state_store=state,
        source_version_loader=lambda: "hosted-source-v1",
        now=fixed_now,
    )

    assert adapter.preparation_resolution(
        identity().application_id, "prepare:bound", official
    ).retry_safe is False
    client.run = HostedWorkflowRun(81, "completed", "success")
    assert adapter.preparation_resolution(
        identity().application_id, "prepare:bound", official
    ).retry_safe is False
    client.run = HostedWorkflowRun(81, "completed", "failure")
    client.package = b"possibly-valid-package"
    assert adapter.preparation_resolution(
        identity().application_id, "prepare:bound", official
    ).retry_safe is False
    client.package = None
    assert adapter.preparation_resolution(
        identity().application_id, "prepare:bound", official
    ).retry_safe is True


def test_hosted_preparation_rejects_application_id_not_bound_to_snapshot(
    tmp_path,
):
    vacancy_version = identity().official_vacancy_version
    store = HostedPreparationInputStore(tmp_path / "inputs")
    store.save(
        HostedPreparationInput(
            stable_id="example:research-role",
            official_vacancy=OfficialVacancy(
                version=vacancy_version,
                fingerprint=vacancy_version,
                freshness="2026-07-24T10:00:00+00:00",
                description="Build reproducible computer-vision systems.",
            ),
            opportunity={
                "artifact_family": "research",
                "requirements_evidence_matrix": {
                    "version": "job-agent.requirements-evidence.v1",
                    "official_vacancy_version": vacancy_version,
                    "rows": [
                        {
                            "id": "req-python",
                            "requirement": "Python",
                            "importance": "required",
                            "status": "gap",
                            "evidence_ids": [],
                            "explanation": "No approved evidence selected.",
                        }
                    ],
                },
            },
        )
    )
    service = HostedArtifactPreparationService.__new__(
        HostedArtifactPreparationService
    )
    service._inputs = store

    with pytest.raises(ValueError, match="application identity"):
        service.prepare(
            identity=ArtifactHandoffIdentity(
                application_id="approved-0000000000000000",
                official_vacancy_version=vacancy_version,
            ),
            destination=tmp_path / "never.enc",
        )
