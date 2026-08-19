import json
import os
import pathlib
import stat
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from deep_grading import (
    DeepGradeResult,
    DeepGradeStore,
    DeepGradingService,
    GradingContractError,
    PortfolioDeepGrader,
    SanitizedProfessionalProfile,
)
import deep_grading_store
import main
from openai_grading_provider import OpenAIProviderError
from vacancy_policy import HardPolicy


SENSITIVE_VALUES = {
    "health": "SENSITIVE_HEALTH_MARKER",
    "demographics": {"race": "SENSITIVE_RACE_MARKER", "gender": "SENSITIVE_GENDER_MARKER"},
    "passport_number": "AA123456",
    "ats_answers": {"salary": "170000"},
}


def vacancy(**overrides):
    value = {
        "stable_id": "example:42",
        "verification_status": "verified",
        "official_url": "https://example.test/jobs/42",
        "retrieved_at": "2026-07-16T10:00:00+00:00",
        "published_at": "2026-07-10",
        "title": "Research Scientist",
        "company": "Example AI",
        "location": "Zurich",
        "modality": "on-site",
        "official_description": "Research computer vision with Python and PyTorch.",
        "compensation": {"status": "unknown"},
        "sponsorship": {
            "status": "not_stated",
            "source": "https://example.test/jobs/42",
            "verified_at": "2026-07-16",
        },
        "ownership": {
            "classification": "allowed",
            "source": "https://example.test/about",
            "verified_at": "2026-07-16",
        },
    }
    value.update(overrides)
    return value


def valid_response():
    components = {
        name: {"score": 80, "explanation": f"Evidence-based {name}"}
        for name in (
            "fit",
            "research_preference",
            "geography",
            "compensation_confidence",
            "wealth_potential",
            "language",
            "immigration",
            "ownership",
            "freshness",
            "deadline",
            "risk",
        )
    }
    return {
        "schema_version": "job-agent.deep-grade.v1",
        "overall_score": 84,
        "top_tier": {
            "value": True,
            "explanation": "Exceptional fit despite unpublished compensation.",
        },
        "rank_explanation": "Strong professional evidence and preferred geography.",
        "components": components,
        "compensation": {
            "base_cash": {"status": "unknown", "facts": []},
            "bonus": {"status": "unknown", "facts": []},
            "equity": {"status": "unknown", "facts": []},
            "benchmarks": [],
            "wealth_potential": {
                "confidence": "low",
                "explanation": "Cannot estimate without base cash.",
                "assumptions": [],
            },
        },
        "sponsorship": {
            "status": "not_stated",
            "source": "https://example.test/jobs/42",
            "verified_at": "2026-07-16",
            "visa_obstacle": False,
        },
        "ownership": {
            "classification": "allowed",
            "source": "https://example.test/about",
            "verified_at": "2026-07-16",
        },
        "risks": ["Startup execution risk"],
        "gaps": ["No direct robotics evidence"],
        "requirements_evidence_matrix": {
            "version": "job-agent.requirements-evidence.v1",
            "official_vacancy_version": "sha256:vacancy-42",
            "rows": [{
                "id": "req.python",
                "requirement": "Python",
                "importance": "required",
                "status": "matched",
                "evidence_ids": ["exp.pathology.python"],
                "explanation": "Demonstrated in research projects.",
            },
            {
                "id": "req.robotics",
                "requirement": "Robotics",
                "importance": "preferred",
                "status": "gap",
                "evidence_ids": [],
                "explanation": "No supplied evidence.",
            }],
        },
        "sources": ["https://example.test/jobs/42"],
    }


class FakeProvider:
    def __init__(self, response=None):
        self.response = response or valid_response()
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        return self.response


