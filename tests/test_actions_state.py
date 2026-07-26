from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import sys
import zipfile

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import actions_state
from actions_state import StateBundle
from application_domain import OfficialVacancy
from application_identity import approved_application_id
from hosted_artifact_preparation import (
    HostedPreparationInput,
    HostedPreparationInputStore,
)
from telegram_delivery import TelegramDeliveryLedger


def test_state_bundle_manifest_is_versioned_and_hash_validated(tmp_path):
    schedule = tmp_path / "data" / "discovery-schedule.json"
    schedule.parent.mkdir()
    schedule.write_text(json.dumps({"version": "job-agent.discovery-schedule.v1"}))
    deliveries = tmp_path / "data" / "telegram-deliveries.sqlite"
    TelegramDeliveryLedger(deliveries)
    bundle = StateBundle(tmp_path)

    manifest = bundle.write_manifest()

    assert manifest["version"] == "job-agent.actions-state.v1"
    assert set(manifest["files"]) == {
        "data/discovery-schedule.json",
        "data/telegram-deliveries.sqlite",
    }
    bundle.validate_manifest()


def test_state_bundle_preserves_versioned_cloud_opportunity_discards(tmp_path):
    decisions = tmp_path / "data" / "opportunity-decisions.json"
    decisions.parent.mkdir()
    decisions.write_text(
        json.dumps(
            {
                "version": "job-agent.opportunity-decisions.v2",
                "discards": {},
            }
        )
    )

    bundle = StateBundle(tmp_path)
    manifest = bundle.write_manifest()

    assert "data/opportunity-decisions.json" in manifest["files"]
    bundle.validate_manifest()


def test_decision_run_can_publish_a_versioned_authoritative_manifest(tmp_path):
    schedule = tmp_path / "data" / "discovery-schedule.json"
    schedule.parent.mkdir()
    schedule.write_text(
        json.dumps({"version": "job-agent.discovery-schedule.v1"})
    )

    result = actions_state.main(
        [
            "write-manifest",
            "--root",
            str(tmp_path),
            "--workflow",
            "run.yml",
            "--branch",
            "main",
            "--stage",
            "decision",
        ]
    )

    assert result == 0
    manifest = json.loads(
        (tmp_path / "data" / "actions-state" / "manifest.json").read_text()
    )
    assert manifest["authority"]["stage"] == "decision"


def test_state_bundle_rejects_tampering_and_unknown_manifest_versions(tmp_path):
    schedule = tmp_path / "data" / "discovery-schedule.json"
    schedule.parent.mkdir()
    schedule.write_text(json.dumps({"version": "job-agent.discovery-schedule.v1"}))
    bundle = StateBundle(tmp_path)
    bundle.write_manifest({
        "repository": "example-org/job-agent",
        "workflow": "run.yml",
        "branch": "main",
        "run_id": 10,
        "run_attempt": 1,
        "stage": "deep",
    })
    staged = bundle.package_dir / "files" / "data" / "discovery-schedule.json"
    staged.write_text("tampered")

    with pytest.raises(ValueError, match="hash mismatch"):
        bundle.validate_manifest()

    manifest = json.loads(bundle.manifest_path.read_text())
    manifest["version"] = "job-agent.actions-state.v999"
    bundle.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsupported Actions state version"):
        bundle.validate_manifest()


