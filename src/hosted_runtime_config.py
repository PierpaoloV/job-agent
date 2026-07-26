"""Owner-neutral identity required by the single-user hosted runtime."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import re
from typing import Mapping, Sequence
from urllib.parse import urlparse


class HostedConfigurationError(ValueError):
    """The hosted runtime cannot safely identify its single user."""


_REQUIRED_KEYS = (
    "JOB_AGENT_CANDIDATE_NAME",
    "JOB_AGENT_CAREER_GMAIL",
    "JOB_AGENT_GITHUB_REPOSITORY",
    "JOB_AGENT_GITHUB_BRANCH",
    "JOB_AGENT_CANONICAL_CV_URL",
    "TELEGRAM_ACTOR_ID",
    "TELEGRAM_CHAT_ID",
)
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TELEGRAM_ID = re.compile(r"^-?[0-9]+$")


@dataclass(frozen=True)
class HostedRuntimeConfig:
    candidate_name: str
    career_gmail: str
    github_repository: str
    github_branch: str
    canonical_cv_url: str
    telegram_actor_id: str
    telegram_chat_id: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "HostedRuntimeConfig":
        normalized = {
            key: str(values.get(key, "")).strip() for key in _REQUIRED_KEYS
        }
        missing = [key for key, value in normalized.items() if not value]
        if missing:
            raise HostedConfigurationError(
                "Missing required hosted identity: " + ", ".join(missing)
            )

        gmail = normalized["JOB_AGENT_CAREER_GMAIL"].casefold()
        if (
            gmail.count("@") != 1
            or not gmail.endswith("@gmail.com")
            or any(character.isspace() for character in gmail)
        ):
            raise HostedConfigurationError(
                "JOB_AGENT_CAREER_GMAIL must be a Gmail address"
            )
        repository = normalized["JOB_AGENT_GITHUB_REPOSITORY"]
        if _GITHUB_REPOSITORY.fullmatch(repository) is None:
            raise HostedConfigurationError(
                "JOB_AGENT_GITHUB_REPOSITORY must use owner/repository format"
            )
        cv_url = normalized["JOB_AGENT_CANONICAL_CV_URL"]
        parsed_cv_url = urlparse(cv_url)
        if parsed_cv_url.scheme != "https" or not parsed_cv_url.netloc:
            raise HostedConfigurationError(
                "JOB_AGENT_CANONICAL_CV_URL must be an absolute HTTPS URL"
            )
        for key in ("TELEGRAM_ACTOR_ID", "TELEGRAM_CHAT_ID"):
            if _TELEGRAM_ID.fullmatch(normalized[key]) is None:
                raise HostedConfigurationError(f"{key} must be a Telegram numeric ID")

        return cls(
            candidate_name=normalized["JOB_AGENT_CANDIDATE_NAME"],
            career_gmail=gmail,
            github_repository=repository,
            github_branch=normalized["JOB_AGENT_GITHUB_BRANCH"],
            canonical_cv_url=cv_url,
            telegram_actor_id=normalized["TELEGRAM_ACTOR_ID"],
            telegram_chat_id=normalized["TELEGRAM_CHAT_ID"],
        )

    @classmethod
    def from_environment(cls) -> "HostedRuntimeConfig":
        return cls.from_mapping(os.environ)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate-env",))
    parser.parse_args(argv)
    HostedRuntimeConfig.from_environment()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HostedConfigurationError",
    "HostedRuntimeConfig",
    "main",
]
