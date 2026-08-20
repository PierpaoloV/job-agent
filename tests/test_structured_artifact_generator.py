import pathlib
import sys
from dataclasses import replace

from pypdf import PdfReader
import pytest
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
            description="Research Scientist\nBuild reproducible computer-vision research systems.",
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
        canonical_cv_text=professional_source(),
    )


def professional_source():
    return "\n".join(
        (
            "Synthetic Candidate",
            "Applied AI Researcher",
            "synthetic@example.com",
            "Professional Profile",
            "Applied AI researcher building reproducible machine-learning systems for clinical computer vision.",
            "I validate machine-learning systems against real operational requirements and independent",
            "evaluation datasets.",
            "Professional Experience",
            "Machine Learning Researcher",
            "2022 - Present",
            "Example Institute",
            "Amsterdam",
            "• Built reproducible research systems.",
            "Education",
            "PhD in Artificial Intelligence",
            "2018 - 2022",
            "Example University",
            "Amsterdam",
            "Peer-Reviewed Publications and Proceedings",
            "Reproducible machine-learning systems for trustworthy computer vision.",
        )
    )


def professional_selection(**updates):
    payload = {
        "headline": "Applied AI Researcher",
        "contacts": ["synthetic@example.com"],
        "summary": [
            "Applied AI researcher building reproducible machine-learning systems for clinical computer vision."
        ],
        "experience": [
            {
                "source_block": "\n".join(
                    (
                        "Machine Learning Researcher",
                        "2022 - Present",
                        "Example Institute",
                        "Amsterdam",
                        "• Built reproducible research systems.",
                    )
                ),
                "role": "Machine Learning Researcher",
                "organization": "Example Institute",
                "location": "Amsterdam",
                "dates": "2022 - Present",
                "bullets": ["Built reproducible research systems."],
            }
        ],
        "education": [
            {
                "source_block": "\n".join(
                    (
                        "PhD in Artificial Intelligence",
                        "2018 - 2022",
                        "Example University",
                        "Amsterdam",
                    )
                ),
                "degree": "PhD in Artificial Intelligence",
                "institution": "Example University",
                "location": "Amsterdam",
                "dates": "2018 - 2022",
            }
        ],
        "selected_publications": [
            "Reproducible machine-learning systems for trustworthy computer vision."
        ],
        "selected_evidence_ids": ["python-research"],
        "target_requirement_ids": ["req-python"],
        "target_role": "Research Scientist",
        "cover_letter_source_paragraphs": [
            "I validate machine-learning systems against real operational requirements and independent evaluation datasets.",
        ],
    }
    payload.update(updates)
    return payload


def write_professional_cv_pdf(
    path,
    *,
    organization="Example Institute",
    location="Amsterdam",
    bullet="Built reproducible research systems.",
    contacts="synthetic@example.com",
):
    canvas = Canvas(str(path))
    y = 810

    def block(*lines):
        nonlocal y
        text = canvas.beginText(50, y)
        text.setLeading(14)
        for line in lines:
            text.textLine(line)
        canvas.drawText(text)
        y -= 14 * len(lines) + 24

    block("Synthetic Candidate")
    block("Applied AI Researcher")
    block(contacts)
    block(
        "Professional Profile",
        "Applied AI researcher building reproducible machine-learning systems for clinical computer vision.",
        "I validate machine-learning systems against real operational requirements and independent",
        "evaluation datasets.",
    )
    block("Professional Experience")
    block("Machine Learning Researcher", "2022 - Present")
    block(organization, location, f"• {bullet}")
    block("Education")
    block("PhD in Artificial Intelligence", "2018 - 2022")
    block("Example University", location)
    block("Peer-Reviewed Publications and Proceedings")
    block("Reproducible machine-learning systems for trustworthy computer vision.")
    block("Synthetic Candidate et al. Example Journal, 2026.")
    block("doi:10.0000/example.2026.1")
    canvas.save()


def test_generates_both_documents_and_exact_claim_traces_in_one_provider_call():
    provider = RecordingProvider(professional_selection())

    generated = StructuredArtifactGenerator(
        provider,
        candidate_name="Synthetic Candidate",
    ).generate(tailoring_request())

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request["official_vacancy"]["description"] == (
        "Research Scientist\nBuild reproducible computer-vision research systems."
    )
    assert request["requirements_evidence_matrix"]["rows"][0]["evidence_ids"] == [
        "python-research"
    ]
    assert request["canonical_cv_text"] == professional_source()
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
    assert generated.cv_text.startswith("# Synthetic Candidate")
    assert (
        "Uses Python to build reproducible ML research pipelines."
        in generated.cv_text
    )
    skill_claim = next(
        claim for claim in generated.claims if claim.evidence_ids == ("python-research",)
    )
    assert skill_claim.kind == EvidenceKind.SKILL
    assert skill_claim.appears_in == (
        ArtifactDocument.CV,
        ArtifactDocument.COVER_LETTER,
    )
    assert generated.additional_evidence


