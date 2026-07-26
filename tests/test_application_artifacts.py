from dataclasses import asdict, replace
import hashlib
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_artifacts import (  # noqa: E402
    ArtifactDocument,
    ArtifactFamily,
    ClaimAudit,
    DeepGradingMatrix,
    EvidenceBankSnapshot,
    EvidenceKind,
    EvidenceRecord,
    GeneratedArtifactBundle,
    MaterialClaim,
    RenderedArtifactBundle,
    RequirementEvidence,
    RequirementImportance,
    RequirementStatus,
    TruthfulApplicationArtifactService,
)
from application_domain import OfficialVacancy, PreparedArtifacts  # noqa: E402


class ReloadableEvidenceSource:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        return self.snapshot


class FakeBundleGenerator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        return self.result


class FakeClaimAuditor:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def audit(self, generated, evidence):
        self.calls.append((generated, evidence))
        return self.result or ClaimAudit(
            claims=generated.claims,
            unsupported_claims=(),
            complete=True,
        )


class FakeBundleRenderer:
    def __init__(self):
        self.calls = []
        self.publish_calls = []

    def render(self, *, application_id, bundle_version, cv_text, cover_letter_text):
        self.calls.append(
            (application_id, bundle_version, cv_text, cover_letter_text)
        )
        root = f"applications/{application_id}/artifacts/{bundle_version}"
        return RenderedArtifactBundle(
            cv_path=f"{root}/cv.pdf",
            cover_letter_path=f"{root}/cover-letter.pdf",
            cv_hash="sha256:rendered-cv",
            cover_letter_hash="sha256:rendered-cover-letter",
        )

    def publish(self, *, application_id, bundle_version, rendered):
        self.publish_calls.append((application_id, bundle_version, rendered))
        root = f"applications/{application_id}/artifacts/{bundle_version}"
        return replace(
            rendered,
            cv_path=f"{root}/cv.pdf",
            cover_letter_path=f"{root}/cover-letter.pdf",
        )


def evidence_snapshot(version="evidence-v1"):
    return EvidenceBankSnapshot(
        version=version,
        canonical_cv_version="master-cv-v1",
        evidence=(
            EvidenceRecord(
                evidence_id="evidence-research",
                families=(ArtifactFamily.RESEARCH, ArtifactFamily.CV_APPLIED_ML),
                kinds=(EvidenceKind.EXPERIENCE, EvidenceKind.IMPACT),
                approved_statement=(
                    "Designed and evaluated a synthetic vision research system."
                ),
                source_reference="master-cv:research-project",
            ),
            EvidenceRecord(
                evidence_id="evidence-python",
                families=(
                    ArtifactFamily.RESEARCH,
                    ArtifactFamily.CV_APPLIED_ML,
                    ArtifactFamily.AGENTIC_AI,
                ),
                kinds=(EvidenceKind.SKILL,),
                approved_statement="Used Python in production-quality ML research code.",
                source_reference="evidence-bank:python",
            ),
        ),
    )


def grading_matrix():
    return DeepGradingMatrix(
        version="grading-v1",
        official_vacancy_version="vacancy-v1",
        rows=(
            RequirementEvidence(
                id="req-research",
                requirement="Applied computer-vision research",
                importance=RequirementImportance.REQUIRED,
                status=RequirementStatus.MATCHED,
                evidence_ids=("evidence-research",),
                explanation="Approved research evidence is present.",
            ),
            RequirementEvidence(
                id="req-python",
                requirement="Python",
                importance=RequirementImportance.REQUIRED,
                status=RequirementStatus.MATCHED,
                evidence_ids=("evidence-python",),
                explanation="Approved Python evidence is present.",
            ),
        ),
    )


def verified_vacancy():
    return OfficialVacancy(
        version="vacancy-v1",
        fingerprint="sha256:vacancy",
        freshness="2026-07-16T10:30:00+00:00",
        description="Research and ship trustworthy computer-vision systems.",
    )


