import pathlib
import sys

import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_artifacts import (  # noqa: E402
    DeepGradingMatrix,
    EvidenceRecord,
    GeneratedArtifactBundle,
    MaterialClaim,
    RequirementEvidence,
    TailoringRequest,
)
from application_domain import (  # noqa: E402
    ArtifactDocument,
    ArtifactFamily,
    EvidenceKind,
    OfficialVacancy,
    StretchDecision,
)
from application_composition import build_application_artifact_service  # noqa: E402
from requirements_evidence import (  # noqa: E402
    RequirementImportance,
    RequirementStatus,
)
from structured_artifact_generator import (  # noqa: E402
    AnthropicArtifactProvider,
    DeterministicClaimAuditor,
    StructuredArtifactGenerator,
)


class RecordingProvider:
    identity = "recording-provider:test"

    def __init__(self, response):
        self.response = response
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return self.response


class FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class RecordingPost:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeHttpResponse(self.payload)


def tailoring_request():
    evidence = EvidenceRecord(
        evidence_id="python-research",
        families=(ArtifactFamily.RESEARCH,),
        kinds=(EvidenceKind.SKILL,),
        approved_statement="Uses Python to build reproducible ML research pipelines.",
        source_reference="master-cv:skills",
    )
    return TailoringRequest(
        application_id="synthetic-001",
        intent_id="prepare:token-1",
        family=ArtifactFamily.RESEARCH,
        official_vacancy=OfficialVacancy(
            version="vacancy-v1",
            fingerprint="sha256:vacancy",
            freshness="2026-07-24T10:00:00+00:00",
            description="Build reproducible computer-vision research systems.",
        ),
        matrix=DeepGradingMatrix(
            version="job-agent.requirements-evidence.v1",
            official_vacancy_version="vacancy-v1",
            rows=(
                RequirementEvidence(
                    id="req-python",
                    requirement="Python",
                    importance=RequirementImportance.REQUIRED,
                    status=RequirementStatus.MATCHED,
                    evidence_ids=("python-research",),
                    explanation="Approved Python evidence is present.",
                ),
            ),
        ),
        canonical_cv_version="sha256:master-cv",
        evidence=(evidence,),
        stretch_decision=StretchDecision(False),
    )


def test_generates_both_documents_and_exact_claim_traces_in_one_provider_call():
    provider = RecordingProvider(
        {
            "cv_text": "Builds reproducible ML research pipelines with Python.",
            "cover_letter_text": (
                "I build reproducible ML research pipelines with Python."
            ),
            "claims": [
                {
                    "statement": (
                        "Builds reproducible ML research pipelines with Python."
                    ),
                    "kind": "skill",
                    "evidence_ids": ["python-research"],
                    "appears_in": ["cv"],
                },
                {
                    "statement": (
                        "I build reproducible ML research pipelines with Python."
                    ),
                    "kind": "skill",
                    "evidence_ids": ["python-research"],
                    "appears_in": ["cover_letter"],
                },
            ],
        }
    )

    generated = StructuredArtifactGenerator(provider).generate(tailoring_request())

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request["official_vacancy"]["description"] == (
        "Build reproducible computer-vision research systems."
    )
    assert request["requirements_evidence_matrix"]["rows"][0]["evidence_ids"] == [
        "python-research"
    ]
    assert request["contract"]["allowed_untraced_lines"] == [
        "CURRICULUM VITAE",
        "PROFESSIONAL SUMMARY",
        "SELECTED EXPERIENCE",
        "EXPERIENCE",
        "SELECTED IMPACT",
        "SKILLS",
        "EDUCATION",
        "SELECTED PUBLICATIONS",
        "PUBLICATIONS",
        "PROJECTS",
        "Dear Hiring Team,",
        "Sincerely,",
    ]
    assert request["approved_evidence"] == [
        {
            "id": "python-research",
            "kind": ["skill"],
            "statement": (
                "Uses Python to build reproducible ML research pipelines."
            ),
            "source_reference": "master-cv:skills",
        }
    ]
    assert generated.cv_text.startswith("Builds reproducible")
    assert generated.cover_letter_text.startswith("I build reproducible")
    assert generated.claims[0].kind == EvidenceKind.SKILL
    assert generated.claims[0].appears_in == (ArtifactDocument.CV,)


