"""Disabled legacy alert-snippet ranker.

Deep grading now requires a verified official vacancy and a sanitized grading
profile. These names remain only to fail closed for old callers.
"""

from __future__ import annotations

import os
import pathlib

import yaml


RESUME_PATH = pathlib.Path(__file__).parent.parent / "resume.md"


class LegacyRankingDisabled(RuntimeError):
    pass


def _load_context():
    """Preserve non-model configuration reads during migration."""
    configured_preferences = os.environ.get(
        "JOB_AGENT_PREFERENCES_PATH", ""
    ).strip()
    if not configured_preferences:
        raise RuntimeError(
            "Portfolio preferences are missing. "
            "Set JOB_AGENT_PREFERENCES_PATH explicitly."
        )
    prefs = yaml.safe_load(
        pathlib.Path(configured_preferences).read_text(encoding="utf-8")
    )
    geography = prefs["portfolio"]["geography"]
    prefs["locations"] = {
        "preferred": list(geography["primary"]),
        "acceptable": list(geography["lower_priority"]),
        "rejected": [],
    }
    resume = os.environ.get("JOB_AGENT_RESUME_MD")
    if not resume and RESUME_PATH.exists():
        resume = RESUME_PATH.read_text()
    if not resume:
        raise RuntimeError(
            "Resume context is missing. Set JOB_AGENT_RESUME_MD in the environment "
            "or create a local ignored resume.md file."
        )
    return prefs, resume


def score_job(job: dict) -> dict:
    raise LegacyRankingDisabled(
        "Legacy snippet ranking is disabled; verify the official vacancy first"
    )


def rank_jobs(jobs: list[dict], top_n: int = 10) -> list[dict]:
    raise LegacyRankingDisabled(
        "Legacy snippet ranking is disabled; use the strict portfolio grader"
    )


__all__ = ["LegacyRankingDisabled", "rank_jobs", "score_job"]
