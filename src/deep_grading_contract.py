"""Typed deep-grading request/response contract and validation codec."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Mapping

from requirements_evidence import (
    MATRIX_SCHEMA_VERSION,
    MatrixContractError,
    RequirementEvidence,
    RequirementsEvidenceMatrix,
)


SCHEMA_VERSION = "job-agent.deep-grade.v1"
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
COMPONENT_NAMES = (
    "fit",
    "research_preference",
    "geography",
    "compensation_confidence",
    "wealth_potential",
    "language",
    "immigration",
    "ownership",
    "freshness",
    "deadline",
    "risk",
)
class SponsorshipStatus(str, Enum):
    YES = "yes"
    NO = "no"
    NOT_STATED = "not_stated"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class CompensationStatus(str, Enum):
    PUBLISHED = "published"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"

_PROFILE_FIELDS = frozenset(
    {
        "professional_summary",
        "skills",
        "professional_evidence",
        "target_preferences",
    }
)
_PROFESSIONAL_EVIDENCE_FIELDS = frozenset(
    {"id", "claim", "context", "dates", "skills", "source_id"}
)
_PREFERENCE_FIELDS = frozenset(
    {
        "role_tracks",
        "geography",
        "research_preference",
        "language",
        "start_date",
        "work_authorization",
        "priority_companies",
        "priority_teams",
    }
)


class GradingContractError(ValueError):
    """The model response or grading input violated the public contract."""


@dataclass(frozen=True)
class SanitizedProfessionalProfile:
    provenance: str
    professional_summary: str
    skills: tuple[str, ...]
    professional_evidence: tuple[dict[str, Any], ...]
    target_preferences: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SanitizedProfessionalProfile":
        protected_terms = value.get("_private_redaction_terms", ())
        if (
            isinstance(protected_terms, str)
            or not isinstance(protected_terms, (list, tuple))
        ):
            raise GradingContractError(
                "_private_redaction_terms must be a list of private strings"
            )
        public_input = {
            key: item
            for key, item in value.items()
            if key != "_private_redaction_terms"
        }
        _validate_professional_profile_content(
            public_input,
            protected_terms=tuple(map(str, protected_terms)),
        )
        provenance = str(value.get("provenance", ""))
        if provenance != "canonical_cv_evidence_bank":
            raise GradingContractError(
                "grading profile requires canonical professional-evidence provenance"
            )
        allowed = {key: value[key] for key in _PROFILE_FIELDS if key in value}
        evidence = tuple(
            {
                key: public_value(item[key])
                for key in _PROFESSIONAL_EVIDENCE_FIELDS
                if key in item
            }
            for item in allowed.get("professional_evidence", ())
            if isinstance(item, Mapping)
        )
        preferences = allowed.get("target_preferences", {})
        profile = cls(
            provenance=provenance,
            professional_summary=str(allowed.get("professional_summary", "")),
            skills=tuple(map(str, allowed.get("skills", ()))),
            professional_evidence=evidence,
            target_preferences=(
                {
                    key: public_value(preferences[key])
                    for key in _PREFERENCE_FIELDS
                    if isinstance(preferences, Mapping) and key in preferences
                }
            ),
        )
        if any(not item.get("source_id") for item in profile.professional_evidence):
            raise GradingContractError(
                "professional evidence requires a canonical source_id"
            )
        return profile

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance,
            "professional_summary": self.professional_summary,
            "skills": list(self.skills),
            "professional_evidence": [dict(item) for item in self.professional_evidence],
            "target_preferences": dict(self.target_preferences),
        }


@dataclass(frozen=True)
class ExplainedScore:
    score: float
    explanation: str


@dataclass(frozen=True)
class ExplainedFlag:
    value: bool
    explanation: str


@dataclass(frozen=True)
class CompensationFact:
    value: str
    source: str
    date: str
    currency: str
    confidence: Confidence
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class CompensationPart:
    status: CompensationStatus
    facts: tuple[CompensationFact, ...]


@dataclass(frozen=True)
class WealthPotential:
    confidence: Confidence
    explanation: str
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class CompensationEvaluation:
    base_cash: CompensationPart
    bonus: CompensationPart
    equity: CompensationPart
    benchmarks: tuple[CompensationFact, ...]
    wealth_potential: WealthPotential


@dataclass(frozen=True)
class DatedEvidence:
    source: str
    verified_at: str


@dataclass(frozen=True)
class SponsorshipEvaluation(DatedEvidence):
    status: SponsorshipStatus
    visa_obstacle: bool


@dataclass(frozen=True)
class OwnershipEvaluation(DatedEvidence):
    classification: str


@dataclass(frozen=True)
class DeepGradeResult:
    schema_version: str
    opportunity_id: str
    vacancy_retrieved_at: str
    grading_input_fingerprint: str
    overall_score: float
    top_tier: ExplainedFlag
    rank_explanation: str
    components: dict[str, ExplainedScore]
    compensation: CompensationEvaluation
    sponsorship: SponsorshipEvaluation
    ownership: OwnershipEvaluation
    risks: tuple[str, ...]
    gaps: tuple[str, ...]
    requirements_evidence_matrix: RequirementsEvidenceMatrix
    sources: tuple[str, ...]

    @property
    def requirements_to_evidence(self) -> tuple[RequirementEvidence, ...]:
        """Compatibility view; new consumers persist and reuse the matrix."""
        return self.requirements_evidence_matrix.rows

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeepGradeResult":
        _reject_unsupported_market_claims(value)
        schema = str(value.get("schema_version", ""))
        if schema != SCHEMA_VERSION:
            raise GradingContractError(f"Unsupported deep-grade schema: {schema}")
        opportunity_id = str(value.get("opportunity_id", "")).strip()
        if not opportunity_id:
            raise GradingContractError("deep-grade opportunity id is required")
        vacancy_retrieved_at = str(
            value.get("vacancy_retrieved_at", "")
        ).strip()
        if not vacancy_retrieved_at:
            raise GradingContractError(
                "deep-grade retrieval timestamp is required"
            )
        try:
            retrieved_at = datetime.fromisoformat(vacancy_retrieved_at)
        except ValueError as exc:
            raise GradingContractError(
                "deep-grade retrieval timestamp must be ISO 8601"
            ) from exc
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise GradingContractError(
                "deep-grade retrieval timestamp must be timezone-aware"
            )
        grading_input_fingerprint = str(
            value.get("grading_input_fingerprint", "")
        ).strip()
        if not grading_input_fingerprint:
            raise GradingContractError(
                "deep-grade input fingerprint is required"
            )
        if not _CANONICAL_SHA256.fullmatch(grading_input_fingerprint):
            raise GradingContractError(
                "deep-grade input fingerprint must be canonical sha256"
            )
        component_values = value.get("components")
        if not isinstance(component_values, Mapping):
            raise GradingContractError("components must be an object")
        components: dict[str, ExplainedScore] = {}
        for name in COMPONENT_NAMES:
            item = component_values.get(name)
            if not isinstance(item, Mapping):
                raise GradingContractError(f"component {name} is required")
            explanation = str(item.get("explanation", "")).strip()
            if not explanation:
                raise GradingContractError(f"component {name} explanation is required")
            components[name] = ExplainedScore(
                score=_bounded_score(item.get("score"), f"component {name}"),
                explanation=explanation,
            )
        top_tier_value = _mapping(value.get("top_tier"), "top_tier")
        top_explanation = str(top_tier_value.get("explanation", "")).strip()
        if not top_explanation:
            raise GradingContractError("top-tier explanation is required")
        compensation = _compensation(value.get("compensation"))
        sponsorship = _sponsorship(value.get("sponsorship"))
        ownership = _ownership(value.get("ownership"))
        matrix_value = _mapping(
            value.get("requirements_evidence_matrix"),
            "requirements_evidence_matrix",
        )
        try:
            matrix = RequirementsEvidenceMatrix.from_dict(matrix_value)
        except (MatrixContractError, ValueError) as exc:
            raise GradingContractError(str(exc)) from exc
        if matrix.version != MATRIX_SCHEMA_VERSION:
            raise GradingContractError(
                f"Unsupported requirements evidence matrix: {matrix.version}"
            )
        rank_explanation = str(value.get("rank_explanation", "")).strip()
        if not rank_explanation:
            raise GradingContractError("rank explanation is required")
        return cls(
            schema_version=schema,
            opportunity_id=opportunity_id,
            vacancy_retrieved_at=vacancy_retrieved_at,
            grading_input_fingerprint=grading_input_fingerprint,
            overall_score=_bounded_score(value.get("overall_score"), "overall score"),
            top_tier=ExplainedFlag(
                value=_boolean(top_tier_value.get("value"), "top_tier value"),
                explanation=top_explanation,
            ),
            rank_explanation=rank_explanation,
            components=components,
            compensation=compensation,
            sponsorship=sponsorship,
            ownership=ownership,
            risks=tuple(map(str, value.get("risks", ()))),
            gaps=tuple(map(str, value.get("gaps", ()))),
            requirements_evidence_matrix=matrix,
            sources=tuple(map(str, value.get("sources", ()))),
        )


def public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): public_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [public_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _validate_professional_profile_content(
    value: Mapping[str, Any], *, protected_terms: tuple[str, ...] = ()
) -> None:
    forbidden_keys = {
        "api_key",
        "health",
        "diagnosis",
        "demographic",
        "race",
        "gender",
        "veteran",
        "passport",
        "identity_document",
        "credential",
        "password",
        "secret",
        "ats_answer",
        "salary_expectation",
    }
    forbidden_parts = {
        "credential",
        "demographic",
        "diagnosis",
        "disability",
        "health",
        "medical",
        "passport",
        "password",
        "secret",
    }
    forbidden_text = (
        "passport number",
        "social security number",
        "medical diagnosis",
        "salary expectation",
        *(
            term.strip().casefold()
            for term in protected_terms
            if len(term.strip()) >= 3
        ),
    )

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                split_camel_case = re.sub(
                    r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)
                )
                normalized = re.sub(
                    r"[^a-z0-9]+", "_", split_camel_case.casefold()
                ).strip("_")
                parts = {part for part in normalized.split("_") if part}
                if normalized in forbidden_keys or bool(parts & forbidden_parts):
                    raise GradingContractError(
                        "sensitive candidate data is not allowed in the grading profile"
                    )
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str) and any(
            term in item.casefold() for term in forbidden_text
        ):
            raise GradingContractError(
                "sensitive candidate data is not allowed in the grading profile"
            )

    walk(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _bounded_score(value: Any, label: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise GradingContractError(f"{label} score must be numeric") from exc
    if not 0 <= score <= 100:
        raise GradingContractError(f"{label} score must be between 0 and 100")
    return score


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise GradingContractError(f"{label} must be boolean")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GradingContractError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise GradingContractError(f"{label} must be an array")
    return value


def _compensation(value: Any) -> CompensationEvaluation:
    item = _mapping(value, "compensation")
    return CompensationEvaluation(
        base_cash=_compensation_part(item.get("base_cash"), "base_cash"),
        bonus=_compensation_part(item.get("bonus"), "bonus"),
        equity=_compensation_part(item.get("equity"), "equity"),
        benchmarks=tuple(
            _compensation_fact(fact, "benchmark")
            for fact in _sequence(item.get("benchmarks", ()), "benchmarks")
        ),
        wealth_potential=_wealth_potential(item.get("wealth_potential")),
    )


def _compensation_part(value: Any, label: str) -> CompensationPart:
    item = _mapping(value, label)
    try:
        status = CompensationStatus(str(item.get("status", "")))
    except ValueError:
        raise GradingContractError(f"{label} has invalid status")
    facts = tuple(
        _compensation_fact(fact, label)
        for fact in _sequence(item.get("facts", ()), f"{label} facts")
    )
    if status == CompensationStatus.UNKNOWN and facts:
        raise GradingContractError(f"{label} cannot be unknown and contain facts")
    if status == CompensationStatus.PUBLISHED and not facts:
        raise GradingContractError(f"published {label} requires a dated fact")
    return CompensationPart(status=status, facts=facts)


def _compensation_fact(value: Any, label: str) -> CompensationFact:
    fact = dict(_mapping(value, label))
    if not str(fact.get("source", "")).strip() or not str(fact.get("date", "")).strip():
        raise GradingContractError(f"{label} facts require source and date")
    required = {"currency", "confidence", "assumptions"}
    if not required.issubset(fact):
        raise GradingContractError(
            f"{label} facts require currency, confidence, and assumptions"
        )
    currency = str(fact.get("currency", "")).strip()
    if not currency:
        raise GradingContractError(
            f"{label} facts require currency, confidence, and assumptions"
        )
    try:
        confidence = Confidence(str(fact.get("confidence", "")))
    except ValueError:
        raise GradingContractError(f"{label} fact has invalid confidence") from None
    assumptions = _sequence(fact.get("assumptions"), f"{label} assumptions")
    return CompensationFact(
        value=str(fact.get("value", "")),
        source=str(fact["source"]),
        date=str(fact["date"]),
        currency=currency,
        confidence=confidence,
        assumptions=tuple(map(str, assumptions)),
    )


def _wealth_potential(value: Any) -> WealthPotential:
    item = _mapping(value, "wealth_potential")
    try:
        confidence = Confidence(str(item.get("confidence", "")))
    except ValueError:
        raise GradingContractError("wealth_potential has invalid confidence")
    explanation = str(item.get("explanation", "")).strip()
    if not explanation:
        raise GradingContractError("wealth_potential explanation is required")
    return WealthPotential(
        confidence=confidence,
        explanation=explanation,
        assumptions=tuple(map(str, item.get("assumptions", ()))),
    )


def _sponsorship(value: Any) -> SponsorshipEvaluation:
    item = _mapping(value, "sponsorship")
    try:
        status = SponsorshipStatus(str(item.get("status", "")))
    except ValueError:
        raise GradingContractError("sponsorship status must be yes, no, or not_stated")
    source = str(item.get("source", "")).strip()
    verified_at = str(item.get("verified_at", "")).strip()
    if not source or not verified_at:
        raise GradingContractError("sponsorship requires source and verification date")
    visa_obstacle = _boolean(item.get("visa_obstacle"), "visa_obstacle")
    return SponsorshipEvaluation(
        source=source,
        verified_at=verified_at,
        status=status,
        visa_obstacle=visa_obstacle,
    )


def _ownership(value: Any) -> OwnershipEvaluation:
    item = _mapping(value, "ownership")
    source = str(item.get("source", "")).strip()
    verified_at = str(item.get("verified_at", "")).strip()
    if not source or not verified_at:
        raise GradingContractError("ownership requires source and verification date")
    return OwnershipEvaluation(
        source=source,
        verified_at=verified_at,
        classification=str(item.get("classification", "unknown")),
    )


def _reject_unsupported_market_claims(value: Mapping[str, Any]) -> None:
    def walk(item: Any) -> bool:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = str(key).casefold().replace("-", "_")
                if "saturation" in normalized or normalized.startswith("market_score"):
                    return True
                if walk(child):
                    return True
        elif isinstance(item, (list, tuple)):
            return any(walk(child) for child in item)
        elif isinstance(item, str) and "saturation" in item.casefold():
            return True
        return False

    if walk(value):
        raise GradingContractError("unsupported market or saturation metric")


__all__ = [
    "COMPONENT_NAMES",
    "SCHEMA_VERSION",
    "DeepGradeResult",
    "ExplainedFlag",
    "GradingContractError",
    "SanitizedProfessionalProfile",
    "public_value",
]