def profile():
    return SanitizedProfessionalProfile.from_mapping(
        {
            "provenance": "canonical_cv_evidence_bank",
            "professional_summary": "AI researcher in computational pathology",
            "skills": ["Python", "PyTorch", "computer vision"],
            "professional_evidence": [
                {
                    "id": "exp.pathology.python",
                    "claim": "Built Python deep-learning research pipelines",
                    "source_id": "canonical-cv:research",
                }
            ],
            "target_preferences": {
                "role_tracks": ["research", "applied", "production"],
                "geography": ["Zurich", "Basel", "Switzerland", "Paris", "US"],
            },
        }
    )


def test_one_call_persists_portfolio_and_reusable_requirement_matrix(tmp_path):
    provider = FakeProvider()
    store = DeepGradeStore(tmp_path)
    service = DeepGradingService(provider=provider, store=store)

    result = service.grade(
        vacancy(official_vacancy_version="vacancy-v1"), profile()
    )

    assert len(provider.calls) == 1
    assert result.overall_score == 84
    assert result.grading_input_fingerprint.startswith("sha256:")
    assert result.requirements_to_evidence[0].evidence_ids == (
        "exp.pathology.python",
    )
    assert store.load("example:42") == result
    persisted = json.loads(next(tmp_path.glob("*.json")).read_text())
    matrix = persisted["requirements_evidence_matrix"]
    assert matrix["version"] == "job-agent.requirements-evidence.v1"
    assert matrix["official_vacancy_version"] == "vacancy-v1"
    assert matrix["rows"][0]["id"] == "req.python"
    assert matrix["rows"][0]["status"] == "matched"

    cached = service.grade(
        vacancy(official_vacancy_version="vacancy-v1"), profile()
    )
    assert cached == result
    assert len(provider.calls) == 1

    changed = service.grade(
        vacancy(
            official_vacancy_version="vacancy-v2",
            official_description="Changed official Python research role.",
        ),
        profile(),
    )
    assert changed.grading_input_fingerprint != result.grading_input_fingerprint
    assert len(provider.calls) == 2


def test_deep_grade_cache_fsyncs_file_and_containing_directory(
    tmp_path, monkeypatch
):
    synced = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        mode = os.fstat(descriptor).st_mode
        synced.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(deep_grading_store.os, "fsync", recording_fsync)
    DeepGradingService(
        provider=FakeProvider(),
        store=DeepGradeStore(tmp_path),
    ).grade(vacancy(), profile())

    assert synced == ["file", "directory"]


@pytest.mark.parametrize(
    ("field", "invalid", "message"),
    (
        ("opportunity_id", "", "opportunity id"),
        ("vacancy_retrieved_at", "", "retrieval timestamp"),
        (
            "vacancy_retrieved_at",
            "2026-07-16T10:00:00",
            "timezone-aware",
        ),
        ("grading_input_fingerprint", "", "input fingerprint"),
        (
            "grading_input_fingerprint",
            "sha256:not-a-canonical-digest",
            "canonical sha256",
        ),
    ),
)
def test_deep_grade_canonical_identity_fields_are_required(
    tmp_path, field, invalid, message
):
    result = DeepGradingService(
        provider=FakeProvider(),
        store=DeepGradeStore(tmp_path),
    ).grade(vacancy(), profile())
    value = json.loads(json.dumps(result.to_dict()))
    value[field] = invalid

    with pytest.raises(GradingContractError, match=message):
        DeepGradeResult.from_dict(value)