def test_uses_one_sonnet_structured_output_request_for_the_whole_bundle():
    model_payload = {
        "cv_text": "Builds reproducible ML research pipelines with Python.",
        "cover_letter_text": (
            "I build reproducible ML research pipelines with Python."
        ),
        "claims": [
            {
                "statement": "Builds reproducible ML research pipelines with Python.",
                "kind": "skill",
                "evidence_ids": ["python-research"],
                "appears_in": ["cv"],
            },
            {
                "statement": (
                    "I build reproducible ML research pipelines with Python."
                ),
                "kind": "skill",
                "evidence_ids": ["python-research"],
                "appears_in": ["cover_letter"],
            },
        ],
    }
    post = RecordingPost(
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": __import__("json").dumps(model_payload)}],
        }
    )
    generator = StructuredArtifactGenerator(
        AnthropicArtifactProvider(api_key="test-secret", post=post)
    )

    generated = generator.generate(tailoring_request())

    assert generated.cv_text.startswith("Builds reproducible")
    assert len(post.calls) == 1
    url, kwargs = post.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert kwargs["json"]["model"] == "claude-sonnet-4-6"
    assert kwargs["json"]["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["json"]["output_config"]["format"]["schema"]["required"] == [
        "cv_text",
        "cover_letter_text",
        "claims",
    ]
    assert kwargs["headers"]["x-api-key"] == "test-secret"
    assert kwargs["timeout"] == 120


def test_deterministic_audit_rejects_untraced_professional_text():
    evidence = tailoring_request().evidence
    supported = "Uses Python to build reproducible ML research pipelines."
    generated = GeneratedArtifactBundle(
        cv_text=f"PROFESSIONAL SUMMARY\n{supported}\nLed a $100M programme.",
        cover_letter_text=f"Dear Hiring Team,\n\n{supported}\n\nSincerely,",
        claims=(
            MaterialClaim(
                statement=supported,
                kind=EvidenceKind.SKILL,
                evidence_ids=("python-research",),
                appears_in=(
                    ArtifactDocument.CV,
                    ArtifactDocument.COVER_LETTER,
                ),
            ),
        ),
    )

    audit = DeterministicClaimAuditor().audit(generated, evidence)

    assert audit.complete is True
    assert audit.claims == generated.claims
    assert audit.unsupported_claims == ("Led a $100M programme.",)


def test_deterministic_audit_rejects_fabrication_citing_a_same_kind_record():
    fabricated = "Led a $100M programme."
    generated = GeneratedArtifactBundle(
        cv_text=fabricated,
        cover_letter_text=fabricated,
        claims=(
            MaterialClaim(
                statement=fabricated,
                kind=EvidenceKind.SKILL,
                evidence_ids=("python-research",),
                appears_in=(
                    ArtifactDocument.CV,
                    ArtifactDocument.COVER_LETTER,
                ),
            ),
        ),
    )

    audit = DeterministicClaimAuditor().audit(
        generated, tailoring_request().evidence
    )

    assert audit.complete is True
    assert audit.unsupported_claims == (fabricated,)


def test_production_composition_builds_one_private_versioned_pdf_bundle(tmp_path):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canonical_cv.write_bytes(b"canonical CV")
    evidence_path = tmp_path / "evidence.yaml"
    evidence_path.write_text(
        yaml.safe_dump(
            {
                "highlights": [],
                "skill_evidence": [
                    {
                        "id": "python-research",
                        "kind": "skill",
                        "claim": (
                            "Uses Python to build reproducible ML research pipelines."
                        ),
                        "evidence": "master-cv:skills",
                        "suitable_for": ["research"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    statement = "Uses Python to build reproducible ML research pipelines."
    provider = RecordingProvider(
        {
            "cv_text": (
                "Synthetic Candidate\nPROFESSIONAL SUMMARY\n" + statement
            ),
            "cover_letter_text": (
                "Dear Hiring Team,\n\n"
                + statement
                + "\n\nSincerely,\nSynthetic Candidate"
            ),
            "claims": [
                {
                    "statement": statement,
                    "kind": "skill",
                    "evidence_ids": ["python-research"],
                    "appears_in": ["cv", "cover_letter"],
                }
            ],
        }
    )
    service = build_application_artifact_service(
        repository_root=tmp_path / "job-agent",
        evidence_path=evidence_path,
        canonical_cv_path=canonical_cv,
        candidate_name="Synthetic Candidate",
        provider=provider,
    )
    request = tailoring_request()

    artifacts = service.prepare(
        request.application_id,
        request.intent_id,
        {
            "artifact_family": "research",
            "requirements_evidence_matrix": request.matrix.to_dict(),
        },
        request.official_vacancy,
    )

    cv = pathlib.Path(artifacts.cv_path)
    cover_letter = pathlib.Path(artifacts.cover_letter_path)
    assert len(provider.requests) == 1
    assert cv.read_bytes().startswith(b"%PDF-")
    assert cover_letter.read_bytes().startswith(b"%PDF-")
    assert cv.parent == cover_letter.parent
    assert cv.parent.parent.parent == (
        tmp_path / "job-agent" / "data" / "private" / "application-artifacts"
    )
    assert artifacts.claims[0].evidence_ids == ("python-research",)