def test_application_code_caps_model_selected_role_bullets_at_four():
    bullets = [
        f"Built verified research pipeline number {number}."
        for number in ("one", "two", "three", "four", "five")
    ]
    source_block = "\n".join(
        (
            "Machine Learning Researcher",
            "2022 - Present",
            "Example Institute",
            "Amsterdam",
            *(f"• {bullet}" for bullet in bullets),
        )
    )
    source = professional_source().replace(
        "Machine Learning Researcher\n2022 - Present\nExample Institute\nAmsterdam\n"
        "• Built reproducible research systems.",
        source_block,
    )
    selection = professional_selection()
    selection["experience"][0] = {
        **selection["experience"][0],
        "source_block": source_block,
        "bullets": bullets,
    }

    generated = StructuredArtifactGenerator(
        RecordingProvider(selection), candidate_name="Synthetic Candidate"
    ).generate(replace(tailoring_request(), canonical_cv_text=source))

    assert bullets[3] in generated.cv_text
    assert bullets[4] not in generated.cv_text


def test_application_code_restores_authoritative_bullet_terminal_punctuation():
    selection = professional_selection()
    selection["experience"][0] = {
        **selection["experience"][0],
        "source_block": selection["experience"][0]["source_block"].removesuffix("."),
        "bullets": ["Built reproducible research systems"],
    }

    generated = StructuredArtifactGenerator(
        RecordingProvider(selection), candidate_name="Synthetic Candidate"
    ).generate(tailoring_request())

    assert "- Built reproducible research systems." in generated.cv_text


def test_application_code_rejects_a_truncated_source_bullet():
    selection = professional_selection()
    selection["experience"][0] = {
        **selection["experience"][0],
        "source_block": selection["experience"][0]["source_block"].replace(
            "• Built reproducible research systems.",
            "• Built reproducible research",
        ),
        "bullets": ["Built reproducible research"],
    }

    with pytest.raises(ValueError, match="complete bullets"):
        StructuredArtifactGenerator(
            RecordingProvider(selection), candidate_name="Synthetic Candidate"
        ).generate(tailoring_request())


def test_uses_one_sonnet_structured_output_request_for_the_whole_bundle():
    model_payload = {
        "headline": "Applied AI Researcher",
        "contacts": ["synthetic@example.com"],
        "summary": [
            "Applied AI researcher building trustworthy machine-learning systems for clinical computer vision."
        ],
        "experience": [
            {
                "source_block": "\n".join(
                    (
                        "Machine Learning Researcher",
                        "2022 - Present",
                        "Example Institute",
                        "Amsterdam",
                            "• Built reproducible research systems.",
                    )
                ),
                "role": "Machine Learning Researcher",
                "organization": "Example Institute",
                "location": "Amsterdam",
                "dates": "2022 - Present",
                "bullets": ["Built reproducible research systems."],
            },
        ],
        "education": [
            {
                "source_block": "\n".join(
                    (
                        "PhD in Artificial Intelligence",
                        "2018 - 2022",
                        "Example University",
                        "Amsterdam",
                    )
                ),
                "degree": "PhD in Artificial Intelligence",
                "institution": "Example University",
                "location": "Amsterdam",
                "dates": "2018 - 2022",
            }
        ],
        "selected_publications": [
            "Reproducible machine-learning systems for trustworthy computer vision."
        ],
        "selected_evidence_ids": ["python-research"],
        "target_requirement_ids": ["req-python"],
        "target_role": "Research Scientist",
        "cover_letter_source_paragraphs": [
            "I validate machine-learning systems against real operational requirements and independent evaluation datasets.",
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
            "Applied AI researcher building trustworthy machine-\nlearning systems for clinical computer vision.",
            "I validate machine-learning systems against real operational requirements and independent evaluation datasets.",
            "Professional Experience",
            "Machine Learning Researcher",
            "2022 - Present",
            "Example Institute",
            "Amsterdam",
            "• Built reproducible research systems.",
            "Education",
            "PhD in Artificial Intelligence",
            "2018 - 2022",
            "Example University",
            "Amsterdam",
            "Peer-Reviewed Publications and Proceedings",
            "Reproducible machine-learning systems for trustworthy computer vision.",
        )
    )
    generated = generator.generate(
        replace(tailoring_request(), canonical_cv_text=source)
    )

    assert generated.cv_text.startswith("# Synthetic Candidate")
    assert "Uses Python to build reproducible" in generated.cv_text
    assert "Research Scientist" in generated.cover_letter_text
    assert "Python" in generated.cover_letter_text
    assert "Please accept my application" not in generated.cover_letter_text
    assert len(post.calls) == 1
    url, kwargs = post.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert kwargs["json"]["model"] == "claude-sonnet-4-6"
    assert kwargs["json"]["max_tokens"] == 8_000
    assert kwargs["json"]["output_config"]["format"]["type"] == "json_schema"
    encoded_schema = __import__("json").dumps(
        kwargs["json"]["output_config"]["format"]["schema"]
    )
    assert all(
        unsupported not in encoded_schema
        for unsupported in (
            '"minItems"',
            '"maxItems"',
            '"minLength"',
            '"maxLength"',
            '"minimum"',
            '"maximum"',
        )
    )
    sent_request = __import__("json").loads(
        kwargs["json"]["messages"][0]["content"]
    )
    assert sent_request["canonical_cv_text"] == source
    assert kwargs["json"]["output_config"]["format"]["schema"]["required"] == [
        "headline",
        "summary",
        "experience",
        "education",
        "selected_publications",
        "selected_evidence_ids",
        "target_requirement_ids",
        "target_role",
        "cover_letter_source_paragraphs",
    ]
    assert kwargs["headers"]["x-api-key"] == "test-secret"
    assert kwargs["timeout"] == 300


