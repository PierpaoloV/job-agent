import pathlib
import sys

import pytest
from reportlab.pdfgen.canvas import Canvas
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import ArtifactFamily, EvidenceKind  # noqa: E402
from yaml_evidence_source import (  # noqa: E402
    YamlEvidenceSource,
    _professional_cv_projection,
)


def write_canonical_cv(path):
    canvas = Canvas(str(path))
    canvas.drawString(50, 800, "Verified canonical CV text")
    canvas.save()


def test_loads_a_versioned_family_scoped_snapshot_without_exposing_other_profile_data(
    tmp_path,
):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    write_canonical_cv(canonical_cv)
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
    assert snapshot.canonical_cv_text == "Verified canonical CV text"
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
    write_canonical_cv(canonical_cv)
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


def test_canonical_cv_projection_excludes_sensitive_profile_lines(tmp_path):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canvas = Canvas(str(canonical_cv))
    for y, line in (
        (800, "Synthetic Candidate"),
        (780, "synthetic@example.com"),
        (760, "Italian citizen based in the Netherlands"),
        (740, "Date of birth: 1990-01-01"),
        (720, "Private key: TOP-SECRET"),
        (700, "Client secret: client-secret-value"),
        (680, "Refresh token: refresh-token-value"),
        (660, "Veteran status: protected veteran"),
        (640, "Salary expectation: EUR 120000"),
        (620, "Hobbies"),
        (600, "Political campaigning"),
        (580, "Professional Experience"),
        (560, "Machine Learning Researcher"),
    ):
        canvas.drawString(50, y, line)
    canvas.save()
    evidence_path = tmp_path / "evidence.yaml"
    evidence_path.write_text("highlights: []\nskill_evidence: []\n", encoding="utf-8")

    projected = YamlEvidenceSource(evidence_path, canonical_cv).load().canonical_cv_text

    assert "Synthetic Candidate" in projected
    assert "synthetic@example.com" in projected
    assert "Professional Experience" in projected
    assert "citizen" not in projected.casefold()
    assert "date of birth" not in projected.casefold()
    assert "top-secret" not in projected.casefold()
    assert "client-secret-value" not in projected.casefold()
    assert "refresh-token-value" not in projected.casefold()
    assert "protected veteran" not in projected.casefold()
    assert "eur 120000" not in projected.casefold()
    assert "political campaigning" not in projected.casefold()


def test_structured_projection_drops_every_unallowlisted_layout_block():
    projected = _professional_cv_projection(
        (
            "Synthetic Candidate",
            "Applied AI Researcher",
            "synthetic@example.com",
            "Professional Profile\nI build and validate trustworthy machine-learning research systems.",
            "Expected remuneration: EUR 120000",
            "Right to work: unrestricted",
            "Family status: married",
            "Unrecognized Personal Section\nPrivate personal narrative",
            "Professional Experience",
            "Machine Learning Researcher\n2022 - Present",
            "Example Institute\nAmsterdam\n• Built reproducible research systems.",
        )
    )

    assert "Synthetic Candidate" in projected
    assert "Professional Profile" in projected
    assert "Machine Learning Researcher" in projected
    assert "Built reproducible research systems" in projected
    assert "remuneration" not in projected.casefold()
    assert "right to work" not in projected.casefold()
    assert "family status" not in projected.casefold()
    assert "private personal narrative" not in projected.casefold()


def test_header_contact_is_kept_when_pdf_layout_places_it_in_its_own_block():
    projected = _professional_cv_projection(
        (
            "Synthetic Candidate",
            "Applied AI Researcher",
            "synthetic93@example.com | +39 371 198 8078 | example.com/profile",
            "Professional Profile\nI build trustworthy machine-learning systems.",
        )
    )

    assert "synthetic93@example.com" in projected
    assert "+39 371 198 8078" in projected
    assert "example.com/profile" in projected


def test_section_heading_block_can_also_contain_the_first_education_header():
    projected = _professional_cv_projection(
        (
            "Synthetic Candidate",
            "Education\nPhD in Artificial Intelligence\n2018 - 2022",
            "Example University\nAmsterdam\n• Defended a source-verified dissertation.",
            "Peer-Reviewed Publications and Proceedings",
        )
    )

    assert "Education" in projected
    assert "PhD in Artificial Intelligence" in projected
    assert "Example University" in projected
    assert "Amsterdam" in projected


def test_single_layout_block_does_not_fall_back_to_permissive_projection():
    projected = _professional_cv_projection(
        (
            "\n".join(
                (
                    "Synthetic Candidate",
                    "Applied AI Researcher",
                    "synthetic@example.com",
                    "Professional Profile",
                    "I build trustworthy machine-learning systems.",
                    "Expected remuneration: EUR 120000",
                    "Right to work: unrestricted",
                    "Family status: married",
                )
            ),
        )
    )

    assert "Synthetic Candidate" in projected
    assert "synthetic@example.com" in projected
    assert "remuneration" not in projected.casefold()
    assert "right to work" not in projected.casefold()
    assert "family status" not in projected.casefold()
