import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from requirements_evidence import MatrixContractError, RequirementsEvidenceMatrix


def canonical():
    return {
        "version": "job-agent.requirements-evidence.v1",
        "official_vacancy_version": "vacancy-v1",
        "rows": [
            {
                "id": "req.python",
                "requirement": "Python",
                "importance": "required",
                "status": "matched",
                "evidence_ids": ["evidence.python"],
                "explanation": "Approved Python evidence.",
            }
        ],
    }


def test_codec_writes_canonical_rows_and_reads_legacy_requirements():
    canonical_matrix = RequirementsEvidenceMatrix.from_dict(canonical())
    legacy_matrix = RequirementsEvidenceMatrix.from_dict(
        {
            "version": "job-agent.requirements-evidence.v1",
            "official_vacancy_version": "vacancy-v1",
            "requirements": [
                {
                    "requirement_id": "req.python",
                    "requirement": "Python",
                    "importance": "required",
                    "status": "supported",
                    "evidence_ids": ["evidence.python"],
                    "explanation": "Approved Python evidence.",
                }
            ],
        }
    )

    assert legacy_matrix == canonical_matrix
    assert legacy_matrix.to_dict() == canonical()
    assert legacy_matrix.report_projection() == tuple(canonical()["rows"])


def test_content_digest_is_stable_and_validates_evidence_and_requirements():
    matrix = RequirementsEvidenceMatrix.from_dict(canonical())

    assert matrix.content_digest == RequirementsEvidenceMatrix.from_dict(
        canonical()
    ).content_digest
    matrix.validate_evidence_ids({"evidence.python"})
    matrix.validate_official_requirements({"Python"})

    with pytest.raises(MatrixContractError, match="unknown professional evidence"):
        matrix.validate_evidence_ids(set())
    with pytest.raises(MatrixContractError, match="omits an official requirement"):
        matrix.validate_official_requirements({"Python", "Kubernetes"})