def test_model_request_contains_only_official_vacancy_and_sanitized_profile(tmp_path):
    provider = FakeProvider()
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    service.grade(
        vacancy(
            **SENSITIVE_VALUES,
            snippet="alert-only snippet must not cross",
            raw_email_context="alert recipient private@example.test",
            email_date="2026-07-16",
            compensation={
                "status": "unknown",
                "ats_answer": "180000 nested",
                "base_cash": {"status": "unknown", "ats_answer": "190000 deep"},
            },
            sponsorship={
                "status": "not_stated",
                "source": "https://example.test/jobs/42",
                "verified_at": "2026-07-16",
                "health": "SENSITIVE_HEALTH_MARKER sponsorship",
            },
        ),
        profile(),
    )

    payload = json.dumps(provider.calls[0], sort_keys=True)
    assert "Research computer vision" in payload
    assert "exp.pathology.python" in payload
    for sensitive in (
        "SENSITIVE_HEALTH_MARKER",
        "SENSITIVE_RACE_MARKER",
        "SENSITIVE_GENDER_MARKER",
        "AA123456",
        "170000",
        "180000 nested",
        "190000 deep",
        "alert-only snippet",
        "private@example.test",
    ):
        assert sensitive not in payload
    for key in SENSITIVE_VALUES:
        assert key not in payload


def test_sensitive_content_hidden_in_an_allowed_professional_field_is_rejected():
    with pytest.raises(GradingContractError, match="sensitive candidate data"):
        SanitizedProfessionalProfile.from_mapping(
            {
                "_private_redaction_terms": ["EXAMPLE_PRIVATE_HEALTH_VALUE"],
                "provenance": "canonical_cv_evidence_bank",
                "professional_summary": (
                    "AI researcher with EXAMPLE_PRIVATE_HEALTH_VALUE"
                ),
                "skills": ["Python"],
                "professional_evidence": [],
            }
        )


def test_raw_sensitive_fields_and_untrusted_profile_provenance_are_rejected():
    with pytest.raises(GradingContractError, match="sensitive candidate data"):
        SanitizedProfessionalProfile.from_mapping(
            {
                "provenance": "canonical_cv_evidence_bank",
                "professional_summary": "AI researcher",
                "health": "private value",
            }
        )
    with pytest.raises(GradingContractError, match="provenance"):
        SanitizedProfessionalProfile.from_mapping(
            {"professional_summary": "AI researcher"}
        )


@pytest.mark.parametrize(
    "sensitive_key",
    ("apiKey", "api_key", "secret", "identityDocument"),
)
def test_nested_sensitive_profile_keys_are_rejected_before_serialization(
    sensitive_key,
):
    with pytest.raises(GradingContractError, match="sensitive candidate data"):
        SanitizedProfessionalProfile.from_mapping(
            {
                "provenance": "canonical_cv_evidence_bank",
                "professional_summary": "AI researcher",
                "skills": ["Python"],
                "professional_evidence": [],
                "target_preferences": {
                    "role_tracks": {sensitive_key: "must stay local"},
                },
            }
        )


@pytest.mark.parametrize("status", ["needs_local_fetch", "filtered", "lead"])
def test_unverified_or_filtered_role_makes_zero_calls(tmp_path, status):
    provider = FakeProvider()
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    assert service.grade_if_eligible(vacancy(verification_status=status), profile()) is None
    assert provider.calls == []


def test_screened_out_role_makes_zero_calls_even_when_vacancy_is_verified(tmp_path):
    provider = FakeProvider()
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    assert service.grade_if_eligible(
        vacancy(screening_outcome="filtered"), profile()
    ) is None
    assert provider.calls == []


def test_missing_official_description_makes_zero_calls(tmp_path):
    provider = FakeProvider()
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    assert service.grade_if_eligible(vacancy(official_description=""), profile()) is None
    assert provider.calls == []


def test_explicit_us_no_sponsorship_is_a_dated_visa_obstacle(tmp_path):
    response = valid_response()
    response["sponsorship"] = {
        "status": "no",
        "source": "https://example.test/jobs/us-1",
        "verified_at": "2026-07-16",
        "visa_obstacle": True,
    }
    provider = FakeProvider(response)
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    result = service.grade(
        vacancy(
            location="New York, USA",
            sponsorship=response["sponsorship"],
        ),
        profile(),
    )

    assert result.sponsorship.status == "no"
    assert result.sponsorship.visa_obstacle is True


