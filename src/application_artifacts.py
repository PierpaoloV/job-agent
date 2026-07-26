"""Truth-preserving generation of versioned application artifact bundles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from application_domain import (
    ArtifactClaimTrace,
    ArtifactDocument,
    ArtifactFamily,
    EvidenceKind,
    OfficialVacancy,
    PreparedArtifacts,
    StretchDecision,
)
from requirements_evidence import (
    RequirementEvidence,
    RequirementImportance,
    RequirementStatus,
    RequirementsEvidenceMatrix,
)


# Compatibility name for the tailoring boundary; the codec lives in one module.
DeepGradingMatrix = RequirementsEvidenceMatrix


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    families: tuple[ArtifactFamily, ...]
    kinds: tuple[EvidenceKind, ...]
    approved_statement: str
    source_reference: str
    approved: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "families", tuple(ArtifactFamily(item) for item in self.families)
        )
        object.__setattr__(
            self, "kinds", tuple(EvidenceKind(item) for item in self.kinds)
        )


@dataclass(frozen=True)
class EvidenceBankSnapshot:
    """Immutable read model derived from the candidate-owned master sources."""

    version: str
    canonical_cv_version: str
    evidence: tuple[EvidenceRecord, ...]


@dataclass(frozen=True)
class MaterialClaim:
    statement: str
    kind: EvidenceKind
    evidence_ids: tuple[str, ...]
    appears_in: tuple[ArtifactDocument, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceKind(self.kind))
        object.__setattr__(
            self,
            "appears_in",
            tuple(ArtifactDocument(item) for item in self.appears_in),
        )


@dataclass(frozen=True)
class GeneratedArtifactBundle:
    cv_text: str
    cover_letter_text: str
    claims: tuple[MaterialClaim, ...]


@dataclass(frozen=True)
class ClaimAudit:
    """Independent, full-document inventory of supported material claims."""

    claims: tuple[MaterialClaim, ...]
    unsupported_claims: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class RenderedArtifactBundle:
    cv_path: str
    cover_letter_path: str
    cv_hash: str
    cover_letter_hash: str


@dataclass(frozen=True)
class TailoringRequest:
    application_id: str
    intent_id: str
    family: ArtifactFamily
    official_vacancy: OfficialVacancy
    matrix: DeepGradingMatrix
    canonical_cv_version: str
    evidence: tuple[EvidenceRecord, ...]
    stretch_decision: StretchDecision


class ReadOnlyEvidenceSource(Protocol):
    def load(self) -> EvidenceBankSnapshot: ...


class ArtifactGenerator(Protocol):
    def generate(self, request: TailoringRequest) -> GeneratedArtifactBundle: ...


class ClaimAuditor(Protocol):
    def audit(
        self,
        generated: GeneratedArtifactBundle,
        evidence: tuple[EvidenceRecord, ...],
    ) -> ClaimAudit: ...


class ArtifactRenderer(Protocol):
    def render(
        self,
        *,
        application_id: str,
        bundle_version: str,
        cv_text: str,
        cover_letter_text: str,
    ) -> RenderedArtifactBundle: ...

    def publish(
        self,
        *,
        application_id: str,
        bundle_version: str,
        rendered: RenderedArtifactBundle,
    ) -> RenderedArtifactBundle: ...


class TruthfulApplicationArtifactService:
    """Build CV and cover letter together from approved, traceable evidence."""

    def __init__(
        self,
        *,
        evidence_source: ReadOnlyEvidenceSource,
        generator: ArtifactGenerator,
        claim_auditor: ClaimAuditor,
        renderer: ArtifactRenderer,
    ) -> None:
        self._evidence_source = evidence_source
        self._generator = generator
        self._claim_auditor = claim_auditor
        self._renderer = renderer
        self._evidence_snapshot: EvidenceBankSnapshot | None = None
    def reload_master_cv(self) -> str:
        """Rebuild the read model; the candidate-owned source is never written."""

        snapshot = self._evidence_source.load()
        self._validate_snapshot(snapshot)
        self._evidence_snapshot = snapshot
        return snapshot.version

    @staticmethod
    def verify_artifacts(artifacts: PreparedArtifacts) -> bool:
        """Verify that published files still match the approved bundle hashes."""

        return all(
            _matches_file_hash(path, expected)
            for path, expected in (
                (artifacts.cv_path, artifacts.cv_hash),
                (artifacts.cover_letter_path, artifacts.cover_letter_hash),
            )
        )

    def prepare(
        self,
        application_id: str,
        intent_id: str,
        opportunity: Mapping[str, Any],
        official_vacancy: OfficialVacancy,
    ) -> PreparedArtifacts:
        if (
            not official_vacancy.available
            or not official_vacancy.verified
            or not official_vacancy.description.strip()
        ):
            raise ValueError("a verified official vacancy is required")
        matrix_payload = opportunity.get("requirements_evidence_matrix")
        if not isinstance(matrix_payload, Mapping):
            raise ValueError("persisted deep-grading matrix is required")
        matrix = DeepGradingMatrix.from_dict(matrix_payload)
        if matrix.official_vacancy_version != official_vacancy.version:
            raise ValueError("deep-grading matrix does not match official vacancy")
        family = ArtifactFamily(str(opportunity.get("artifact_family", "")))
        snapshot = self._snapshot()
        matrix.validate_evidence_ids(
            {item.evidence_id for item in snapshot.evidence if item.approved}
        )
        evidence = self._select_evidence(snapshot, matrix, family)
        effective_matrix = self._effective_matrix(matrix, evidence)
        stretch = self._stretch_decision(effective_matrix)
        request = TailoringRequest(
            application_id=application_id,
            intent_id=intent_id,
            family=family,
            official_vacancy=official_vacancy,
            matrix=effective_matrix,
            canonical_cv_version=snapshot.canonical_cv_version,
            evidence=evidence,
            stretch_decision=stretch,
        )
        generated = self._generator.generate(request)
        audit = self._claim_auditor.audit(generated, evidence)
        if not audit.complete:
            raise ValueError("full-document material claim audit is incomplete")
        if audit.unsupported_claims:
            raise ValueError(
                "unsupported material claims: "
                + "; ".join(audit.unsupported_claims)
            )
        traces = self._validate_claims(generated, evidence, audit.claims)
        generation_version = self._generation_version(
            generated=generated,
            traces=traces,
            snapshot=snapshot,
            matrix=effective_matrix,
            stretch=stretch,
            family=family,
            official_vacancy=official_vacancy,
        )
        rendered = self._renderer.render(
            application_id=application_id,
            bundle_version=generation_version,
            cv_text=generated.cv_text,
            cover_letter_text=generated.cover_letter_text,
        )
        version = self._rendered_bundle_version(generation_version, rendered)
        rendered = self._renderer.publish(
            application_id=application_id,
            bundle_version=version,
            rendered=rendered,
        )
        if version != self._rendered_bundle_version(generation_version, rendered):
            raise ValueError("publishing changed the rendered artifact bytes")
        return PreparedArtifacts(
            version=version,
            cv_path=rendered.cv_path,
            cover_letter_path=rendered.cover_letter_path,
            cv_hash=rendered.cv_hash,
            cover_letter_hash=rendered.cover_letter_hash,
            evidence_source_version=snapshot.version,
            matrix_version=matrix.version,
            family=family,
            claims=traces,
            stretch_decision=stretch,
        )

    def _snapshot(self) -> EvidenceBankSnapshot:
        if self._evidence_snapshot is None:
            self.reload_master_cv()
        assert self._evidence_snapshot is not None
        return self._evidence_snapshot

    @staticmethod
    def _validate_snapshot(snapshot: EvidenceBankSnapshot) -> None:
        identifiers = [item.evidence_id for item in snapshot.evidence]
        if (
            not snapshot.version
            or not snapshot.canonical_cv_version
        ):
            raise ValueError("evidence snapshot must be versioned")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evidence identifiers must be unique")
        if any(
            item.approved
            and (not item.approved_statement.strip() or not item.source_reference.strip())
            for item in snapshot.evidence
        ):
            raise ValueError("approved evidence must preserve statement and source")

    @staticmethod
    def _select_evidence(
        snapshot: EvidenceBankSnapshot,
        matrix: DeepGradingMatrix,
        family: ArtifactFamily,
    ) -> tuple[EvidenceRecord, ...]:
        permitted_ids = {
            evidence_id
            for requirement in matrix.rows
            if requirement.status
            in {RequirementStatus.MATCHED, RequirementStatus.PARTIAL}
            for evidence_id in requirement.evidence_ids
        }
        return tuple(
            item
            for item in snapshot.evidence
            if item.approved
            and item.evidence_id in permitted_ids
            and family in item.families
        )

    @staticmethod
    def _stretch_decision(matrix: DeepGradingMatrix) -> StretchDecision:
        gaps = tuple(
            item
            for item in matrix.rows
            if item.importance == RequirementImportance.REQUIRED
            and item.status
            in {
                RequirementStatus.PARTIAL,
                RequirementStatus.GAP,
                RequirementStatus.UNKNOWN,
            }
        )
        if not gaps:
            return StretchDecision(False)
        explanations = tuple(
            item.explanation
            or (
                f"Only partial approved evidence for {item.requirement}."
                if item.status == RequirementStatus.PARTIAL
                else f"No approved evidence for {item.requirement}."
            )
            for item in gaps
        )
        return StretchDecision(
            True,
            tuple(item.requirement for item in gaps),
            " ".join(explanations),
        )

    @staticmethod
    def _effective_matrix(
        matrix: DeepGradingMatrix,
        evidence: tuple[EvidenceRecord, ...],
    ) -> DeepGradingMatrix:
        available = {item.evidence_id for item in evidence}
        rows = tuple(
            TruthfulApplicationArtifactService._project_requirement_to_family(
                item, available
            )
            for item in matrix.rows
        )
        return DeepGradingMatrix(
            version=matrix.version,
            rows=rows,
            official_vacancy_version=matrix.official_vacancy_version,
        )

    @staticmethod
    def _project_requirement_to_family(
        requirement: RequirementEvidence,
        available: set[str],
    ) -> RequirementEvidence:
        if requirement.status not in {
            RequirementStatus.MATCHED,
            RequirementStatus.PARTIAL,
        }:
            return requirement
        surviving = tuple(
            evidence_id
            for evidence_id in requirement.evidence_ids
            if evidence_id in available
        )
        if not surviving:
            return RequirementEvidence(
                id=requirement.id,
                requirement=requirement.requirement,
                importance=requirement.importance,
                status=RequirementStatus.GAP,
                evidence_ids=(),
                explanation=(
                    requirement.explanation
                    or "Referenced evidence is unavailable in the selected CV family."
                ),
            )
        if len(surviving) == len(requirement.evidence_ids):
            return requirement
        explanation = requirement.explanation.strip()
        family_note = "Some cited evidence is unavailable in the selected CV family."
        return RequirementEvidence(
            id=requirement.id,
            requirement=requirement.requirement,
            importance=requirement.importance,
            status=RequirementStatus.PARTIAL,
            evidence_ids=surviving,
            explanation=f"{explanation} {family_note}".strip(),
        )

    @staticmethod
    def _validate_claims(
        generated: GeneratedArtifactBundle,
        evidence: tuple[EvidenceRecord, ...],
        audited_claims: tuple[MaterialClaim, ...],
    ) -> tuple[ArtifactClaimTrace, ...]:
        if not generated.cv_text.strip() or not generated.cover_letter_text.strip():
            raise ValueError("generator must return both CV and cover letter")
        if not audited_claims:
            raise ValueError("generated artifacts must declare material claim traces")
        approved = {item.evidence_id: item for item in evidence}
        traces = []
        for claim in audited_claims:
            if not claim.statement.strip() or not claim.evidence_ids:
                raise ValueError("every material claim requires approved evidence")
            unknown = set(claim.evidence_ids) - set(approved)
            if unknown:
                raise ValueError(
                    "unsupported evidence for professional claim: "
                    + ", ".join(sorted(unknown))
                )
            if any(claim.kind not in approved[item].kinds for item in claim.evidence_ids):
                raise ValueError(
                    f"unsupported {claim.kind.value} claim for its cited evidence"
                )
            if not claim.appears_in or not set(claim.appears_in).issubset(
                {ArtifactDocument.CV, ArtifactDocument.COVER_LETTER}
            ):
                raise ValueError("claim location must identify CV or cover letter")
            documents = {
                ArtifactDocument.CV: generated.cv_text,
                ArtifactDocument.COVER_LETTER: generated.cover_letter_text,
            }
            if any(
                claim.statement not in documents[document]
                for document in claim.appears_in
            ):
                raise ValueError("claim trace does not match generated document text")
            traces.append(
                ArtifactClaimTrace(
                    statement=claim.statement,
                    kind=claim.kind,
                    evidence_ids=claim.evidence_ids,
                    appears_in=claim.appears_in,
                )
            )
        return tuple(traces)

    def _generation_version(
        self,
        *,
        generated: GeneratedArtifactBundle,
        traces: tuple[ArtifactClaimTrace, ...],
        snapshot: EvidenceBankSnapshot,
        matrix: DeepGradingMatrix,
        stretch: StretchDecision,
        family: ArtifactFamily,
        official_vacancy: OfficialVacancy,
    ) -> str:
        payload = {
            "cv_text": generated.cv_text,
            "cover_letter_text": generated.cover_letter_text,
            "claims": [asdict(item) for item in traces],
            "evidence_source_version": snapshot.version,
            "canonical_cv_version": snapshot.canonical_cv_version,
            "matrix": matrix.to_dict(),
            "stretch_decision": asdict(stretch),
            "family": family.value,
            "official_vacancy_version": official_vacancy.version,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _rendered_bundle_version(
        generation_version: str, rendered: RenderedArtifactBundle
    ) -> str:
        if not rendered.cv_hash.startswith("sha256:") or not (
            rendered.cover_letter_hash.startswith("sha256:")
        ):
            raise ValueError("renderer must return content hashes for both artifacts")
        payload = {
            "generation_version": generation_version,
            "cv_hash": rendered.cv_hash,
            "cover_letter_hash": rendered.cover_letter_hash,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _matches_file_hash(path: str, expected: str) -> bool:
    if not expected.startswith("sha256:"):
        return False
    artifact = Path(path)
    if not artifact.is_file():
        return False
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return expected == f"sha256:{digest}"


__all__ = [
    "ArtifactDocument",
    "ArtifactFamily",
    "ClaimAudit",
    "DeepGradingMatrix",
    "EvidenceBankSnapshot",
    "EvidenceKind",
    "EvidenceRecord",
    "GeneratedArtifactBundle",
    "MaterialClaim",
    "RenderedArtifactBundle",
    "RequirementImportance",
    "RequirementEvidence",
    "RequirementStatus",
    "TailoringRequest",
    "TruthfulApplicationArtifactService",
]