def test_contact_display_is_derived_from_canonical_cv_not_model_formatting():
    generated = StructuredArtifactGenerator(
        RecordingProvider(
            professional_selection(contacts=["normalized-address@example.test"])
        ),
        candidate_name="Synthetic Candidate",
    ).generate(tailoring_request())

    assert "synthetic@example.com" in generated.cv_text
    assert "normalized-address@example.test" not in generated.cv_text


def test_structured_generation_rejects_identity_not_present_in_master_cv():
    provider = RecordingProvider(professional_selection())

    with pytest.raises(ValueError, match="candidate identity"):
        StructuredArtifactGenerator(
            provider,
            candidate_name="Different Candidate",
        ).generate(
            replace(tailoring_request(), canonical_cv_text=professional_source())
        )


def test_structured_generation_requires_approved_technical_skill_evidence():
    impact = EvidenceRecord(
        evidence_id="research-impact",
        families=(ArtifactFamily.RESEARCH,),
        kinds=(EvidenceKind.IMPACT,),
        approved_statement="Improved research evaluation quality.",
        source_reference="master-cv:impact",
    )
    provider = RecordingProvider(
        professional_selection(selected_evidence_ids=["research-impact"])
    )

    with pytest.raises(ValueError, match="technical skill"):
        StructuredArtifactGenerator(
            provider,
            candidate_name="Synthetic Candidate",
        ).generate(
            replace(
                tailoring_request(),
                canonical_cv_text=professional_source(),
                evidence=(impact,),
            )
        )


def test_structured_generation_rejects_fields_mixed_across_source_roles():
    second_role = "\n".join(
        (
            "Research Engineer",
            "2020 - 2022",
            "Different Institute",
            "London",
            "• Built a separate trustworthy research platform.",
        )
    )
    source = professional_source().replace(
        "\nEducation\n", f"\n{second_role}\nEducation\n"
    )
    mixed = professional_selection()
    mixed["experience"] = [
        {
            "source_block": "\n".join(
                (
                    "Machine Learning Researcher",
                    "2022 - Present",
                    "Example Institute",
                    "Amsterdam",
                    "• Built reproducible research systems.",
                    "Research Engineer",
                    "2020 - 2022",
                    "Different Institute",
                    "London",
                    "• Built a separate trustworthy research platform.",
                )
            ),
            "role": "Machine Learning Researcher",
            "organization": "Different Institute",
            "location": "London",
            "dates": "2020 - 2022",
            "bullets": ["Built a separate trustworthy research platform."],
        }
    ]

    with pytest.raises(ValueError, match="does not bind one source entry"):
        StructuredArtifactGenerator(
            RecordingProvider(mixed),
            candidate_name="Synthetic Candidate",
        ).generate(replace(tailoring_request(), canonical_cv_text=source))


def test_structured_generation_rejects_bullet_from_a_second_source_role():
    second_role = "\n".join(
        (
            "Research Engineer",
            "2020 - 2022",
            "Different Institute",
            "London",
            "• Built a separate trustworthy research platform.",
        )
    )
    source = professional_source().replace(
        "\nEducation\n", f"\n{second_role}\nEducation\n"
    )
    payload = professional_selection()
    payload["experience"] = [
        {
            "source_block": "\n".join(
                (
                    "Machine Learning Researcher",
                    "2022 - Present",
                    "Example Institute",
                    "Amsterdam",
                    "• Built reproducible research systems.",
                    second_role,
                )
            ),
            "role": "Machine Learning Researcher",
            "organization": "Example Institute",
            "location": "Amsterdam",
            "dates": "2022 - Present",
            "bullets": ["Built a separate trustworthy research platform."],
        }
    ]

    with pytest.raises(ValueError, match="contains another entry"):
        StructuredArtifactGenerator(
            RecordingProvider(payload),
            candidate_name="Synthetic Candidate",
        ).generate(replace(tailoring_request(), canonical_cv_text=source))