def test_response_rejects_unsupported_saturation_metrics(tmp_path):
    response = valid_response()
    response["market_saturation_score"] = 72
    provider = FakeProvider(response)
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    with pytest.raises(GradingContractError, match="unsupported market or saturation"):
        service.grade(vacancy(), profile())


def test_response_requires_explanations_for_top_tier_and_every_component(tmp_path):
    response = valid_response()
    response["components"]["risk"]["explanation"] = ""
    provider = FakeProvider(response)
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    with pytest.raises(GradingContractError, match="component risk explanation"):
        service.grade(vacancy(), profile())


def test_matrix_rejects_unknown_evidence_and_missing_official_requirements(tmp_path):
    response = valid_response()
    response["requirements_evidence_matrix"]["rows"][0]["evidence_ids"] = [
        "invented.evidence"
    ]
    service = DeepGradingService(
        provider=FakeProvider(response), store=DeepGradeStore(tmp_path)
    )
    with pytest.raises(GradingContractError, match="unknown professional evidence"):
        service.grade(vacancy(), profile())

    response = valid_response()
    service = DeepGradingService(
        provider=FakeProvider(response), store=DeepGradeStore(tmp_path / "missing")
    )
    with pytest.raises(GradingContractError, match="omits an official requirement"):
        service.grade(vacancy(requirements=["Python", "Kubernetes"]), profile())


@pytest.mark.parametrize(
    "override",
    [
        {"process_language": "Spanish"},
        {
            "ownership": {
                "classification": "restricted_control",
                "source": "https://example.test/ownership",
                "verified_at": "2026-07-16",
            }
        },
    ],
)
def test_hard_policy_exclusions_make_zero_calls(tmp_path, override):
    provider = FakeProvider()
    service = DeepGradingService(
        provider=provider,
        store=DeepGradeStore(tmp_path),
        hard_policy=HardPolicy(
            allowed_languages=("english",),
            excluded_ownership=("restricted_control",),
        ),
    )

    assert service.grade_if_eligible(vacancy(**override), profile()) is None
    assert provider.calls == []


def test_top_tier_is_enforced_from_fit_score_not_model_boolean(tmp_path):
    response = valid_response()
    response["top_tier"] = {"value": True, "explanation": "Model says top."}
    response["components"]["fit"]["score"] = 40
    service = DeepGradingService(
        provider=FakeProvider(response), store=DeepGradeStore(tmp_path)
    )

    result = service.grade(vacancy(), profile())

    assert result.top_tier.value is False
    assert "configured" in result.top_tier.explanation


def test_unknown_compensation_stays_eligible_and_separates_base_bonus_equity(tmp_path):
    provider = FakeProvider()
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))

    result = service.grade(vacancy(), profile())

    assert result.compensation.base_cash.status == "unknown"
    assert result.compensation.bonus.status == "unknown"
    assert result.compensation.equity.status == "unknown"
    assert result.top_tier.value is True


def test_compensation_facts_preserve_currency_confidence_and_assumptions(tmp_path):
    response = valid_response()
    fact = {
        "value": "CHF 160000-180000",
        "source": "https://example.test/jobs/42",
        "date": "2026-07-16",
        "currency": "CHF",
        "confidence": "high",
        "assumptions": ["Published annual base-cash range"],
    }
    response["compensation"]["base_cash"] = {
        "status": "published",
        "facts": [fact],
    }
    response["compensation"]["benchmarks"] = [
        {
            **fact,
            "value": "CHF 170000 median",
            "confidence": "medium",
            "assumptions": ["Comparable seniority and canton"],
        }
    ]
    service = DeepGradingService(
        provider=FakeProvider(response), store=DeepGradeStore(tmp_path)
    )

    result = service.grade(vacancy(), profile())

    assert result.compensation.base_cash.facts[0].currency == "CHF"
    assert result.compensation.base_cash.facts[0].confidence == "high"
    assert result.compensation.base_cash.facts[0].assumptions == (
        "Published annual base-cash range",
    )
    assert result.compensation.benchmarks[0].confidence == "medium"


