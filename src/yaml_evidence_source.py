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
import pymupdf

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
    "application answers",
    "compensation",
    "demographics",
    "equal opportunity information",
    "health",
    "hobbies",
    "interests",
    "languages and interests",
    "personal details",
    "personal information",
    "references",
    "salary expectations",
    "work authorization",
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
_MODEL_SECTION_HEADINGS = {
    "education",
    "peer-reviewed publications and proceedings",
    "professional experience",
    "professional profile",
}
_SENSITIVE_LINE = re.compile(
    r"\b(?:"
    r"api[ _-]?key|access[ _-]?token|refresh[ _-]?token|auth(?:entication)?[ _-]?token|"
    r"ats answer|application answer|bearer token|citizen|citizenship|"
    r"client[ _-]?secret|compensation|credential|"
    r"date of birth|"
    r"demographic|diagnosis|diagnosed|disabil\w*|ethnic\w*|gender|health condition|"
    r"hobb(?:y|ies)|identity document|marital|nationality|oauth|passport|"
    r"passphrase|password|political|"
    r"private[ _-]?key|race|religio\w*|secret(?:[ _-]?key)?|social security|"
    r"salary expectation|ssh[ _-]?key|tax id|veteran|webhook[ _-]?(?:secret|token)|"
    r"work authori[sz]ation"
    r")\b",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,}|"
    r"\bghp_[A-Za-z0-9]{12,}|\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|\bAKIA[A-Z0-9]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_HEADER_CONTACT = re.compile(
    r"(?:@|https?://|www\.|github|linkedin|orcid|google scholar|"
    r"\+?\d[\d ()+.-]{6,}\d)",
    re.IGNORECASE,
)
_HEADER_PROFESSION = re.compile(
    r"\b(?:phd|researcher|scientist|engineer|machine learning|artificial intelligence)\b",
    re.IGNORECASE,
)
_PAGE_FOOTER = re.compile(
    r"(?:complete curriculum vitae|curriculum vitae)\s+\d+$", re.IGNORECASE
)
_CV_DATE_RANGE = re.compile(
    r"\b(?:19|20)\d{2}\b.*(?:[-‐‑‒–—−]|\bto\b|\bPresent\b|\bCurrent\b).*"
    r"(?:\b(?:19|20)\d{2}\b|\bPresent\b|\bCurrent\b)",
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
        with pymupdf.open(self._canonical_cv_path) as document:
            extracted_blocks = tuple(
                str(block[4]).strip()
                for page in document
                for block in page.get_text("blocks", sort=True)
                if str(block[4]).strip()
            )
        canonical_cv_text = _professional_cv_projection(
            extracted_blocks[0] if len(extracted_blocks) == 1 else extracted_blocks
        )
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


def _professional_cv_projection(value: str | Iterable[str]) -> str:
    if not isinstance(value, str):
        projected = _project_structured_blocks(tuple(value))
        if projected:
            return projected
        value = "\n".join(value)

    projected: list[str] = []
    blocked_section = True
    kept_identity = False
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
        if (
            _SENSITIVE_LINE.search(line)
            or _SECRET_VALUE.search(line)
            or _PAGE_FOOTER.search(line)
        ):
            continue
        if blocked_section:
            if not kept_identity:
                projected.append(line)
                kept_identity = True
                continue
            if not (_HEADER_CONTACT.search(line) or _HEADER_PROFESSION.search(line)):
                continue
        projected.append(line)
    return "\n".join(projected).strip()


def _project_structured_blocks(blocks: tuple[str, ...]) -> str:
    """Allowlist only model-needed professional PDF layout blocks."""

    result: list[str] = []
    section = "header"
    expect = ""
    publication_phase = 0
    pending_header: str | None = None
    for raw_block in blocks:
        lines = tuple(line.strip() for line in raw_block.splitlines() if line.strip())
        if not lines:
            continue
        if _is_sensitive_block(lines):
            if expect == "entry_detail":
                pending_header = None
                section = "blocked"
            continue
        first = _heading_key(lines[0])
        if _PAGE_FOOTER.search(" ".join(lines)):
            continue
        if first in _SAFE_SECTION_HEADINGS or first in _BLOCKED_SECTION_HEADINGS:
            if first in _MODEL_SECTION_HEADINGS:
                section = first
                expect = "entry_header" if first in {
                    "professional experience",
                    "education",
                } else ""
                publication_phase = 0
                pending_header = None
                result.append("\n".join(lines))
            else:
                section = "blocked"
            continue
        if section == "header":
            safe = tuple(line for line in lines if _safe_header_projection_line(line))
            if safe:
                result.append("\n".join(safe))
            continue
        if section == "professional profile":
            # The profile is retained only when it shares the allowlisted
            # heading block. A later standalone block is fail-closed.
            section = "blocked"
            continue
        if section in {"professional experience", "education"}:
            if expect == "entry_header" and _entry_header_block(lines):
                pending_header = "\n".join(lines)
                expect = "entry_detail"
                continue
            if expect == "entry_detail" and _entry_detail_block(lines, section):
                assert pending_header is not None
                result.append(pending_header)
                result.append("\n".join(lines))
                pending_header = None
                expect = "entry_header"
                continue
            pending_header = None
            section = "blocked"
            continue
        if section == "peer-reviewed publications and proceedings":
            if _publication_block(lines, publication_phase):
                result.append("\n".join(lines))
                publication_phase = (publication_phase + 1) % 3
                continue
            section = "blocked"
    return "\n\n".join(result).strip()


def _heading_key(value: str) -> str:
    normalized = re.sub(r"[‐‑‒–—−]", "-", str(value))
    return " ".join(normalized.casefold().split())


def _is_sensitive_block(lines: tuple[str, ...]) -> bool:
    return any(_SENSITIVE_LINE.search(line) or _SECRET_VALUE.search(line) for line in lines)


def _safe_header_projection_line(line: str) -> bool:
    if _SENSITIVE_LINE.search(line) or _SECRET_VALUE.search(line):
        return False
    return bool(
        not line.strip()
        or _HEADER_CONTACT.search(line)
        or _HEADER_PROFESSION.search(line)
        or (len(line.split()) <= 5 and not any(char.isdigit() for char in line))
    )


def _entry_header_block(lines: tuple[str, ...]) -> bool:
    return len(lines) == 2 and bool(_CV_DATE_RANGE.search(lines[1]))


def _entry_detail_block(lines: tuple[str, ...], section: str) -> bool:
    if len(lines) < 2:
        return False
    if section == "professional experience":
        return any(line.startswith("•") for line in lines[2:])
    return not _CV_DATE_RANGE.search(lines[1])


def _publication_block(lines: tuple[str, ...], phase: int) -> bool:
    text = " ".join(lines)
    if phase == 0:
        return len(text.split()) >= 6 and not re.search(r"\b(?:19|20)\d{2}\b", text)
    if phase == 1:
        return bool(re.search(r"\b(?:19|20)\d{2}\b", text))
    return bool(re.search(r"\b(?:doi:|pubmed|conference|first author)\b", text, re.I))


__all__ = ["YamlEvidenceSource"]
