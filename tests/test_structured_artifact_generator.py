import pathlib
import sys
from dataclasses import replace

from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas
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
    allowed = set(request["contract"]["allowed_untraced_lines"])
    assert {
        "PROFESSIONAL SUMMARY",
        "PROFESSIONAL EXPERIENCE",
        "TECHNICAL SKILLS",
        "EDUCATION",
        "SELECTED PUBLICATIONS",
        "Dear Hiring Team,",
        "Sincerely,",
    }.issubset(allowed)
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
    assert generated.cv_text.startswith("CURRICULUM VITAE")
    assert (
        "Uses Python to build reproducible ML research pipelines."
        in generated.cover_letter_text
    )
    assert len(generated.claims) == 1
    assert generated.claims[0].kind == EvidenceKind.SKILL
    assert generated.claims[0].appears_in == (
        ArtifactDocument.CV,
        ArtifactDocument.COVER_LETTER,
    )


def test_uses_one_sonnet_structured_output_request_for_the_whole_bundle():
    model_payload = {
        "headline": "Applied AI Researcher",
        "contacts": ["synthetic@example.com"],
        "summary": ["Applied AI researcher building machine-learning systems."],
        "experience": [
            {
                "role": "Machine Learning Researcher",
                "organization": "Example Institute",
                "location": "Amsterdam",
                "dates": "2022 - Present",
                "bullets": ["Built reproducible research systems."],
            },
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
        "selected_evidence_ids": ["python-research"],
        "target_role": "computer-vision research systems",
        "cover_letter_source_paragraphs": [
            "Applied AI researcher building machine-learning systems."
        ],
    }
    post = RecordingPost(
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": __import__("json").dumps(model_payload)}],
        }
    )
    generator = StructuredArtifactGenerator(
        AnthropicArtifactProvider(api_key="test-secret", post=post),
        candidate_name="Synthetic Candidate",
    )

    source = "\n".join(
        (
            "Synthetic Candidate",
            "Applied AI Researcher",
            "synthetic@example.com",
            "Applied AI researcher building machine-\nlearning systems.",
            "Machine Learning Researcher",
            "Example Institute",
            "Amsterdam",
            "2022 - Present",
            "Built reproducible research systems.",
            "PhD in Artificial Intelligence",
            "Example University",
            "2018 - 2022",
        )
    )
    generated = generator.generate(
        replace(tailoring_request(), canonical_cv_text=source)
    )

    assert generated.cv_text.startswith("# Synthetic Candidate")
    assert "Uses Python to build reproducible" in generated.cv_text
    assert "computer-vision research systems" in generated.cover_letter_text
    assert len(post.calls) == 1
    url, kwargs = post.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert kwargs["json"]["model"] == "claude-sonnet-4-6"
    assert kwargs["json"]["output_config"]["format"]["type"] == "json_schema"
    sent_request = __import__("json").loads(
        kwargs["json"]["messages"][0]["content"]
    )
    assert sent_request["canonical_cv_text"] == source
    assert kwargs["json"]["output_config"]["format"]["schema"]["required"] == [
        "headline",
        "contacts",
        "summary",
        "experience",
        "education",
        "selected_publications",
        "selected_evidence_ids",
        "target_role",
        "cover_letter_source_paragraphs",
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


def test_generator_rebuilds_documents_from_exact_evidence_when_model_adds_prose():
    approved = "Uses Python to build reproducible ML research pipelines."
    provider = RecordingProvider(
        {
            "cv_text": (
                "AI researcher with extensive production experience.\n"
                + approved
            ),
            "cover_letter_text": (
                "I am the ideal candidate for this role.\n" + approved
            ),
            "claims": [
                {
                    "statement": approved,
                    "kind": "skill",
                    "evidence_ids": ["python-research"],
                    "appears_in": ["cv", "cover_letter"],
                }
            ],
        }
    )

    generated = StructuredArtifactGenerator(
        provider,
        candidate_name="Synthetic Candidate",
    ).generate(tailoring_request())

    assert len(provider.requests) == 1
    assert "extensive production experience" not in generated.cv_text
    assert "ideal candidate" not in generated.cover_letter_text
    assert approved in generated.cv_text
    assert approved in generated.cover_letter_text
    audit = DeterministicClaimAuditor(
        structural_lines=("Synthetic Candidate",)
    ).audit(generated, tailoring_request().evidence)
    assert audit.unsupported_claims == ()


def test_production_composition_builds_one_private_versioned_pdf_bundle(tmp_path):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canvas = Canvas(str(canonical_cv))
    canvas.drawString(50, 800, "Synthetic Candidate canonical CV")
    canvas.save()
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


def test_production_bundle_preserves_complete_master_cv_identity_and_structure(
    tmp_path,
):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    canvas = Canvas(str(canonical_cv))
    source_lines = (
        "Synthetic Candidate",
        "Applied AI Researcher",
        "synthetic@example.com | example.com/synthetic",
        "Professional Profile",
        "Applied AI researcher building reproducible machine-learning systems.",
        "Professional Experience",
        "Machine Learning Researcher",
        "Example Research Institute",
        "Amsterdam, The Netherlands",
        "2022 - Present",
        "Built reproducible computer-vision research pipelines.",
        "Education",
        "PhD in Artificial Intelligence",
        "Example University",
        "2018 - 2022",
    )
    y = 800
    for line in source_lines:
        canvas.drawString(50, y, line)
        y -= 18
    canvas.save()
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
    provider = RecordingProvider(
        {
            "headline": "Applied AI Researcher",
            "contacts": [
                "synthetic@example.com",
                "example.com/synthetic",
            ],
            "summary": [
                "Applied AI researcher building reproducible machine-learning systems."
            ],
            "experience": [
                {
                    "role": "Machine Learning Researcher",
                    "organization": "Example Research Institute",
                    "location": "Amsterdam, The Netherlands",
                    "dates": "2022 - Present",
                    "bullets": [
                        "Built reproducible computer-vision research pipelines."
                    ],
                }
            ],
            "education": [
                {
                    "degree": "PhD in Artificial Intelligence",
                    "institution": "Example University",
                    "location": "Amsterdam, The Netherlands",
                    "dates": "2018 - 2022",
                }
            ],
            "selected_publications": [],
            "selected_evidence_ids": ["python-research"],
            "target_role": "",
            "cover_letter_source_paragraphs": [
                "Applied AI researcher building reproducible machine-learning systems."
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

    cv_text = "\n".join(
        page.extract_text() or "" for page in PdfReader(artifacts.cv_path).pages
    )
    letter_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(artifacts.cover_letter_path).pages
    )
    assert "synthetic@example.com" in cv_text
    assert "PROFESSIONAL EXPERIENCE" in cv_text
    assert "Machine Learning Researcher" in cv_text
    assert "Example Research Institute" in cv_text
    assert "2022 - Present" in cv_text
    assert "EDUCATION" in cv_text
    assert "PhD in Artificial Intelligence" in cv_text
    assert "TECHNICAL SKILLS" in cv_text
    assert "Applied AI researcher building reproducible" in letter_text
    assert len(PdfReader(artifacts.cv_path).pages) <= 2