def test_compensation_fact_rejects_missing_spec_provenance_fields(tmp_path):
    response = valid_response()
    response["compensation"]["base_cash"] = {
        "status": "published",
        "facts": [
            {
                "value": "160000",
                "source": "https://example.test/jobs/42",
                "date": "2026-07-16",
            }
        ],
    }

    with pytest.raises(GradingContractError, match="currency, confidence, and assumptions"):
        DeepGradingService(
            provider=FakeProvider(response), store=DeepGradeStore(tmp_path)
        ).grade(vacancy(), profile())


def test_portfolio_adapter_calls_once_per_explicitly_verified_shortlisted_role(tmp_path):
    provider = FakeProvider()
    service = DeepGradingService(provider=provider, store=DeepGradeStore(tmp_path))
    grader = PortfolioDeepGrader(service=service, profile=profile())

    ranked = grader.rank(
        [
            vacancy(stable_id="example:verified"),
            vacancy(stable_id="example:missing-status", verification_status=None),
            vacancy(stable_id="example:no-description", official_description=""),
            vacancy(stable_id="example:local", verification_status="needs_local_fetch"),
        ],
        top_n=10,
    )

    assert len(provider.calls) == 1
    assert [item["stable_id"] for item in ranked] == ["example:verified"]
    matrix = ranked[0]["requirements_evidence_matrix"]
    assert matrix["rows"][0]["requirement"] == "Python"


def test_production_grader_resolves_and_grades_pending_role_in_one_model_call(
    tmp_path,
):
    response = valid_response()

    class WebProvider(FakeProvider):
        identity = "fake-web-grader"

        def __init__(self):
            super().__init__(response)
            self.resolve_calls = []

        def resolve_and_grade(self, lead, professional_profile):
            self.resolve_calls.append((lead, professional_profile))
            return {
                "resolution_status": "verified",
                "resolved_vacancy": {
                    "official_url": "https://example.test/jobs/42",
                    "official_job_id": "42",
                    "title": "Research Scientist",
                    "company": "Example AI",
                    "team": "Research",
                    "location": "Zurich",
                    "modality": "on-site",
                    "seniority": "",
                    "official_description": (
                        "Official employer description for a research scientist "
                        "working on computer vision, Python, PyTorch, and robotics. "
                        "The role designs experiments and deploys validated models."
                    ),
                    "requirements": ["Python", "Robotics"],
                    "published_at": "2026-07-16",
                    "process_language": "english",
                },
                "grade": response,
            }

    provider = WebProvider()
    grader = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=provider,
        profile_loader=profile,
    )

    ranked = grader.rank(
        [
            vacancy(
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
                source="Glassdoor",
                url="https://glassdoor.example/42",
            )
        ],
        10,
    )

    assert len(provider.resolve_calls) == 1
    assert provider.calls == []
    assert len(ranked) == 1
    assert ranked[0]["verification_status"] == "verified"
    assert ranked[0]["official_url"] == "https://example.test/jobs/42"
    assert ranked[0]["score"] == 0.84
    serialized = ranked[0]["portfolio_evaluation"]
    assert (
        serialized["requirements_evidence_matrix"]["rows"][0]["status"]
        == "matched"
    )
    assert serialized["compensation"]["base_cash"]["status"] == "unknown"


