import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from portfolio_policy import LocalPortfolioScreener, PortfolioPolicy


def job(**overrides):
    value = {
        "title": "Machine Learning Engineer",
        "company": "Example AI",
        "location": "London, United Kingdom",
        "official_description": (
            "Build Python and PyTorch computer-vision systems in an English-speaking team."
        ),
        "verification_status": "verified",
        "process_language": "English",
        "ownership": {
            "classification": "allowed",
            "source": "https://example.test/about",
            "verified_at": "2026-07-16",
        },
    }
    value.update(overrides)
    return value


def configured_policy():
    return PortfolioPolicy.from_mapping(
        {
            "portfolio": {
                "geography": {
                    "primary": ["London", "Dublin"],
                    "lower_priority": ["Remote"],
                }
            }
        }
    )


def test_local_screening_is_auditable_and_uses_configured_location_order():
    policy = configured_policy()
    screener = LocalPortfolioScreener(policy)

    first = screener.screen(job(title="Research Scientist", location="London"))
    second = screener.screen(job(title="Research Scientist", location="Dublin"))
    other = screener.screen(job(title="Research Scientist", location="Lisbon"))

    assert first["score"] > second["score"] > other["score"]
    assert first["features"]["geography"]["label"] == "london"
    assert first["features"]["role_track"]["label"] == "research"
    assert first["outcome"] == "shortlisted"
    assert first["reasons"]


def test_configured_title_exclusion_is_applied_without_filtering_adjacent_roles():
    screener = LocalPortfolioScreener(
        PortfolioPolicy.from_mapping(
            {
                "portfolio": {
                    "shortlist_threshold": 0.3,
                    "excluded_title_terms": ["licensed clinician"],
                }
            }
        )
    )

    clinical = screener.screen(
        job(title="Licensed Clinician", official_description="")
    )
    ai_role = screener.screen(
        job(title="AI Engineer, Clinical Systems")
    )

    assert clinical["outcome"] == "filtered"
    assert clinical["reasons"] == [
        "Title matches configured exclusion: licensed clinician"
    ]
    assert ai_role["outcome"] == "shortlisted"


def test_only_configured_application_languages_are_allowed():
    screener = LocalPortfolioScreener(
        PortfolioPolicy.from_mapping(
            {
                "portfolio": {
                    "shortlist_threshold": 0.3,
                    "language": {"application_process": ["English", "Spanish"]}
                }
            }
        )
    )

    assert screener.screen(job(process_language="Spanish"))["shortlisted"] is True
    assert screener.screen(job(process_language="English"))["shortlisted"] is True
    assert screener.screen(job(process_language="French"))["outcome"] == "filtered"
    assert screener.screen(job(process_language="German"))["outcome"] == "filtered"


def test_research_bonus_is_not_a_rigid_production_exclusion():
    screener = LocalPortfolioScreener(configured_policy())

    weak_research = screener.screen(
        job(
            title="Research Scientist",
            official_description="Study an unrelated research topic.",
        )
    )
    exceptional_applied = screener.screen(
        job(
            title="Senior Applied AI Engineer",
            official_description=(
                "Own production Python PyTorch deep learning, computer vision, "
                "transformers, MLOps and AI agent systems."
            ),
        )
    )

    assert exceptional_applied["score"] > weak_research["score"]
    assert exceptional_applied["shortlisted"] is True


def test_geography_is_priority_not_global_filter():
    screener = LocalPortfolioScreener(configured_policy())

    remote = screener.screen(job(location="Remote, Europe"))
    other = screener.screen(
        job(
            location="Lisbon, Portugal",
            modality="on-site",
            sponsorship={
                "status": "not_stated",
                "source": "https://example.test/jobs/1",
                "verified_at": "2026-07-16",
            },
        )
    )

    assert remote["features"]["geography"]["label"] == "remote"
    assert other["features"]["geography"]["label"] == "other_or_unknown"
    assert other["outcome"] != "filtered"


def test_language_and_verified_ownership_filters_are_explicit_and_configurable():
    screener = LocalPortfolioScreener(
        PortfolioPolicy.from_mapping(
            {
                "portfolio": {
                    "shortlist_threshold": 0.3,
                    "language": {"application_process": ["English"]},
                    "ownership": {
                        "excluded_current_control": ["restricted_control"]
                    },
                }
            }
        )
    )

    french = screener.screen(job(process_language="French"))
    excluded = screener.screen(
        job(
            ownership={
                "classification": "restricted_control",
                "source": "https://example.test/ownership",
                "verified_at": "2026-07-16",
            }
        )
    )
    standard = screener.screen(job(sector="manufacturing"))

    assert french["outcome"] == "filtered"
    assert excluded["outcome"] == "filtered"
    assert standard["outcome"] == "shortlisted"
    assert standard["features"]["sector"]["label"] == "manufacturing"


def test_needs_local_fetch_and_low_scores_remain_auditable_without_model_decision():
    screener = LocalPortfolioScreener(configured_policy())

    waiting = screener.screen(job(verification_status="needs_local_fetch"))
    low = screener.screen(
        job(
            title="Office Administrator",
            location="Unknown",
            official_description="Manage calendars and office supplies.",
        )
    )

    assert waiting["shortlisted"] is False
    assert waiting["outcome"] == "needs_local_fetch"
    assert low["shortlisted"] is False
    assert low["outcome"] == "overflow"
    assert low["score"] >= 0
    assert low["reasons"]


def test_policy_is_built_from_persisted_preferences_instead_of_ignored_defaults():
    policy = PortfolioPolicy.from_mapping(
        {
            "portfolio": {"shortlist_threshold": 0.73},
            "target_roles": ["Research Scientist", "AI Safety Engineer"],
            "must_have_keywords": ["causal inference"],
            "nice_to_have_keywords": ["mechanistic interpretability"],
        }
    )

    assert policy.shortlist_threshold == 0.73
    assert "ai safety engineer" in policy.role_taxonomy["applied"]
    assert policy.skill_taxonomy["configured_preferences"] == (
        "causal inference",
        "mechanistic interpretability",
    )


def test_stale_legacy_preferences_cannot_change_current_portfolio_ranking():
    current = {
        "portfolio": {
            "shortlist_threshold": 0.45,
            "geography": {
                    "primary": ["London", "Dublin"],
                    "lower_priority": ["New York"],
                "global_filter": False,
            },
            "compensation": {
                "hard_salary_floor": None,
                "unpublished_is_eligible": True,
            },
            "language": {
                "application_process": ["English"],
                "unknown_is_eligible": True,
            },
        }
    }
    stale_legacy_copy = {
        **current,
        "preferred_location": "Spain",
        "locations": {"preferred": ["Madrid"], "rejected": ["Canada"]},
        "salary": {"hard_floor": 180_000},
        "languages": {"required": ["Spanish"]},
        "relocation": {"canada": False},
    }
    vacancy = job(
        location="New York, NY, USA",
        modality="on-site",
        process_language="English",
        compensation="unknown",
    )

    clean_result = LocalPortfolioScreener(PortfolioPolicy.from_mapping(current)).screen(
        vacancy
    )
    stale_result = LocalPortfolioScreener(
        PortfolioPolicy.from_mapping(stale_legacy_copy)
    ).screen(vacancy)

    assert stale_result == clean_result
    assert stale_result["features"]["geography"]["label"] == "new_york"
    assert stale_result["outcome"] != "filtered"
