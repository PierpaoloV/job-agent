"""One-call orchestration and deterministic portfolio guardrails."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Mapping, Protocol

from deep_grading_contract import (
    COMPONENT_NAMES,
    SCHEMA_VERSION,
    DeepGradeResult,
    ExplainedFlag,
    GradingContractError,
    SanitizedProfessionalProfile,
    public_value,
)
from deep_grading_store import DeepGradeStore
from portfolio_policy import is_us_location
from requirements_evidence import MatrixContractError
from vacancy_policy import (
    HardPolicy,
    OFFICIAL_GRADING_FIELDS,
    ScreeningOutcome,
    VerificationState,
    hard_policy_exclusion,
    screening_outcome,
    verification_state,
)


_PUBLIC_EVIDENCE_FIELDS = {
    "compensation": frozenset(
        {"status", "base_cash", "bonus", "equity", "published_range", "facts"}
    ),
    "sponsorship": frozenset({"status", "source", "verified_at"}),
    "ownership": frozenset({"classification", "source", "verified_at"}),
}


class GradingProvider(Protocol):
    def complete(self, request: Mapping[str, Any]) -> Mapping[str, Any] | str: ...


@dataclass(frozen=True)
class TopTierPolicy:
    minimum_fit: float = 75
    exceptional_overall: float = 84
    priority_overall: float = 78


class DeepGradingService:
    def __init__(
        self,
        *,
        provider: GradingProvider,
        store: DeepGradeStore,
        top_tier_policy: TopTierPolicy = TopTierPolicy(),
        hard_policy: HardPolicy = HardPolicy(),
    ) -> None:
        self._provider = provider
        self._store = store
        self._top_tier_policy = top_tier_policy
        self._hard_policy = hard_policy

    def grade_if_eligible(
        self,
        vacancy: Mapping[str, Any],
        profile: SanitizedProfessionalProfile,
    ) -> DeepGradeResult | None:
        if verification_state(vacancy.get("verification_status")) != VerificationState.VERIFIED:
            return None
        if hard_policy_exclusion(vacancy, self._hard_policy) is not None:
            return None
        if screening_outcome(vacancy.get("screening_outcome")) in {
            ScreeningOutcome.FILTERED,
            ScreeningOutcome.OVERFLOW,
            ScreeningOutcome.NEEDS_LOCAL_FETCH,
        }:
            return None
        if not str(vacancy.get("official_description", "")).strip():
            return None
        return self.grade(vacancy, profile)

    def grading_input_fingerprint(
        self,
        vacancy: Mapping[str, Any],
        profile: SanitizedProfessionalProfile,
    ) -> str:
        """Return the exact cache identity before crossing the provider boundary."""

        if verification_state(vacancy.get("verification_status")) != VerificationState.VERIFIED:
            raise GradingContractError("Deep grading requires a verified vacancy")
        exclusion = hard_policy_exclusion(vacancy, self._hard_policy)
        if exclusion is not None:
            raise GradingContractError(exclusion)
        if not str(vacancy.get("official_description", "")).strip():
            raise GradingContractError("Deep grading requires the official description")
        public_vacancy = _sanitize_vacancy(vacancy)
        if not str(public_vacancy.get("stable_id", "")).strip():
            raise GradingContractError("Deep grading requires a stable opportunity id")
        return _grading_input_fingerprint(
            _request(public_vacancy, profile),
            self._provider,
        )

    def cached_grade(
        self, opportunity_id: str, grading_input_fingerprint: str
    ) -> DeepGradeResult | None:
        """Read only an exact persisted grade suitable for reconciliation."""

        return self._cached(opportunity_id, grading_input_fingerprint)

    def grade(
        self,
        vacancy: Mapping[str, Any],
        profile: SanitizedProfessionalProfile,
    ) -> DeepGradeResult:
        if verification_state(vacancy.get("verification_status")) != VerificationState.VERIFIED:
            raise GradingContractError("Deep grading requires a verified vacancy")
        exclusion = hard_policy_exclusion(vacancy, self._hard_policy)
        if exclusion is not None:
            raise GradingContractError(exclusion)
        if not str(vacancy.get("official_description", "")).strip():
            raise GradingContractError("Deep grading requires the official description")
        public_vacancy = _sanitize_vacancy(vacancy)
        opportunity_id = str(public_vacancy.get("stable_id", "")).strip()
        if not opportunity_id:
            raise GradingContractError("Deep grading requires a stable opportunity id")
        request = _request(public_vacancy, profile)
        fingerprint = _grading_input_fingerprint(request, self._provider)
        cached = self._cached(opportunity_id, fingerprint)
        if cached is not None:
            return cached
        raw = self._provider.complete(request)
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        parsed["opportunity_id"] = opportunity_id
        parsed["vacancy_retrieved_at"] = str(public_vacancy.get("retrieved_at", ""))
        parsed["grading_input_fingerprint"] = fingerprint
        matrix = parsed.get("requirements_evidence_matrix")
        if isinstance(matrix, Mapping):
            official_version = public_vacancy.get("official_vacancy_version")
            parsed["requirements_evidence_matrix"] = {
                **matrix,
                "official_vacancy_version": (
                    None if official_version is None else str(official_version)
                ),
            }
        result = DeepGradeResult.from_dict(parsed)
        _validate_matrix_against_inputs(result, public_vacancy, profile)
        if (
            is_us_location(str(public_vacancy.get("location", "")))
            and profile.target_preferences.get("requires_us_sponsorship") is True
            and result.sponsorship.status == "no"
            and not result.sponsorship.visa_obstacle
        ):
            raise GradingContractError(
                "US no-sponsorship roles must enter the visa-obstacle section"
            )
        result = _apply_top_tier_policy(
            result, public_vacancy, profile, self._top_tier_policy
        )
        self._store.save(result)
        return result

    def _cached(
        self, opportunity_id: str, fingerprint: str
    ) -> DeepGradeResult | None:
        try:
            cached = self._store.load(opportunity_id)
        except KeyError:
            return None
        return cached if cached.grading_input_fingerprint == fingerprint else None


def _request(
    vacancy: Mapping[str, Any], profile: SanitizedProfessionalProfile
) -> dict[str, Any]:
    return {
        "contract": {
            "schema_version": SCHEMA_VERSION,
            "required_components": list(COMPONENT_NAMES),
            "instructions": [
                "Return one JSON object containing the portfolio evaluation and requirements_evidence_matrix.",
                "Keep unknown compensation eligible; separate base cash, bonus, equity, benchmarks, and inferred wealth potential.",
                "Use sponsorship yes, no, or not_stated with source and verification date; evaluate obstacles from the supplied work-authorization preferences.",
                "Apply only the target preferences supplied in the professional grading profile.",
                "Treat user preferences as ranking inputs unless they explicitly declare a hard exclusion.",
                "Do not invent saturation measures or market facts. Every compensation fact or benchmark needs a source and date.",
                "The matrix uses canonical rows with id, requirement, importance, status, evidence_ids, and explanation.",
                "Explain top-tier classification and every score component concisely.",
            ],
        },
        "official_vacancy": vacancy,
        "professional_grading_profile": profile.to_dict(),
    }


def _sanitize_vacancy(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in OFFICIAL_GRADING_FIELDS:
        if key not in value:
            continue
        item = value[key]
        nested_fields = _PUBLIC_EVIDENCE_FIELDS.get(key)
        if nested_fields is not None and isinstance(item, Mapping):
            sanitized[key] = {
                nested_key: _vacancy_value(item[nested_key])
                for nested_key in nested_fields
                if nested_key in item
            }
        else:
            sanitized[key] = _vacancy_value(item)
    return sanitized


def _grading_input_fingerprint(
    request: Mapping[str, Any], provider: GradingProvider
) -> str:
    provider_identity = str(
        getattr(provider, "identity", provider.__class__.__qualname__)
    )
    encoded = json.dumps(
        {"provider": provider_identity, "request": request},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _vacancy_value(value: Any) -> Any:
    forbidden = {
        "ats_answer",
        "salary_expectation",
        "health",
        "diagnosis",
        "demographic",
        "passport",
        "credential",
        "password",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _vacancy_value(item)
            for key, item in value.items()
            if str(key).casefold().replace("-", "_") not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [_vacancy_value(item) for item in value]
    return public_value(value)


def _validate_matrix_against_inputs(
    result: DeepGradeResult,
    vacancy: Mapping[str, Any],
    profile: SanitizedProfessionalProfile,
) -> None:
    known_evidence = {
        str(item.get("id"))
        for item in profile.professional_evidence
        if item.get("id")
    }
    official_requirements = {
        str(item).strip()
        for item in vacancy.get("requirements", ())
        if str(item).strip()
    }
    try:
        result.requirements_evidence_matrix.validate_evidence_ids(known_evidence)
        result.requirements_evidence_matrix.validate_official_requirements(
            official_requirements
        )
    except MatrixContractError as exc:
        raise GradingContractError(str(exc)) from exc


def _apply_top_tier_policy(
    result: DeepGradeResult,
    vacancy: Mapping[str, Any],
    profile: SanitizedProfessionalProfile,
    policy: TopTierPolicy,
) -> DeepGradeResult:
    preferences = profile.target_preferences
    priority_companies = {
        str(item).casefold() for item in preferences.get("priority_companies", ())
    }
    priority_teams = {
        str(item).casefold() for item in preferences.get("priority_teams", ())
    }
    priority_match = (
        str(vacancy.get("company", "")).casefold() in priority_companies
        or str(vacancy.get("team", "")).casefold() in priority_teams
    )
    top_tier = result.components["fit"].score >= policy.minimum_fit and (
        result.overall_score >= policy.exceptional_overall
        or (priority_match and result.overall_score >= policy.priority_overall)
    )
    return replace(
        result,
        top_tier=ExplainedFlag(
            value=top_tier,
            explanation=(
                "Top-tier: strong real fit and exceptional portfolio score"
                + (" with priority company/team alignment" if priority_match else "")
                if top_tier
                else "Not top-tier under the configured fit and portfolio thresholds"
            ),
        ),
    )


__all__ = ["DeepGradingService", "GradingProvider", "TopTierPolicy"]