def test_web_resolution_accepts_company_scoped_gem_ats_url(tmp_path, capsys):
    response = valid_response()
    response["sources"] = [
        "https://jobs.gem.com/rivia/am9icG9zdDpX6tPeu4scKBFrmPoeoZ57"
    ]

    class GemProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            return {
                "resolution_status": "verified",
                "resolved_vacancy": {
                    "official_url": response["sources"][0],
                    "official_job_id": "R28",
                    "title": "Senior AI Engineer",
                    "company": "Rivia",
                    "team": "Product",
                    "location": "Zurich, Switzerland",
                    "modality": "Hybrid",
                    "seniority": "Senior",
                    "official_description": (
                        "Rivia is hiring a Senior AI Engineer to architect and "
                        "ship production-grade agentic AI workflows for clinical "
                        "trial intelligence, from evaluation through deployment, "
                        "observability, and continuous improvement."
                    ),
                    "requirements": ["Production-grade AI systems"],
                    "published_at": "2026-02-17",
                    "process_language": "english",
                },
                "grade": response,
            }

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=GemProvider(),
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                stable_id="linkedin:4399398799",
                title="Senior AI Engineer",
                company="Rivia",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
                source="LinkedIn",
                url="https://www.linkedin.com/jobs/view/4399398799",
            )
        ],
        10,
    )

    assert len(ranked) == 1
    assert ranked[0]["official_url"].startswith("https://jobs.gem.com/rivia/")
    assert "status=verified, accepted=true" in capsys.readouterr().out


def test_web_resolution_uses_lead_company_when_scoped_board_names_operator(
    tmp_path, capsys
):
    official_url = (
        "https://careers.speedinvest.com/companies/rivia/jobs/"
        "67935149-senior-ai-engineer"
    )
    response = valid_response()
    response["sources"] = [
        official_url,
        "https://jobs.gem.com/rivia/am9icG9zdDpX6tPeu4scKBFrmPoeoZ57",
    ]

    class PortfolioBoardProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            return {
                "resolution_status": "verified",
                "resolved_vacancy": {
                    "official_url": official_url,
                    "official_job_id": "R28",
                    "title": "Senior AI Engineer",
                    "company": "Speedinvest",
                    "team": "Product",
                    "location": "Zurich, Switzerland",
                    "modality": "Hybrid",
                    "seniority": "Senior",
                    "official_description": (
                        "Rivia is hiring a Senior AI Engineer to architect and "
                        "ship production-grade agentic AI workflows for clinical "
                        "trial intelligence, from evaluation through deployment, "
                        "observability, and continuous improvement."
                    ),
                    "requirements": ["Production-grade AI systems"],
                    "published_at": "2026-02-17",
                    "process_language": "english",
                },
                "grade": response,
            }

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=PortfolioBoardProvider(),
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                stable_id="linkedin:4399398799",
                title="Senior AI Engineer",
                company="Rivia",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
                source="LinkedIn",
                url="https://www.linkedin.com/jobs/view/4399398799",
            )
        ],
        10,
    )

    assert len(ranked) == 1
    assert ranked[0]["company"] == "Rivia"
    assert ranked[0]["official_url"] == official_url
    assert "status=verified, accepted=true" in capsys.readouterr().out


def test_web_resolution_rejects_similar_company_on_another_ats_scope(
    tmp_path, capsys
):
    response = valid_response()
    response["sources"] = [
        "https://jobs.lever.co/rivr/robotics-role",
        "https://www.rivr.ai",
    ]

    class SimilarCompanyProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            return {
                "resolution_status": "verified",
                "resolved_vacancy": {
                    "official_url": response["sources"][0],
                    "official_job_id": "robotics-role",
                    "title": "Senior AI Engineer Self-Supervised Learning",
                    "company": "RIVR",
                    "team": "Embodied AI",
                    "location": "Zurich, Switzerland",
                    "modality": "On-site",
                    "seniority": "Senior",
                    "official_description": (
                        "RIVR is a robotics company hiring an engineer to build "
                        "self-supervised learning systems for multimodal sensor "
                        "data from autonomous delivery robots operating in "
                        "complex real-world environments."
                    ),
                    "requirements": ["Robotics sensor-data experience"],
                    "published_at": None,
                    "process_language": "english",
                },
                "grade": response,
            }

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=SimilarCompanyProvider(),
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                stable_id="linkedin:4399398799",
                title="Senior AI Engineer",
                company="Rivia",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
                source="LinkedIn",
                url="https://www.linkedin.com/jobs/view/4399398799",
            )
        ],
        10,
    )

    assert ranked == []
    assert "reason=company_mismatch" in capsys.readouterr().out