def test_structured_generation_rejects_abbreviated_role_metadata_and_target():
    abbreviated = professional_selection()
    abbreviated["experience"] = [
        {
            **abbreviated["experience"][0],
            "role": "Machine",
            "dates": "2022",
        }
    ]
    with pytest.raises(ValueError, match="role must copy canonical CV text"):
        StructuredArtifactGenerator(
            RecordingProvider(abbreviated),
            candidate_name="Synthetic Candidate",
        ).generate(tailoring_request())

    with pytest.raises(ValueError, match="target_role"):
        StructuredArtifactGenerator(
            RecordingProvider(
                professional_selection(target_role="Research Scientist")
            ),
            candidate_name="Synthetic Candidate",
        ).generate(
            replace(
                tailoring_request(),
                official_vacancy=replace(
                    tailoring_request().official_vacancy,
                    description="Build trustworthy research systems.",
                ),
            )
        )

    with pytest.raises(ValueError, match="target_role"):
        StructuredArtifactGenerator(
            RecordingProvider(professional_selection(target_role="research systems")),
            candidate_name="Synthetic Candidate",
        ).generate(tailoring_request())


def test_structured_generation_requires_publication_and_first_person_letter_source():
    generator = lambda payload: StructuredArtifactGenerator(  # noqa: E731
        RecordingProvider(payload),
        candidate_name="Synthetic Candidate",
    )

    with pytest.raises(ValueError, match="selected_publications"):
        generator(professional_selection(selected_publications=[])).generate(
            tailoring_request()
        )
    with pytest.raises(ValueError, match="first-person"):
        generator(
            professional_selection(
                cover_letter_source_paragraphs=[
                    "Applied AI researcher building reproducible machine-learning systems for clinical computer vision."
                ]
            )
        ).generate(tailoring_request())


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


def test_generator_rejects_model_text_that_is_not_in_master_cv():
    provider = RecordingProvider(
        professional_selection(headline="Invented production AI executive")
    )

    with pytest.raises(ValueError, match="headline must copy canonical CV text"):
        StructuredArtifactGenerator(
            provider,
            candidate_name="Synthetic Candidate",
        ).generate(tailoring_request())


def test_production_composition_builds_one_private_versioned_pdf_bundle(tmp_path):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    write_professional_cv_pdf(canonical_cv)
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
    provider = RecordingProvider(professional_selection())
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
    assert any(
        claim.evidence_ids == ("python-research",)
        for claim in artifacts.claims
    )


def test_production_bundle_preserves_complete_master_cv_identity_and_structure(
    tmp_path,
):
    canonical_cv = tmp_path / "curriculum_vitae.pdf"
    write_professional_cv_pdf(
        canonical_cv,
        organization="Example Research Institute",
        location="Amsterdam, The Netherlands",
        bullet="Built reproducible computer-vision research pipelines.",
        contacts="synthetic@example.com | example.com/synthetic",
    )
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
                "Applied AI researcher building reproducible machine-learning systems for clinical computer vision."
            ],
            "experience": [
                {
                    "source_block": "\n".join(
                        (
                            "Machine Learning Researcher",
                            "2022 - Present",
                            "Example Research Institute",
                            "Amsterdam, The Netherlands",
                            "• Built reproducible computer-vision research pipelines.",
                        )
                    ),
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
                    "source_block": "\n".join(
                        (
                            "PhD in Artificial Intelligence",
                            "2018 - 2022",
                            "Example University",
                            "Amsterdam, The Netherlands",
                        )
                    ),
                    "degree": "PhD in Artificial Intelligence",
                    "institution": "Example University",
                    "location": "Amsterdam, The Netherlands",
                    "dates": "2018 - 2022",
                }
            ],
            "selected_publications": [
                "Reproducible machine-learning systems for trustworthy computer vision."
            ],
            "selected_evidence_ids": ["python-research"],
            "target_requirement_ids": ["req-python"],
            "target_role": "Research Scientist",
            "cover_letter_source_paragraphs": [
                "I validate machine-learning systems against real operational requirements and independent evaluation datasets.",
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
    assert "I validate machine-learning systems" in letter_text
    assert "Uses Python to build reproducible" in letter_text
    assert len(PdfReader(artifacts.cv_path).pages) <= 2
    traced = {claim.statement for claim in artifacts.claims}
    assert "Machine Learning Researcher" in traced
    assert "PhD in Artificial Intelligence" in traced
