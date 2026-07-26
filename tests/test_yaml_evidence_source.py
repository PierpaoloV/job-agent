import pathlib
import sys

import pytest
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import ArtifactFamily, EvidenceKind  # noqa: E402
from yaml_evidence_source import YamlEvidenceSource  # noqa: E402


def test_loads_a_versioned_family_scoped_snapshot_without_exposing_other_profile_data(
    tmp_path,
):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canonical_cv.write_bytes(b"verified canonical CV bytes")
    evidence_path = tmp_path / "evidence.yaml"
    evidence_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "candidate": "Private Candidate Name",
                "identity": {"date_of_birth": "must not be loaded"},
                "highlights": [
                    {
                        "id": "research-impact",
                        "claim": "Built a patient-level research pipeline.",
                        "evidence": "master-cv:research",
                        "kind": "impact",
                        "suitable_for": ["research", "applied_ml"],
                    }
                ],
                "skill_evidence": [
                    {
                        "id": "python-skill",
                        "claim": "Uses Python for reproducible ML workflows.",
                        "evidence": "master-cv:skills",
                        "kind": "skill",
                        "suitable_for": ["research", "applied_ml", "agentic_ai"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    snapshot = YamlEvidenceSource(evidence_path, canonical_cv).load()

    assert snapshot.version.startswith("sha256:")
    assert snapshot.canonical_cv_version.startswith("sha256:")
    assert [(item.evidence_id, item.kinds) for item in snapshot.evidence] == [
        ("research-impact", (EvidenceKind.IMPACT,)),
        ("python-skill", (EvidenceKind.SKILL,)),
    ]
    assert snapshot.evidence[0].families == (
        ArtifactFamily.RESEARCH,
        ArtifactFamily.CV_APPLIED_ML,
    )
    assert "date_of_birth" not in repr(snapshot)
    assert "Private Candidate Name" not in repr(snapshot)


def test_rejects_non_boolean_approval_and_excludes_unapproved_evidence(tmp_path):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canonical_cv.write_bytes(b"verified canonical CV bytes")
    evidence_path = tmp_path / "evidence.yaml"
    evidence_path.write_text(
        yaml.safe_dump(
            {
                "highlights": [
                    {
                        "id": "not-approved",
                        "claim": "Do not tailor with this claim.",
                        "evidence": "private-note",
                        "kind": "experience",
                        "suitable_for": ["research"],
                        "approved": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert YamlEvidenceSource(evidence_path, canonical_cv).load().evidence == ()

    evidence_path.write_text(
        yaml.safe_dump(
            {
                "highlights": [
                    {
                        "id": "ambiguous-approval",
                        "claim": "Must not become approved through truthiness.",
                        "evidence": "private-note",
                        "kind": "experience",
                        "suitable_for": ["research"],
                        "approved": "false",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="approved must be a YAML boolean"):
        YamlEvidenceSource(evidence_path, canonical_cv).load()