def test_web_resolution_unavailable_makes_no_grade(tmp_path, capsys):
    class UnavailableProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            return {
                "resolution_status": "unavailable",
                "resolved_vacancy": None,
                "grade": None,
            }

    provider = UnavailableProvider()
    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=provider,
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
            )
        ],
        10,
    )

    assert ranked == []
    assert provider.calls == []
    assert "status=unavailable, accepted=false" in capsys.readouterr().out


def test_one_web_provider_error_does_not_block_other_resolutions(
    tmp_path,
    capsys,
):
    class PartiallyFailingProvider(FakeProvider):
        identity = "fake-web-grader"

        def __init__(self):
            super().__init__()
            self.resolve_calls = 0

        def resolve_and_grade(self, lead, professional_profile):
            self.resolve_calls += 1
            if self.resolve_calls == 2:
                raise RuntimeError("malformed provider output")
            return {
                "resolution_status": "unavailable",
                "resolved_vacancy": None,
                "grade": None,
            }

    provider = PartiallyFailingProvider()
    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=provider,
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                stable_id="example:first",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
            ),
            vacancy(
                stable_id="example:second",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
            ),
        ],
        10,
    )

    assert ranked == []
    output = capsys.readouterr().out
    assert "example:first: status=unavailable" in output
    assert "example:second: status=provider_error" in output


def test_one_invalid_web_grade_does_not_block_other_verified_roles(
    tmp_path,
    capsys,
):
    class PartiallyInvalidProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            del professional_profile
            response = valid_response()
            requirements = ["Python", "Robotics"]
            if lead["stable_id"] == "example:invalid":
                requirements = []
                response["requirements_evidence_matrix"]["rows"] = []
            return {
                "resolution_status": "verified",
                "resolved_vacancy": {
                    "official_url": f"https://example.test/jobs/{lead['stable_id']}",
                    "official_job_id": lead["stable_id"],
                    "title": "Research Scientist",
                    "company": "Example AI",
                    "team": "Research",
                    "location": "Zurich",
                    "modality": "on-site",
                    "seniority": "",
                    "official_description": (
                        "Example AI is hiring a research scientist to design, "
                        "validate, and deploy computer-vision systems using "
                        "Python and robotics methods in production research."
                    ),
                    "requirements": requirements,
                    "published_at": "2026-07-16",
                    "process_language": "english",
                },
                "grade": response,
            }

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=PartiallyInvalidProvider(),
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                stable_id="example:invalid",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
            ),
            vacancy(
                stable_id="example:valid",
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
            ),
        ],
        10,
    )

    assert [item["stable_id"] for item in ranked] == ["example:valid"]
    assert "example:invalid: status=contract_error" in capsys.readouterr().out


def test_one_invalid_verified_grade_does_not_block_another_role(
    tmp_path,
    capsys,
):
    invalid = valid_response()
    invalid["requirements_evidence_matrix"]["rows"] = []

    class SequentialProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.responses = [invalid, valid_response()]

        def complete(self, request):
            self.calls.append(request)
            return self.responses.pop(0)

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=SequentialProvider(),
        profile_loader=profile,
    ).rank(
        [
            vacancy(stable_id="example:invalid"),
            vacancy(stable_id="example:valid"),
        ],
        10,
    )

    assert [item["stable_id"] for item in ranked] == ["example:valid"]
    assert "example:invalid: status=contract_error" in capsys.readouterr().out


def test_single_invalid_verified_grade_is_isolated(tmp_path, capsys):
    invalid = valid_response()
    invalid["requirements_evidence_matrix"]["rows"] = []

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=FakeProvider(invalid),
        profile_loader=profile,
    ).rank([vacancy()], 10)

    assert ranked == []
    assert "status=contract_error" in capsys.readouterr().out


