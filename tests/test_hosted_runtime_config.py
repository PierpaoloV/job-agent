from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hosted_runtime_config import (  # noqa: E402
    HostedConfigurationError,
    HostedRuntimeConfig,
)


def _third_party_environment() -> dict[str, str]:
    return {
        "JOB_AGENT_CANDIDATE_NAME": "Alex Example",
        "JOB_AGENT_CAREER_GMAIL": "alex.jobs@gmail.com",
        "JOB_AGENT_GITHUB_REPOSITORY": "alex-example/job-agent",
        "JOB_AGENT_GITHUB_BRANCH": "stable",
        "JOB_AGENT_CANONICAL_CV_URL": (
            "https://github.com/alex-example/cv/releases/latest/download/cv.pdf"
        ),
        "TELEGRAM_ACTOR_ID": "123456",
        "TELEGRAM_CHAT_ID": "-100123456",
    }


def test_third_party_can_supply_complete_hosted_identity():
    config = HostedRuntimeConfig.from_mapping(_third_party_environment())

    assert config.candidate_name == "Alex Example"
    assert config.career_gmail == "alex.jobs@gmail.com"
    assert config.github_repository == "alex-example/job-agent"
    assert config.github_branch == "stable"
    assert config.canonical_cv_url.endswith("/releases/latest/download/cv.pdf")
    assert config.telegram_actor_id == "123456"
    assert config.telegram_chat_id == "-100123456"


@pytest.mark.parametrize(
    "missing_key",
    (
        "JOB_AGENT_CANDIDATE_NAME",
        "JOB_AGENT_CAREER_GMAIL",
        "JOB_AGENT_GITHUB_REPOSITORY",
        "JOB_AGENT_GITHUB_BRANCH",
        "JOB_AGENT_CANONICAL_CV_URL",
        "TELEGRAM_ACTOR_ID",
        "TELEGRAM_CHAT_ID",
    ),
)
def test_missing_required_hosted_identity_fails_closed(missing_key):
    environment = _third_party_environment()
    environment.pop(missing_key)

    with pytest.raises(HostedConfigurationError, match=missing_key):
        HostedRuntimeConfig.from_mapping(environment)
