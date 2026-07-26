"""Deterministic first-stage portfolio screening.

The module intentionally has no model-provider, network, or secret dependency.
It combines explicit policy checks with a small, inspectable bag-of-words
taxonomy.  Its score is a shortlist priority, never an irreversible deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from vacancy_policy import (
    HardPolicy,
    VerificationState,
    hard_policy_exclusion,
    verification_state,
)


_WORDS = re.compile(r"[a-z0-9+#.]+")
_US_LOCATION_TERMS = (
    "usa",
    "united states",
    "new york",
    "california",
    "boston",
    "seattle",
)


@dataclass(frozen=True)
class PortfolioPolicy:
    shortlist_threshold: float
    role_taxonomy: Mapping[str, tuple[str, ...]]
    skill_taxonomy: Mapping[str, tuple[str, ...]]
    hard_policy: HardPolicy = HardPolicy()
    primary_locations: tuple[str, ...] = ()
    lower_priority_locations: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> "PortfolioPolicy":
        return cls(
            shortlist_threshold=0.45,
            role_taxonomy={
                "research": (
                    "research scientist",
                    "applied scientist",
                    "research engineer",
                    "ai scientist",
                    "machine learning scientist",
                ),
                "applied": (
                    "applied ai",
                    "machine learning engineer",
                    "ml engineer",
                    "computer vision engineer",
                    "ai engineer",
                ),
                "production": (
                    "production",
                    "mlops",
                    "platform engineer",
                    "llm engineer",
                    "software engineer",
                ),
            },
            skill_taxonomy={
                "machine_learning": ("machine learning", "deep learning", "ai"),
                "python": ("python",),
                "pytorch": ("pytorch",),
                "computer_vision": (
                    "computer vision",
                    "image analysis",
                ),
                "representation_learning": (
                    "transformer",
                    "representation learning",
                ),
                "ml_systems": ("mlops", "docker", "slurm", "gcp", "cloud"),
            },
            hard_policy=HardPolicy(),
            primary_locations=(),
            lower_priority_locations=(),
        )

    @classmethod
    def from_mapping(cls, preferences: Mapping[str, Any]) -> "PortfolioPolicy":
        """Build the executable screening policy from persisted preferences."""
        default = cls.default()
        portfolio = preferences.get("portfolio", {})
        if not isinstance(portfolio, Mapping):
            raise ValueError("portfolio preferences must be an object")
        threshold = float(
            portfolio.get("shortlist_threshold", default.shortlist_threshold)
        )
        if not 0 <= threshold <= 1:
            raise ValueError("portfolio shortlist threshold must be between 0 and 1")

        role_taxonomy = {
            track: list(phrases) for track, phrases in default.role_taxonomy.items()
        }
        for raw_role in preferences.get("target_roles", ()):
            role = str(raw_role).strip().casefold()
            if not role:
                continue
            if "research" in role:
                track = "research"
            elif any(
                term in role
                for term in ("mlops", "platform", "production", "software", "llm")
            ):
                track = "production"
            else:
                track = "applied"
            if role not in role_taxonomy[track]:
                role_taxonomy[track].append(role)

        configured_keywords = tuple(
            dict.fromkeys(
                str(keyword).strip().casefold()
                for group in ("must_have_keywords", "nice_to_have_keywords")
                for keyword in preferences.get(group, ())
                if str(keyword).strip()
            )
        )
        skill_taxonomy = dict(default.skill_taxonomy)
        if configured_keywords:
            skill_taxonomy["configured_preferences"] = configured_keywords
        geography = portfolio.get("geography", {})
        if not isinstance(geography, Mapping):
            raise ValueError("portfolio.geography must be an object")
        return cls(
            shortlist_threshold=threshold,
            role_taxonomy={
                track: tuple(phrases) for track, phrases in role_taxonomy.items()
            },
            skill_taxonomy=skill_taxonomy,
            hard_policy=HardPolicy.from_mapping(preferences),
            primary_locations=_configured_values(geography.get("primary", ())),
            lower_priority_locations=_configured_values(
                geography.get("lower_priority", ())
            ),
        )


class LocalPortfolioScreener:
    """Return an auditable local screening decision without an LLM call."""

    def __init__(self, policy: PortfolioPolicy) -> None:
        self._policy = policy

    def screen(self, job: Mapping[str, Any]) -> dict[str, Any]:
        verification = verification_state(job.get("verification_status"))
        if verification == VerificationState.NEEDS_LOCAL_FETCH:
            return self._decision(
                score=0.0,
                outcome="needs_local_fetch",
                reasons=("Official vacancy requires retrieval on the Mac",),
                features={
                    "verification": self._feature("needs_local_fetch", 0),
                },
                shortlisted=False,
            )

        language = _language(job)
        exclusion = hard_policy_exclusion(job, self._policy.hard_policy)
        if exclusion is not None:
            return self._decision(
                score=0.0,
                outcome="filtered",
                reasons=(exclusion,),
                features={"language": self._feature(language, 0)},
                shortlisted=False,
            )

        ownership = _evidence_mapping(job.get("ownership"))
        ownership_class = str(ownership.get("classification", "unknown")).casefold()
        title = str(job.get("title", ""))
        description = str(job.get("official_description", ""))
        text = f"{title} {description}".casefold()
        role_track, role_points = self._role_track(title.casefold())
        matched_skills = tuple(
            name
            for name, phrases in self._policy.skill_taxonomy.items()
            if any(_contains_phrase(text, phrase) for phrase in phrases)
        )
        skill_points = min(40, len(matched_skills) * 6)
        geography, geography_points = _geography(job, self._policy)
        sector = str(job.get("sector", "")).strip().casefold() or "unknown"
        startup = _startup_risk(job)
        score = round(
            min(1.0, (role_points + skill_points + geography_points) / 100),
            3,
        )
        shortlisted = score >= self._policy.shortlist_threshold
        outcome = "shortlisted" if shortlisted else "overflow"
        reasons = (
            f"{role_track} track contributes {role_points} points",
            f"{geography} geography contributes {geography_points} points",
            (
                f"Local taxonomy matched: {', '.join(matched_skills)}"
                if matched_skills
                else "Local taxonomy found no target-skill match"
            ),
        )
        return self._decision(
            score=score,
            outcome=outcome,
            reasons=reasons,
            shortlisted=shortlisted,
            features={
                "verification": self._feature(verification.value, 0),
                "role_track": self._feature(role_track, role_points),
                "skill_taxonomy": {
                    "label": "matched" if matched_skills else "no_match",
                    "points": skill_points,
                    "matches": list(matched_skills),
                    "method": "local_bag_of_phrases_v1",
                },
                "geography": self._feature(geography, geography_points),
                "language": self._feature(language, 0),
                "ownership": {
                    "label": ownership_class,
                    "points": 0,
                    "source": ownership.get("source"),
                    "verified_at": ownership.get("verified_at"),
                },
                "sector": self._feature(sector, 0),
                "startup_risk": self._feature(startup, 0),
            },
        )

    def _role_track(self, title: str) -> tuple[str, int]:
        for track, points in (("research", 20), ("applied", 14), ("production", 12)):
            if any(phrase in title for phrase in self._policy.role_taxonomy[track]):
                return track, points
        return "adjacent", 0

    @staticmethod
    def _feature(label: str, points: int) -> dict[str, Any]:
        return {"label": label, "points": points}

    @staticmethod
    def _decision(
        *,
        score: float,
        outcome: str,
        reasons: tuple[str, ...],
        features: Mapping[str, Any],
        shortlisted: bool,
    ) -> dict[str, Any]:
        return {
            "score": score,
            "outcome": outcome,
            "reasons": list(reasons),
            "features": dict(features),
            "shortlisted": shortlisted,
        }


def _contains_phrase(text: str, phrase: str) -> bool:
    if " " in phrase:
        return phrase in text
    return phrase in set(_WORDS.findall(text))


def _language(job: Mapping[str, Any]) -> str:
    value = str(job.get("process_language") or job.get("language") or "unknown")
    normalized = value.strip().casefold()
    return normalized if normalized else "unknown"


def _evidence_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {"classification": value or "unknown"}


def _geography(
    job: Mapping[str, Any], policy: PortfolioPolicy
) -> tuple[str, int]:
    location = str(job.get("location", "")).casefold()
    for index, configured in enumerate(policy.primary_locations):
        if _location_matches(location, configured):
            return _location_label(configured), max(12, 20 - index * 2)
    for configured in policy.lower_priority_locations:
        if _location_matches(location, configured):
            return _location_label(configured), 8
    return "other_or_unknown", 0


def _configured_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        str(item).strip().casefold() for item in value if str(item).strip()
    )


def _location_matches(location: str, configured: str) -> bool:
    normalized = configured.split("(", 1)[0].strip()
    return bool(normalized and normalized in location)


def _location_label(value: str) -> str:
    label = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return label or "configured"


def _startup_risk(job: Mapping[str, Any]) -> str:
    stage = str(job.get("company_stage", "")).casefold()
    if any(term in stage for term in ("seed", "series a", "early")):
        return "elevated"
    if "startup" in str(job.get("company_type", "")).casefold():
        return "review"
    return "not_established"


def is_us_location(location: str) -> bool:
    normalized = location.casefold()
    return any(name in normalized for name in _US_LOCATION_TERMS)


__all__ = ["LocalPortfolioScreener", "PortfolioPolicy", "is_us_location"]
