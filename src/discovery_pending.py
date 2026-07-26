"""Durable verified shortlist retry state across discovery runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from vacancy_policy import VerificationState, verification_state
from workflow import NormalizedOpportunity, ShortlistArtifact


class PendingShortlistStore:
    def __init__(self, path: Path = Path("data/pending-shortlist.json")) -> None:
        self._path = Path(path)

    def merge(self, current: ShortlistArtifact) -> ShortlistArtifact:
        current.validate()
        pending = self._read()
        combined = _merge_opportunities(
            current.opportunities,
            () if pending is None else pending.opportunities,
        )
        retryable = tuple(item for item in combined if _requires_grade(item))
        self._write_or_clear(
            ShortlistArtifact(
                version=current.version,
                created_at=current.created_at,
                opportunities=retryable,
            )
        )
        return ShortlistArtifact(
            version=current.version,
            created_at=current.created_at,
            opportunities=combined,
        )

    def clear_graded(self, graded_jobs: Iterable[Mapping[str, Any]]) -> None:
        pending = self._read()
        if pending is None:
            return
        completed = {
            (
                str(item.get("stable_id") or item.get("dedup_key")),
                str(item.get("official_vacancy_version") or "unversioned"),
            )
            for item in graded_jobs
        }
        completed_ids = {stable_id for stable_id, _ in completed}
        remaining = tuple(
            item
            for item in pending.opportunities
            if not (
                (
                    verification_state(
                        item.job.get("verification_status")
                    )
                    == VerificationState.NEEDS_LOCAL_FETCH
                    and item.stable_id in completed_ids
                )
                or (
                    item.stable_id,
                    str(
                        item.job.get("official_vacancy_version")
                        or "unversioned"
                    ),
                )
                in completed
            )
        )
        self._write_or_clear(
            ShortlistArtifact(
                version=pending.version,
                created_at=pending.created_at,
                opportunities=remaining,
            )
        )

    def _read(self) -> ShortlistArtifact | None:
        if not self._path.exists():
            return None
        return ShortlistArtifact.read(self._path)

    def _write_or_clear(self, artifact: ShortlistArtifact) -> None:
        if not artifact.opportunities:
            self._path.unlink(missing_ok=True)
            return
        artifact.write(self._path)


def _merge_opportunities(
    current: tuple[NormalizedOpportunity, ...],
    pending: tuple[NormalizedOpportunity, ...],
) -> tuple[NormalizedOpportunity, ...]:
    current_ids = {item.stable_id for item in current}
    merged: dict[tuple[str, str], NormalizedOpportunity] = {}
    surviving_pending = tuple(
        item for item in pending if item.stable_id not in current_ids
    )
    for item in (*surviving_pending, *current):
        version = str(item.job.get("official_vacancy_version") or "unversioned")
        merged[(item.stable_id, version)] = item
    return tuple(merged.values())


def _requires_grade(item: NormalizedOpportunity) -> bool:
    state = verification_state(item.job.get("verification_status"))
    return item.shortlisted and (
        state == VerificationState.NEEDS_LOCAL_FETCH
        or (
            state == VerificationState.VERIFIED
            and bool(str(item.job.get("official_description", "")).strip())
        )
    )


__all__ = ["PendingShortlistStore"]