def generated_bundle():
    return GeneratedArtifactBundle(
        cv_text=(
            "Built and evaluated a computer-vision research system. "
            "Experienced with Python for ML research."
        ),
        cover_letter_text=(
            "Built and evaluated a computer-vision research system."
        ),
        claims=(
            MaterialClaim(
                statement="Built and evaluated a computer-vision research system.",
                kind=EvidenceKind.EXPERIENCE,
                evidence_ids=("evidence-research",),
                appears_in=("cv", "cover_letter"),
            ),
            MaterialClaim(
                statement="Experienced with Python for ML research.",
                kind=EvidenceKind.SKILL,
                evidence_ids=("evidence-python",),
                appears_in=("cv",),
            ),
        ),
    )


def synthetic_opportunity(matrix=None):
    return {
        "stable_id": "acme:research-42",
        "company": "Acme AI",
        "title": "Research Scientist",
        "artifact_family": "research",
        "requirements_evidence_matrix": (matrix or grading_matrix()).to_dict(),
    }


def test_prepare_reuses_deep_grading_matrix_and_generates_one_traced_bundle():
    source = ReloadableEvidenceSource(evidence_snapshot())
    generator = FakeBundleGenerator(generated_bundle())
    renderer = FakeBundleRenderer()
    service = TruthfulApplicationArtifactService(
        evidence_source=source,
        generator=generator,
        claim_auditor=FakeClaimAuditor(),
        renderer=renderer,
    )

    artifacts = service.prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(),
        verified_vacancy(),
    )

    assert len(generator.calls) == 1
    request = generator.calls[0]
    assert request.matrix.version == "grading-v1"
    assert request.family == ArtifactFamily.RESEARCH
    assert [item.evidence_id for item in request.evidence] == [
        "evidence-research",
        "evidence-python",
    ]
    assert len(renderer.calls) == 1
    assert len(renderer.publish_calls) == 1
    assert artifacts.cv_path.endswith("/cv.pdf")
    assert artifacts.cover_letter_path.endswith("/cover-letter.pdf")
    assert f"/{artifacts.version}/" in artifacts.cv_path
    assert artifacts.evidence_source_version == "evidence-v1"
    assert artifacts.matrix_version == "grading-v1"
    assert [trace.evidence_ids for trace in artifacts.claims] == [
        ("evidence-research",),
        ("evidence-python",),
    ]
    assert artifacts.stretch_decision.is_stretch is False
    assert PreparedArtifacts.from_dict(asdict(artifacts)) == artifacts


def test_persisted_artifact_vocabulary_reloads_as_typed_domain_values():
    artifacts = PreparedArtifacts.from_dict(
        {
            "version": "artifacts-v1",
            "cv_path": "applications/synthetic/cv.pdf",
            "cover_letter_path": "applications/synthetic/cover.pdf",
            "cv_hash": "sha256:cv",
            "cover_letter_hash": "sha256:cover",
            "family": "research",
            "claims": [
                {
                    "statement": "Experienced with Python.",
                    "kind": "skill",
                    "evidence_ids": ["evidence-python"],
                    "appears_in": ["cv"],
                }
            ],
        }
    )

    assert artifacts.family == ArtifactFamily.RESEARCH
    assert artifacts.claims[0].kind == EvidenceKind.SKILL
    assert artifacts.claims[0].appears_in == (ArtifactDocument.CV,)


def test_prepare_rejects_a_skill_or_impact_without_approved_support():
    unsupported = replace(
        generated_bundle(),
        claims=(
            MaterialClaim(
                statement="Scaled training to 10,000 GPUs.",
                kind=EvidenceKind.IMPACT,
                evidence_ids=("invented-impact",),
                appears_in=("cv",),
            ),
        ),
    )
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(unsupported),
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )

    try:
        service.prepare(
            "synthetic-001",
            "prepare:token-1",
            synthetic_opportunity(),
            verified_vacancy(),
        )
    except ValueError as error:
        assert "unsupported evidence" in str(error)
    else:
        raise AssertionError("unsupported professional claim was accepted")


