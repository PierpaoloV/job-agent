"""Canonical requirements-to-evidence matrix shared across grading and tailoring."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


MATRIX_SCHEMA_VERSION = "job-agent.requirements-evidence.v1"


class MatrixContractError(ValueError):
    pass


class RequirementImportance(str, Enum):
    REQUIRED = "required"
    PREFERRED = "preferred"


class RequirementStatus(str, Enum):
    MATCHED = "matched"
    PARTIAL = "partial"
    GAP = "gap"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RequirementEvidence:
    id: str
    requirement: str
    importance: RequirementImportance
    status: RequirementStatus
    evidence_ids: tuple[str, ...]
    explanation: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RequirementEvidence":
        row_id = str(value.get("id") or value.get("requirement_id") or "").strip()
        requirement = str(value.get("requirement", "")).strip()
        explanation = str(value.get("explanation", "")).strip()
        if not row_id or not requirement or not explanation:
            raise MatrixContractError(
                "matrix rows require id, requirement, and explanation"
            )
        raw_status = str(value.get("status", ""))
        status = (
            RequirementStatus.MATCHED
            if raw_status == "supported"
            else RequirementStatus(raw_status)
        )
        evidence_ids = tuple(map(str, value.get("evidence_ids", ())))
        if status in {RequirementStatus.MATCHED, RequirementStatus.PARTIAL} and not evidence_ids:
            raise MatrixContractError(
                "matched or partial requirements need professional evidence"
            )
        return cls(
            id=row_id,
            requirement=requirement,
            importance=RequirementImportance(str(value.get("importance", ""))),
            status=status,
            evidence_ids=evidence_ids,
            explanation=explanation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement": self.requirement,
            "importance": self.importance.value,
            "status": self.status.value,
            "evidence_ids": list(self.evidence_ids),
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class RequirementsEvidenceMatrix:
    version: str
    rows: tuple[RequirementEvidence, ...]
    official_vacancy_version: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RequirementsEvidenceMatrix":
        version = str(value.get("version", "")).strip()
        if not version:
            raise MatrixContractError("requirements evidence matrix version is required")
        payload = value.get("rows")
        if payload is None:
            payload = value.get("requirements")
        if not isinstance(payload, (list, tuple)) or not payload:
            raise MatrixContractError("requirements evidence matrix rows are required")
        matrix = cls(
            version=version,
            rows=tuple(RequirementEvidence.from_dict(item) for item in payload),
            official_vacancy_version=(
                None
                if value.get("official_vacancy_version") is None
                else str(value["official_vacancy_version"])
            ),
        )
        matrix._validate_unique()
        return matrix

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "official_vacancy_version": self.official_vacancy_version,
            "rows": [row.to_dict() for row in self.rows],
        }

    @property
    def content_digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    def validate_evidence_ids(self, known_ids: set[str]) -> None:
        referenced = {item for row in self.rows for item in row.evidence_ids}
        if referenced - known_ids:
            raise MatrixContractError(
                "requirements evidence matrix cites unknown professional evidence"
            )

    def validate_official_requirements(self, requirements: set[str]) -> None:
        normalized_rows = {row.requirement.strip().casefold() for row in self.rows}
        normalized_required = {item.strip().casefold() for item in requirements if item.strip()}
        if not normalized_required.issubset(normalized_rows):
            raise MatrixContractError(
                "requirements evidence matrix omits an official requirement"
            )

    def report_projection(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "id": row.id,
                "requirement": row.requirement,
                "importance": row.importance.value,
                "status": row.status.value,
                "evidence_ids": list(row.evidence_ids),
                "explanation": row.explanation,
            }
            for row in self.rows
        )

    def _validate_unique(self) -> None:
        identifiers = [row.id for row in self.rows]
        requirements = [row.requirement.casefold() for row in self.rows]
        if len(identifiers) != len(set(identifiers)) or len(requirements) != len(
            set(requirements)
        ):
            raise MatrixContractError("requirements evidence matrix contains duplicates")


__all__ = [
    "MATRIX_SCHEMA_VERSION",
    "MatrixContractError",
    "RequirementEvidence",
    "RequirementImportance",
    "RequirementStatus",
    "RequirementsEvidenceMatrix",
]
