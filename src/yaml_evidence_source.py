"""Read candidate-owned professional evidence and a safe canonical-CV view.

The source projects explicitly approved claim records and extractable
professional CV text. It removes personal health, demographic,
identity-document, credential, and ATS-answer lines before the model boundary.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import yaml
from pypdf import PdfReader

from application_artifacts import EvidenceBankSnapshot, EvidenceRecord
from application_domain import ArtifactFamily, EvidenceKind


_FAMILY_ALIASES = {
    "research": ArtifactFamily.RESEARCH,
    "applied_ml": ArtifactFamily.CV_APPLIED_ML,
    "cv_applied_ml": ArtifactFamily.CV_APPLIED_ML,
    "agentic_ai": ArtifactFamily.AGENTIC_AI,
}
_RECORD_SECTIONS = ("highlights", "skill_evidence")
_BLOCKED_SECTION_HEADINGS = {
    "demographics",
    "health",
    "interests",
    "languages and interests",
    "personal details",
    "personal information",
}
_SAFE_SECTION_HEADINGS = {
    "certifications and additional learning",
    "complete skills inventory",
    "education",
    "honors, fellowships, and grants",
    "open-source and personal engineering projects",
    "peer-reviewed publications and proceedings",
    "professional experience",
    "professional profile",
    "research programs and technical contributions",
    "scientific service and university governance",
    "teaching, supervision, and mentoring",
}
_SENSITIVE_LINE = re.compile(
    r"\b(?:"
    r"api key|access token|ats answer|citizen|citizenship|date of birth|"
    r"demographic|diagnos\w*|disabil\w*|ethnic\w*|gender|health condition|"
    r"identity document|marital|nationality|passport|password|race|religio\w*|"
    r"social security|tax id"
    r")\b",
    re.IGNORECASE,
)


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
        extracted_cv_text = "\n".join(
            (page.extract_text() or "").strip()
            for page in PdfReader(self._canonical_cv_path).pages
        ).strip()
        canonical_cv_text = _professional_cv_projection(extracted_cv_text)
        if not canonical_cv_text:
            raise ValueError("canonical CV must contain extractable text")
        return EvidenceBankSnapshot(
            version=_sha256(evidence_bytes),
            canonical_cv_version=_sha256(canonical_cv_bytes),
            evidence=records,
            canonical_cv_text=canonical_cv_text,
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


def _professional_cv_projection(value: str) -> str:
    projected: list[str] = []
    blocked_section = False
    for raw_line in str(value).splitlines():
        line = raw_line.strip()
        if not line:
            if projected and projected[-1]:
                projected.append("")
            continue
        heading = " ".join(line.casefold().split())
        if heading in _BLOCKED_SECTION_HEADINGS:
            blocked_section = True
            continue
        if heading in _SAFE_SECTION_HEADINGS:
            blocked_section = False
        if blocked_section or _SENSITIVE_LINE.search(line):
            continue
        projected.append(line)
    return "\n".join(projected).strip()


__all__ = ["YamlEvidenceSource"]