def test_required_gap_becomes_an_explained_stretch_without_fabricated_evidence():
    matrix = replace(
        grading_matrix(),
        rows=grading_matrix().rows
        + (
            RequirementEvidence(
                id="req-hpc",
                requirement="Large-scale HPC training",
                importance=RequirementImportance.REQUIRED,
                status=RequirementStatus.GAP,
                evidence_ids=(),
                explanation="No approved HPC evidence is present.",
            ),
        ),
    )
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(generated_bundle()),
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )

    artifacts = service.prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(matrix),
        verified_vacancy(),
    )

    assert artifacts.stretch_decision.is_stretch is True
    assert artifacts.stretch_decision.gaps == ("Large-scale HPC training",)
    assert "No approved HPC evidence" in artifacts.stretch_decision.explanation


def test_partially_supported_required_requirement_is_also_a_stretch():
    partial = replace(
        grading_matrix(),
        rows=(
            replace(
                grading_matrix().rows[0],
                status=RequirementStatus.PARTIAL,
                explanation="Evidence covers experimentation but not deployment.",
            ),
            grading_matrix().rows[1],
        ),
    )
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(generated_bundle()),
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )

    artifacts = service.prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(partial),
        verified_vacancy(),
    )

    assert artifacts.stretch_decision.is_stretch is True
    assert artifacts.stretch_decision.gaps == (
        "Applied computer-vision research",
    )
    assert "not deployment" in artifacts.stretch_decision.explanation


def test_rileggi_cv_master_reloads_source_without_writing_to_it():
    source = ReloadableEvidenceSource(evidence_snapshot())
    service = TruthfulApplicationArtifactService(
        evidence_source=source,
        generator=FakeBundleGenerator(generated_bundle()),
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )
    service.prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(),
        verified_vacancy(),
    )
    source.snapshot = replace(
        evidence_snapshot("evidence-v2"), canonical_cv_version="master-cv-v2"
    )

    assert service.reload_master_cv() == "evidence-v2"
    assert source.load_calls == 2

    artifacts = service.prepare(
        "synthetic-002",
        "prepare:token-2",
        synthetic_opportunity(),
        verified_vacancy(),
    )
    assert artifacts.evidence_source_version == "evidence-v2"
    assert source.load_calls == 2


def test_each_cv_family_selects_only_from_the_same_approved_evidence_bank():
    for family, expected_ids in (
        ("research", ["evidence-research", "evidence-python"]),
        ("cv_applied_ml", ["evidence-research", "evidence-python"]),
        ("agentic_ai", ["evidence-python"]),
    ):
        generator = FakeBundleGenerator(
            replace(
                generated_bundle(),
                cv_text="Experienced with Python for ML research.",
                cover_letter_text="I am interested in this synthetic role.",
                claims=(
                    MaterialClaim(
                        statement="Experienced with Python for ML research.",
                        kind=EvidenceKind.SKILL,
                        evidence_ids=("evidence-python",),
                        appears_in=("cv",),
                    ),
                ),
            )
        )
        service = TruthfulApplicationArtifactService(
            evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
            generator=generator,
            claim_auditor=FakeClaimAuditor(),
            renderer=FakeBundleRenderer(),
        )
        opportunity = {**synthetic_opportunity(), "artifact_family": family}

        service.prepare(
            f"synthetic-{family}",
            f"prepare:{family}",
            opportunity,
            verified_vacancy(),
        )

        assert [item.evidence_id for item in generator.calls[0].evidence] == (
            expected_ids
        )


def test_prepare_refuses_an_unverified_official_description():
    generator = FakeBundleGenerator(generated_bundle())
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=generator,
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )

    try:
        service.prepare(
            "synthetic-001",
            "prepare:token-1",
            synthetic_opportunity(),
            replace(verified_vacancy(), verified=False),
        )
    except ValueError as error:
        assert "verified official vacancy" in str(error)
    else:
        raise AssertionError("tailoring began from an unverified description")
    assert generator.calls == []


