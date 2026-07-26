"""Batch workflow adapter for strict per-vacancy deep grading."""

from __future__ import annotations

from typing import Any

from deep_grading_contract import SanitizedProfessionalProfile
from deep_grading_service import DeepGradingService


class PortfolioDeepGrader:
    def __init__(
        self,
        *,
        service: DeepGradingService,
        profile: SanitizedProfessionalProfile,
    ) -> None:
        self._service = service
        self._profile = profile

    def rank(self, jobs: list[dict] | tuple[dict, ...], top_n: int) -> list[dict]:
        graded: list[dict[str, Any]] = []
        for job in jobs:
            grading_job = {
                **job,
                "stable_id": job.get("stable_id") or job.get("dedup_key"),
                "screening_outcome": job.get("screening_outcome", "shortlisted"),
            }
            result = self._service.grade_if_eligible(grading_job, self._profile)
            if result is None:
                continue
            grade = result.to_dict()
            graded.append(
                {
                    **job,
                    "score": result.overall_score / 100,
                    "priority": "high" if result.top_tier.value else "medium",
                    "rationale": result.rank_explanation,
                    "top_tier": grade["top_tier"],
                    "portfolio_evaluation": grade,
                    "requirements_evidence_matrix": grade[
                        "requirements_evidence_matrix"
                    ],
                }
            )
        return sorted(graded, key=lambda item: item["score"], reverse=True)[:top_n]


__all__ = ["PortfolioDeepGrader"]
