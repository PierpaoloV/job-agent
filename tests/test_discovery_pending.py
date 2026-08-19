from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_pending import PendingShortlistStore
import discovery_jobs
from workflow import ShortlistArtifact


def artifact(
    *ids: str,
    verification_status: str = "verified",
    official_description: str = "Official role description",
) -> ShortlistArtifact:
    return ShortlistArtifact.from_dict({
        "version": "job-agent.shortlist.v1",
        "created_at": "2026-07-16T10:00:00+00:00",
        "opportunities": [
            {
                "stable_id": stable_id,
                "discovered_at": "2026-07-16T10:00:00+00:00",
                "source_confidence": "supported",
                "local_score": 0.8,
                "screening_reasons": ["fit"],
                "shortlisted": True,
                "screening_outcome": "shortlisted",
                "job": {
                    "dedup_key": stable_id,
                    "official_vacancy_version": "v1",
                    "verification_status": verification_status,
                    "official_description": official_description,
                },
            }
            for stable_id in ids
        ],
    })


def test_ungraded_roles_survive_empty_next_discovery_until_each_grade_completes(tmp_path):
    path = tmp_path / "pending.json"
    store = PendingShortlistStore(path)
    store.merge(artifact("one", "two"))

    retry = store.merge(artifact())
    assert [item.stable_id for item in retry.opportunities] == ["one", "two"]

    store.clear_graded([{"stable_id": "one", "official_vacancy_version": "v1"}])
    retry = store.merge(artifact())
    assert [item.stable_id for item in retry.opportunities] == ["two"]

    store.clear_graded([{"stable_id": "two", "official_vacancy_version": "v1"}])
    assert not path.exists()


def test_shortlisted_needs_local_fetch_survives_without_official_description(tmp_path):
    path = tmp_path / "pending.json"
    store = PendingShortlistStore(path)

    store.merge(
        artifact(
            "blocked",
            verification_status="needs_local_fetch",
            official_description="",
        )
    )
    retry = store.merge(artifact())

    assert [item.stable_id for item in retry.opportunities] == ["blocked"]
    assert retry.opportunities[0].job["verification_status"] == "needs_local_fetch"
    assert path.is_file()


def test_web_resolved_grade_clears_prior_unversioned_pending_record(tmp_path):
    path = tmp_path / "pending.json"
    store = PendingShortlistStore(path)
    store.merge(
        artifact(
            "blocked",
            verification_status="needs_local_fetch",
            official_description="",
        )
    )

    store.clear_graded([
        {
            "stable_id": "blocked",
            "official_vacancy_version": "sha256:resolved",
        }
    ])

    assert not path.exists()


def test_failed_deep_grade_leaves_pending_shortlist_retryable(
    monkeypatch,
    tmp_path,
):
    pending_path = tmp_path / "data" / "pending-shortlist.json"
    source = artifact(
        "blocked",
        verification_status="needs_local_fetch",
        official_description="",
    )
    PendingShortlistStore(pending_path).merge(source)
    shortlist_path = tmp_path / "shortlist.json"
    source.write(shortlist_path)

    class FailingGrader:
        def __init__(self, *, portfolio_policy):
            assert portfolio_policy == "configured-policy"

        def rank(self, jobs, limit):
            raise RuntimeError("provider circuit opened safely")

    monkeypatch.setitem(
        sys.modules,
        "main",
        SimpleNamespace(
            ProductionPortfolioGrader=FailingGrader,
            _load_portfolio_policy=lambda: "configured-policy",
        ),
    )
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / "graded.json"

    with pytest.raises(RuntimeError, match="provider circuit opened safely"):
        discovery_jobs.main([
            "deep-grade",
            "--artifact",
            str(shortlist_path),
            "--output",
            str(output_path),
        ])

    retry = PendingShortlistStore(pending_path).merge(artifact())
    assert [item.stable_id for item in retry.opportunities] == ["blocked"]
    assert not output_path.exists()
