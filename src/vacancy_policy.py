"""Canonical public-vacancy fields and deterministic hard-policy states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping


PUBLIC_VACANCY_FIELDS = (
    "title",
    "role",
    "company",
    "team",
    "location",
    "url",
    "dedup_key",
    "source",
    "snippet",
    "salary",
    "remote_policy",
    "seniority",
    "required_skills",
    "email_date",
    "raw_email_context",
    "publication_date",
    "published_at",
    "application_deadline",
    "official_description",
    "official_description_url",
    "official_url",
    "canonical_url",
    "official_vacancy_version",
    "verification_status",
    "retrieved_at",
    "process_language",
    "modality",
    "requirements",
    "compensation",
    "sponsorship",
    "ownership",
    "company_stage",
    "sector",
)

# Only immutable official-vacancy facts may cross the deep-grading boundary.
# Alert snippets, email metadata, tracking context, and legacy salary strings
# deliberately remain outside this projection.
OFFICIAL_GRADING_FIELDS = (
    "stable_id",
    "official_url",
    "canonical_url",
    "official_vacancy_version",
    "verification_status",
    "retrieved_at",
    "published_at",
    "application_deadline",
    "title",
    "role",
    "company",
    "team",
    "location",
    "modality",
    "seniority",
    "official_description",
    "requirements",
    "compensation",
    "sponsorship",
    "ownership",
    "company_stage",
    "sector",
    "process_language",
)


class VerificationState(str, Enum):
    LEAD = "lead"
    VERIFIED = "verified"
    NEEDS_LOCAL_FETCH = "needs_local_fetch"
    NEEDS_OFFICIAL_DESCRIPTION = "needs_official_description"


class ScreeningOutcome(str, Enum):
    UNKNOWN = "unknown"
    SHORTLISTED = "shortlisted"
    OVERFLOW = "overflow"
    FILTERED = "filtered"
    NEEDS_LOCAL_FETCH = "needs_local_fetch"


@dataclass(frozen=True)
class HardPolicy:
    """User-configured deterministic exclusions.

    Empty values are deliberately permissive. Personal search boundaries belong
    in the ignored preferences file, never in the public source tree.
    """

    allowed_languages: tuple[str, ...] = ()
    excluded_title_terms: tuple[str, ...] = ()
    excluded_ownership: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, preferences: Mapping[str, Any]) -> "HardPolicy":
        portfolio = preferences.get("portfolio", {})
        if not isinstance(portfolio, Mapping):
            raise ValueError("portfolio preferences must be an object")
        language = portfolio.get("language", {})
        ownership = portfolio.get("ownership", {})
        if not isinstance(language, Mapping):
            raise ValueError("portfolio.language must be an object")
        if not isinstance(ownership, Mapping):
            raise ValueError("portfolio.ownership must be an object")

        return cls(
            allowed_languages=_normalized_values(
                language.get("application_process", ())
            ),
            excluded_title_terms=_normalized_values(
                portfolio.get("excluded_title_terms", ())
            ),
            excluded_ownership=_normalized_values(
                ownership.get("excluded_current_control", ())
            ),
        )


def verification_state(value: Any) -> VerificationState:
    try:
        return VerificationState(str(value or VerificationState.LEAD.value).casefold())
    except ValueError:
        return VerificationState.LEAD


def screening_outcome(value: Any) -> ScreeningOutcome:
    try:
        return ScreeningOutcome(str(value or ScreeningOutcome.UNKNOWN.value).casefold())
    except ValueError:
        return ScreeningOutcome.UNKNOWN


def hard_policy_exclusion(
    vacancy: Mapping[str, Any], policy: HardPolicy = HardPolicy()
) -> str | None:
    title = str(vacancy.get("title") or vacancy.get("role") or "").casefold()
    for term in policy.excluded_title_terms:
        if re.search(rf"\b{re.escape(term)}\b", title):
            return f"Title matches configured exclusion: {term}"
    language = str(vacancy.get("process_language", "unknown")).strip().casefold()
    if (
        policy.allowed_languages
        and language not in {"", "unknown"}
        and language not in policy.allowed_languages
    ):
        return "Application process language is outside configured languages"
    ownership = vacancy.get("ownership", {})
    if isinstance(ownership, Mapping):
        classification = str(ownership.get("classification", "unknown")).casefold()
        dated = bool(ownership.get("source") and ownership.get("verified_at"))
        if dated and classification in policy.excluded_ownership:
            return "Current ownership matches a configured exclusion"
    return None


def _normalized_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        dict.fromkeys(
            str(item).strip().casefold()
            for item in value
            if str(item).strip()
        )
    )


__all__ = [
    "HardPolicy",
    "PUBLIC_VACANCY_FIELDS",
    "OFFICIAL_GRADING_FIELDS",
    "ScreeningOutcome",
    "VerificationState",
    "hard_policy_exclusion",
    "screening_outcome",
    "verification_state",
]
