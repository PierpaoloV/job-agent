"""Read the candidate-owned, professional-only YAML evidence bank.

The source deliberately projects only explicitly approved professional claim
records. It never loads profile, health, demographic, contact, or other
non-evidence fields into the tailoring boundary.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from application_artifacts import EvidenceBankSnapshot, EvidenceRecord
from application_domain import ArtifactFamily, EvidenceKind


_FAMILY_ALIASES = {
    "research": ArtifactFamily.RESEARCH,
    "applied_ml": ArtifactFamily.CV_APPLIED_ML,
    "cv_applied_ml": ArtifactFamily.CV_APPLIED_ML,
    "agentic_ai": ArtifactFamily.AGENTIC_AI,
}
_RECORD_SECTIONS = ("highlights", "skill_evidence")


class YamlEvidenceSource:
    """Immutable projection of the candidate's YAML evidence bank."""

    def __init__(self, evidence_path: Path, canonical_cv_path: Path) -> None:
        self._evidence_path = Path(evidence_path)
        self._canonical_cv_path = Path(canonical_cv_path)

    def load(self) -> EvidenceBankSnapshot:
        evidence_bytes = self._evidence_path.read_bytes()
        canonical_cv_bytes = self._canonical_cv_path.read_bytes()
        payload = yaml.safe_load(evidence_bytes) or {}
        if not isinstance(payload, Mapping):
            raise ValueError("evidence bank must contain a YAML mapping")

        records = tuple(self._records(payload))
        return EvidenceBankSnapshot(
            version=_sha256(evidence_bytes),
            canonical_cv_version=_sha256(canonical_cv_bytes),
            evidence=records,
        )

    @staticmethod
    def _records(payload: Mapping[str, Any]) -> Iterable[EvidenceRecord]:
        for section in _RECORD_SECTIONS:
            rows = payload.get(section, [])
            if rows is None:
                continue
            if not isinstance(rows, list):
                raise ValueError(f"{section} must be a YAML list")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError(f"{section} entries must be YAML mappings")
                if not _is_approved(row):
                    continue
                yield EvidenceRecord(
                    evidence_id=_required_text(row, "id"),
                    families=_families(row.get("suitable_for")),
                    kinds=(EvidenceKind(_required_text(row, "kind")),),
                    approved_statement=_required_text(row, "claim"),
                    source_reference=_required_text(row, "evidence"),
                    approved=True,
                )


def _families(value: Any) -> tuple[ArtifactFamily, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("evidence suitable_for must be a non-empty YAML list")
    try:
        return tuple(_FAMILY_ALIASES[str(item)] for item in value)
    except KeyError as error:
        raise ValueError(f"unknown evidence family: {error.args[0]}") from error


def _required_text(row: Mapping[str, Any], key: str) -> str:
    value = str(row.get(key, "")).strip()
    if not value:
        raise ValueError(f"evidence entry requires {key}")
    return value


def _is_approved(row: Mapping[str, Any]) -> bool:
    value = row.get("approved", True)
    if not isinstance(value, bool):
        raise ValueError("evidence approved must be a YAML boolean")
    return value


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = ["YamlEvidenceSource"]