def test_prepare_accepts_and_reemits_the_canonical_deep_grade_batch_matrix():
    canonical_matrix = {
        "version": "job-agent.requirements-evidence.v1",
        "official_vacancy_version": "vacancy-v1",
        "rows": [
            {
                "id": "req.python",
                "requirement": "Python",
                "importance": "required",
                "status": "matched",
                "evidence_ids": ["evidence-python"],
                "explanation": "Approved Python evidence is present.",
            },
            {
                "id": "req.robotics",
                "requirement": "Robotics",
                "importance": "preferred",
                "status": "unknown",
                "evidence_ids": [],
                "explanation": "The grading evidence did not establish robotics.",
            },
        ],
    }
    generator = FakeBundleGenerator(
        replace(
            generated_bundle(),
            cv_text="Experienced with Python for ML research.",
            cover_letter_text="I am interested in this synthetic role.",
            claims=(
                MaterialClaim(
                    statement="Experienced with Python for ML research.",
                    kind=EvidenceKind.SKILL,
                    evidence_ids=("evidence-python",),
                    appears_in=("cv",),
                ),
            ),
        )
    )
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=generator,
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )
    ranked_batch_item = {
        **synthetic_opportunity(),
        "requirements_evidence_matrix": canonical_matrix,
    }

    service.prepare(
        "synthetic-001",
        "prepare:token-1",
        ranked_batch_item,
        verified_vacancy(),
    )

    normalized = generator.calls[0].matrix
    assert normalized.rows[0].id == "req.python"
    assert normalized.rows[0].status == RequirementStatus.MATCHED
    serialized = normalized.to_dict()
    assert serialized["rows"][0]["id"] == "req.python"
    assert serialized["rows"][0]["status"] == "matched"
    assert "requirements" not in serialized


def test_prepare_rejects_an_unbound_legacy_matrix_for_generation():
    unbound = replace(grading_matrix(), official_vacancy_version=None)
    generator = FakeBundleGenerator(generated_bundle())
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=generator,
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )

    try:
        service.prepare(
            "synthetic-001",
            "prepare:token-1",
            synthetic_opportunity(unbound),
            verified_vacancy(),
        )
    except ValueError as error:
        assert "does not match official vacancy" in str(error)
    else:
        raise AssertionError("unbound grading matrix was used for generation")
    assert generator.calls == []


def test_family_projection_removes_unavailable_evidence_and_downgrades_match():
    agent_only = EvidenceRecord(
        evidence_id="evidence-agent-only",
        families=(ArtifactFamily.AGENTIC_AI,),
        kinds=(EvidenceKind.EXPERIENCE,),
        approved_statement="Built a synthetic agent workflow.",
        source_reference="evidence-bank:agent-workflow",
    )
    source = replace(
        evidence_snapshot(),
        evidence=evidence_snapshot().evidence + (agent_only,),
    )
    mixed = DeepGradingMatrix(
        version="grading-v1",
        official_vacancy_version="vacancy-v1",
        rows=(
            RequirementEvidence(
                id="req-research",
                requirement="Applied research and agent workflows",
                importance=RequirementImportance.REQUIRED,
                status=RequirementStatus.MATCHED,
                evidence_ids=("evidence-research", "evidence-agent-only"),
                explanation="Two approved records were cited by grading.",
            ),
        ),
    )
    generated = GeneratedArtifactBundle(
        cv_text="Built and evaluated a computer-vision research system.",
        cover_letter_text="Built and evaluated a computer-vision research system.",
        claims=(
            MaterialClaim(
                statement="Built and evaluated a computer-vision research system.",
                kind=EvidenceKind.EXPERIENCE,
                evidence_ids=("evidence-research",),
                appears_in=("cv", "cover_letter"),
            ),
        ),
    )
    generator = FakeBundleGenerator(generated)
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(source),
        generator=generator,
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )

    artifacts = service.prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(mixed),
        verified_vacancy(),
    )

    projected = generator.calls[0].matrix.rows[0]
    assert projected.status == RequirementStatus.PARTIAL
    assert projected.evidence_ids == ("evidence-research",)
    assert artifacts.stretch_decision.is_stretch is True


