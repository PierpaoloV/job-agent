from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from application_domain import OfficialVacancy
from application_identity import approved_application_id
from hosted_artifact_preparation import (
    HostedPreparationInput,
    HostedPreparationInputStore,
)


VACANCY_VERSION = "sha256:" + "a" * 64


def _matrix(version: str = VACANCY_VERSION) -> dict[str, object]:
    return {
        "version": "job-agent.requirements-evidence.v1",
        "official_vacancy_version": version,
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
    }


def _snapshot(**opportunity: object) -> HostedPreparationInput:
    return HostedPreparationInput(
        stable_id="example:research-role",
        official_vacancy=OfficialVacancy(
            version=VACANCY_VERSION,
            fingerprint=VACANCY_VERSION,
            freshness="2026-07-24T10:00:00+00:00",
            description="Build reproducible computer-vision systems.",
        ),
        opportunity={
            "artifact_family": "research",
            "requirements_evidence_matrix": _matrix(),
            **opportunity,
        },
    )


def _graded_job(**overrides: object) -> dict[str, object]:
    evaluation = {
        "schema_version": "job-agent.deep-grade.v1",
        "opportunity_id": "example:research-role",
        "vacancy_retrieved_at": "2026-07-24T10:00:00+00:00",
        "grading_input_fingerprint": "sha256:" + "b" * 64,
        "requirements_evidence_matrix": _matrix(),
    }
    # capture_graded only needs the identity-bearing subset of the canonical
    # evaluation; unrelated score fields are intentionally omitted here.
    return {
        "stable_id": "example:research-role",
        "title": "Research Scientist",
        "official_description": "Build reproducible computer-vision systems.",
        "official_vacancy_version": VACANCY_VERSION,
        "verification_status": "verified",
        "retrieved_at": "2026-07-24T10:00:00+00:00",
        "portfolio_evaluation": evaluation,
        "requirements_evidence_matrix": _matrix(),
        **overrides,
    }


def test_approved_application_identity_rejects_noncanonical_inputs():
    with pytest.raises(ValueError, match="canonical"):
        approved_application_id(" example:research-role ", VACANCY_VERSION)

    with pytest.raises(ValueError, match="sha256"):
        approved_application_id("example:research-role", "latest")


def test_hosted_snapshot_rejects_non_allowlisted_candidate_fields():
    with pytest.raises(ValueError, match="candidate-safe"):
        _snapshot(diagnosis="must not cross the hosted boundary")


def test_hosted_snapshot_load_rejects_noncanonical_outer_fields(tmp_path):
    store = HostedPreparationInputStore(tmp_path)
    store.save(_snapshot())
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["candidate_profile"] = {"health": "private"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical"):
        store.load(VACANCY_VERSION)


def test_capture_rejects_a_grade_bound_to_another_opportunity(tmp_path):
    store = HostedPreparationInputStore(tmp_path)
    job = _graded_job()
    job["portfolio_evaluation"]["opportunity_id"] = "example:other-role"

    with pytest.raises(ValueError, match="opportunity"):
        store.capture_graded([job])


def test_capture_rejects_a_noncanonical_or_mismatched_vacancy_version(tmp_path):
    store = HostedPreparationInputStore(tmp_path)

    with pytest.raises(ValueError, match="sha256"):
        store.capture_graded(
            [_graded_job(official_vacancy_version="latest")]
        )

    job = _graded_job()
    job["requirements_evidence_matrix"] = _matrix("sha256:" + "c" * 64)
    with pytest.raises(ValueError, match="matrix"):
        store.capture_graded([job])


def test_two_roles_with_the_same_vacancy_hash_resolve_by_full_application_identity(
    tmp_path,
):
    store = HostedPreparationInputStore(tmp_path)
    first = _snapshot()
    second = HostedPreparationInput(
        stable_id="example:second-research-role",
        official_vacancy=first.official_vacancy,
        opportunity=first.opportunity,
    )

    store.save(first)
    store.save(second)

    first_application_id = approved_application_id(
        first.stable_id, first.official_vacancy.version
    )
    second_application_id = approved_application_id(
        second.stable_id, second.official_vacancy.version
    )
    assert store.load(
        first_application_id, VACANCY_VERSION
    ).stable_id == first.stable_id
    assert store.load(
        second_application_id, VACANCY_VERSION
    ).stable_id == second.stable_id
    assert len(tuple(tmp_path.glob("*.json"))) == 2
    with pytest.raises(RuntimeError, match="ambiguous"):
        store.load(VACANCY_VERSION)