def test_all_web_provider_errors_fail_the_batch(tmp_path):
    class FailingProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            raise RuntimeError("provider unavailable")

    with pytest.raises(
        RuntimeError,
        match="All web grading resolutions failed safely",
    ):
        main.ProductionPortfolioGrader(
            store=DeepGradeStore(tmp_path),
            provider=FailingProvider(),
            profile_loader=profile,
        ).rank(
            [
                vacancy(
                    verification_status="needs_local_fetch",
                    official_description="",
                    screening_outcome="shortlisted",
                )
            ],
            10,
        )


def test_repeated_openai_provider_failure_opens_safe_circuit(
    tmp_path,
    capsys,
):
    safe_detail = (
        "HTTP 429, type=rate_limit_error, "
        "code=insufficient_quota, param=unknown"
    )

    class GloballyFailingProvider(FakeProvider):
        identity = "fake-web-grader"

        def __init__(self):
            super().__init__()
            self.resolve_calls = 0

        def resolve_and_grade(self, lead, professional_profile):
            self.resolve_calls += 1
            raise OpenAIProviderError(
                http_status=429,
                error_type="rate_limit_error",
                code="insufficient_quota",
                param="unknown",
            )

    provider = GloballyFailingProvider()
    jobs = [
        vacancy(
            stable_id=f"example:{index}",
            verification_status="needs_local_fetch",
            official_description="",
            screening_outcome="shortlisted",
        )
        for index in range(10)
    ]

    with pytest.raises(
        RuntimeError,
        match="Web grading provider circuit opened safely",
    ) as failure:
        main.ProductionPortfolioGrader(
            store=DeepGradeStore(tmp_path),
            provider=provider,
            profile_loader=profile,
        ).rank(jobs, 10)

    assert provider.resolve_calls == 2
    assert safe_detail in str(failure.value)
    output = capsys.readouterr().out
    assert output.count(safe_detail) == 2


def test_web_grade_drops_invented_evidence_and_canonicalizes_requirements(
    tmp_path,
):
    response = valid_response()
    response["requirements_evidence_matrix"]["rows"] = [
        {
            "id": "model-row",
            "requirement": "Python",
            "importance": "required",
            "status": "matched",
            "evidence_ids": ["invented.by.model"],
            "explanation": "The candidate appears to know Python.",
        }
    ]

    class WebProvider(FakeProvider):
        identity = "fake-web-grader"

        def resolve_and_grade(self, lead, professional_profile):
            return {
                "resolution_status": "verified",
                "resolved_vacancy": {
                    "official_url": "https://example.test/jobs/42",
                    "official_job_id": "42",
                    "title": "Research Scientist",
                    "company": "Example AI",
                    "team": "Research",
                    "location": "Zurich",
                    "modality": "on-site",
                    "seniority": "",
                    "official_description": (
                        "Official employer description for a research scientist "
                        "working on computer vision, Python, PyTorch, and robotics. "
                        "The role designs experiments and deploys validated models."
                    ),
                    "requirements": ["Python", "Robotics"],
                    "published_at": "2026-07-16",
                    "process_language": "english",
                },
                "grade": response,
            }

    ranked = main.ProductionPortfolioGrader(
        store=DeepGradeStore(tmp_path),
        provider=WebProvider(),
        profile_loader=profile,
    ).rank(
        [
            vacancy(
                verification_status="needs_local_fetch",
                official_description="",
                screening_outcome="shortlisted",
                source="Glassdoor",
            )
        ],
        10,
    )

    rows = ranked[0]["requirements_evidence_matrix"]["rows"]
    assert [row["requirement"] for row in rows] == ["Python", "Robotics"]
    assert rows[0]["status"] == "unknown"
    assert rows[0]["evidence_ids"] == []
    assert rows[1]["status"] == "unknown"