def test_artifact_integrity_verification_detects_changed_published_bytes(tmp_path):
    cv = tmp_path / "cv.pdf"
    cover = tmp_path / "cover.pdf"
    cv.write_bytes(b"approved cv")
    cover.write_bytes(b"approved cover")
    artifacts = PreparedArtifacts(
        version="bundle-v1",
        cv_path=str(cv),
        cover_letter_path=str(cover),
        cv_hash=f"sha256:{hashlib.sha256(cv.read_bytes()).hexdigest()}",
        cover_letter_hash=f"sha256:{hashlib.sha256(cover.read_bytes()).hexdigest()}",
    )

    assert TruthfulApplicationArtifactService.verify_artifacts(artifacts) is True

    cv.write_bytes(b"modified cv")

    assert TruthfulApplicationArtifactService.verify_artifacts(artifacts) is False


def test_legacy_requirement_matrix_shape_remains_readable():
    legacy = {
        "version": "grading-v1",
        "requirements": [
            {
                "requirement_id": "req-python",
                "requirement": "Python",
                "importance": "required",
                "status": "supported",
                "evidence_ids": ["evidence-python"],
                "explanation": "Approved Python evidence is present.",
            }
        ],
    }

    normalized = DeepGradingMatrix.from_dict(legacy)

    assert normalized.rows[0].id == "req-python"
    assert normalized.to_dict()["rows"][0]["status"] == "matched"


def test_bundle_version_tracks_matrix_content_and_stretch_decision():
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(generated_bundle()),
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    )
    standard = service.prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(),
        verified_vacancy(),
    )
    partial_matrix = replace(
        grading_matrix(),
        rows=(
            replace(
                grading_matrix().rows[0],
                status=RequirementStatus.PARTIAL,
                explanation="Only experimental evidence is approved.",
            ),
            grading_matrix().rows[1],
        ),
    )

    stretch = service.prepare(
        "synthetic-001",
        "prepare:token-2",
        synthetic_opportunity(partial_matrix),
        verified_vacancy(),
    )

    assert stretch.stretch_decision.is_stretch is True
    assert stretch.matrix_version == standard.matrix_version
    assert stretch.version != standard.version


def test_bundle_version_tracks_rendered_cv_and_cover_bytes():
    class ChangedTemplateRenderer(FakeBundleRenderer):
        def render(self, **kwargs):
            rendered = super().render(**kwargs)
            return replace(
                rendered,
                cv_hash="sha256:changed-template-cv",
                cover_letter_hash="sha256:changed-template-cover",
            )

    standard = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(generated_bundle()),
        claim_auditor=FakeClaimAuditor(),
        renderer=FakeBundleRenderer(),
    ).prepare(
        "synthetic-001",
        "prepare:token-1",
        synthetic_opportunity(),
        verified_vacancy(),
    )
    changed = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(generated_bundle()),
        claim_auditor=FakeClaimAuditor(),
        renderer=ChangedTemplateRenderer(),
    ).prepare(
        "synthetic-001",
        "prepare:token-2",
        synthetic_opportunity(),
        verified_vacancy(),
    )

    assert changed.cv_hash != standard.cv_hash
    assert changed.cover_letter_hash != standard.cover_letter_hash
    assert changed.version != standard.version


def test_full_document_audit_rejects_an_undeclared_fabricated_claim():
    generated = replace(
        generated_bundle(),
        cv_text=generated_bundle().cv_text + " Led a $100M programme.",
    )
    auditor = FakeClaimAuditor(
        ClaimAudit(
            claims=generated.claims,
            unsupported_claims=("Led a $100M programme.",),
            complete=True,
        )
    )
    renderer = FakeBundleRenderer()
    service = TruthfulApplicationArtifactService(
        evidence_source=ReloadableEvidenceSource(evidence_snapshot()),
        generator=FakeBundleGenerator(generated),
        claim_auditor=auditor,
        renderer=renderer,
    )

    try:
        service.prepare(
            "synthetic-001",
            "prepare:token-1",
            synthetic_opportunity(),
            verified_vacancy(),
        )
    except ValueError as error:
        assert "unsupported material claims" in str(error)
    else:
        raise AssertionError("full-document audit accepted a fabricated claim")
    assert len(auditor.calls) == 1
    assert renderer.calls == []