def test_state_bundle_rejects_sensitive_nested_shortlist_data(tmp_path):
    pending = tmp_path / "data" / "pending-shortlist.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(
        json.dumps(
            {
                "version": "job-agent.shortlist.v1",
                "created_at": "2026-07-16T10:00:00+00:00",
                "opportunities": [
                    {
                        "stable_id": "linkedin:42",
                        "discovered_at": "2026-07-16T10:00:00+00:00",
                        "source_confidence": "supported",
                        "local_score": 0.9,
                        "screening_reasons": ["fit"],
                        "shortlisted": True,
                        "screening_outcome": "needs_local_fetch",
                        "screening_features": {
                            "public": [{"credential": "nested-secret"}]
                        },
                        "job": {
                            "dedup_key": "linkedin:42",
                            "verification_status": "needs_local_fetch",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="candidate-sensitive data"):
        StateBundle(tmp_path).write_manifest()


def test_state_bundle_rejects_identity_documents_in_shortlist_job_data(tmp_path):
    pending = tmp_path / "data" / "pending-shortlist.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(
        json.dumps(
            {
                "version": "job-agent.shortlist.v1",
                "created_at": "2026-07-16T10:00:00+00:00",
                "opportunities": [
                    {
                        "stable_id": "linkedin:42",
                        "discovered_at": "2026-07-16T10:00:00+00:00",
                        "source_confidence": "supported",
                        "local_score": 0.9,
                        "screening_reasons": ["fit"],
                        "shortlisted": True,
                        "screening_outcome": "needs_local_fetch",
                        "screening_features": {},
                        "job": {
                            "dedup_key": "linkedin:42",
                            "verification_status": "needs_local_fetch",
                            "identity_document": "passport-scan.pdf",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="candidate-sensitive data"):
        StateBundle(tmp_path).write_manifest()


def test_state_bundle_rejects_unvalidated_deep_grade_json(tmp_path):
    grade = tmp_path / "data" / "deep-grades" / "arbitrary.json"
    grade.parent.mkdir(parents=True)
    grade.write_text(
        json.dumps(
            {
                "schema_version": "job-agent.deep-grade.v1",
                "candidate_profile": {"diagnosis": "private"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid deep-grade artifact"):
        StateBundle(tmp_path).write_manifest()


def test_state_bundle_preserves_and_validates_hosted_preparation_inputs(tmp_path):
    vacancy_version = "sha256:" + "a" * 64
    HostedPreparationInputStore(
        tmp_path / "data" / "hosted-preparation-inputs"
    ).save(
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

    bundle = StateBundle(tmp_path)
    manifest = bundle.write_manifest()

    preparation_paths = [
        path
        for path in manifest["files"]
        if path.startswith("data/hosted-preparation-inputs/")
    ]
    assert len(preparation_paths) == 1
    bundle.validate_manifest()

    staged = bundle.package_dir / "files" / preparation_paths[0]
    payload = json.loads(staged.read_text(encoding="utf-8"))
    payload["official_vacancy"]["version"] = "sha256:" + "b" * 64
    staged.write_text(json.dumps(payload), encoding="utf-8")
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    manifest["files"][preparation_paths[0]] = actions_state._file_hash(staged)
    manifest["manifest_digest"] = actions_state._manifest_digest(manifest)
    bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="hosted preparation input"):
        bundle.validate_manifest()


def test_state_bundle_keeps_distinct_roles_with_the_same_vacancy_hash(tmp_path):
    vacancy_version = "sha256:" + "e" * 64
    root = tmp_path / "data" / "hosted-preparation-inputs"
    store = HostedPreparationInputStore(root)
    for stable_id in ("example:first-role", "example:second-role"):
        store.save(
            HostedPreparationInput(
                stable_id=stable_id,
                official_vacancy=OfficialVacancy(
                    version=vacancy_version,
                    fingerprint=vacancy_version,
                    freshness="2026-07-24T10:00:00+00:00",
                    description="Shared source text published for two distinct roles.",
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

    bundle = StateBundle(tmp_path)
    manifest = bundle.write_manifest()

    preparation_paths = tuple(
        path
        for path in manifest["files"]
        if path.startswith("data/hosted-preparation-inputs/")
    )
    assert len(preparation_paths) == 2
    bundle.validate_manifest()
    assert {
        store.load(
            approved_application_id(stable_id, vacancy_version),
            vacancy_version,
        ).stable_id
        for stable_id in ("example:first-role", "example:second-role")
    } == {"example:first-role", "example:second-role"}


def test_state_bundle_rejects_candidate_fields_in_hosted_preparation_input(tmp_path):
    vacancy_version = "sha256:" + "a" * 64
    root = tmp_path / "data" / "hosted-preparation-inputs"
    root.mkdir(parents=True)
    path = root / (
        actions_state.sha256(vacancy_version.encode("utf-8")).hexdigest()
        + ".json"
    )
    payload = HostedPreparationInput(
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
    ).to_dict()
    payload["opportunity"]["diagnosis"] = "must not enter Actions state"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hosted preparation input"):
        StateBundle(tmp_path).write_manifest()


def test_latest_immutable_artifact_restores_authoritative_state(monkeypatch, tmp_path):
    source = tmp_path / "source"
    vacancy_version = "sha256:" + "d" * 64
    schedule = source / "data" / "discovery-schedule.json"
    schedule.parent.mkdir(parents=True)
    schedule.write_text(
        json.dumps({"version": "job-agent.discovery-schedule.v1", "roles": {}})
    )
    HostedPreparationInputStore(
        source / "data" / "hosted-preparation-inputs"
    ).save(
        HostedPreparationInput(
            stable_id="example:restored-role",
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
    bundle = StateBundle(source)
    bundle.write_manifest({
        "repository": "example-org/job-agent",
        "workflow": "run.yml",
        "branch": "main",
        "run_id": 10,
        "run_attempt": 1,
        "stage": "deep",
    })
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        for path in bundle.package_dir.rglob("*"):
            if path.is_file():
                output.write(path, path.relative_to(bundle.package_dir))
    monkeypatch.setattr(
        actions_state.GitHubActionsStateClient,
        "latest_archive",
        lambda self: archive.getvalue(),
    )
    target = tmp_path / "target"
    stale = target / "data" / "telegram-deliveries.sqlite"
    TelegramDeliveryLedger(stale)

    restored = actions_state.restore_latest(
        root=target, repository="example-org/job-agent", token="test-token"
    )

    assert restored is True
    assert json.loads(
        (target / "data" / "discovery-schedule.json").read_text()
    )["version"] == "job-agent.discovery-schedule.v1"
    assert HostedPreparationInputStore(
        target / "data" / "hosted-preparation-inputs"
    ).load(vacancy_version).stable_id == "example:restored-role"
    assert not stale.exists()


def test_state_install_rejects_regression_and_unrelated_parent_lineage(tmp_path):
    authority = {
        "repository": "example-org/job-agent",
        "workflow": "run.yml",
        "branch": "main",
        "run_id": 10,
        "run_attempt": 1,
        "stage": "deep",
    }
    first = tmp_path / "first"
    schedule = first / "data" / "discovery-schedule.json"
    schedule.parent.mkdir(parents=True)
    schedule.write_text(json.dumps({"version": "job-agent.discovery-schedule.v1"}))
    first_bundle = StateBundle(first)
    first_bundle.write_manifest(authority)
    target = tmp_path / "target"
    StateBundle(target).install_package(first_bundle.package_dir)

    regression = tmp_path / "regression"
    reg_schedule = regression / "data" / "discovery-schedule.json"
    reg_schedule.parent.mkdir(parents=True)
    reg_schedule.write_text(json.dumps({"version": "job-agent.discovery-schedule.v1"}))
    reg_bundle = StateBundle(regression)
    reg_bundle.write_manifest({**authority, "run_id": 9})
    with pytest.raises(ValueError, match="not monotonic"):
        StateBundle(target).install_package(reg_bundle.package_dir)

    unrelated = tmp_path / "unrelated"
    unrelated_schedule = unrelated / "data" / "discovery-schedule.json"
    unrelated_schedule.parent.mkdir(parents=True)
    unrelated_schedule.write_text(
        json.dumps({"version": "job-agent.discovery-schedule.v1"})
    )
    unrelated_bundle = StateBundle(unrelated)
    unrelated_bundle.write_manifest({**authority, "run_id": 11})
    with pytest.raises(ValueError, match="parent lineage"):
        StateBundle(target).install_package(unrelated_bundle.package_dir)


def test_artifact_listing_prefers_newer_run_id_on_default_branch(monkeypatch):
    calls = []

    class Response:
        ok = True

        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/actions/artifacts"):
            return Response({"artifacts": [
                {
                    "name": "discovery-state-deep-feature",
                    "created_at": "2026-07-17T12:00:00Z",
                    "expired": False,
                    "archive_download_url": "https://download/feature",
                    "workflow_run": {"head_branch": "feature", "id": 99},
                },
                {
                    "name": "discovery-state-deep-main-old-run-rerun",
                    "created_at": "2026-07-18T11:00:00Z",
                    "expired": False,
                    "archive_download_url": "https://download/old-main",
                    "workflow_run": {"head_branch": "main", "id": 40},
                },
                {
                    "name": "discovery-state-deep-main-new-run",
                    "created_at": "2026-07-17T11:00:00Z",
                    "expired": False,
                    "archive_download_url": "https://download/new-main",
                    "workflow_run": {"head_branch": "main", "id": 41},
                },
            ]})
        return Response(content=b"main-state")

    monkeypatch.setattr(actions_state.requests, "get", fake_get)
    client = actions_state.GitHubActionsStateClient(
        repository="example-org/job-agent", token="test", branch="main"
    )

    assert client.latest_archive() == b"main-state"
    assert calls[-1] == "https://download/new-main"
