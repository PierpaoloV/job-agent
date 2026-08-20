"""Hosted preparation from authoritative grading inputs to encrypted handoff."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from application_composition import build_application_artifact_service
from application_domain import ArtifactFamily, OfficialVacancy, PreparedArtifacts
from application_identity import approved_application_id
from hosted_artifact_handoff import (
    ArtifactHandoffAuthority,
    ArtifactHandoffIdentity,
    ArtifactHandoffKey,
    EncryptedArtifactPackage,
    HostedArtifactHandoff,
)
from requirements_evidence import RequirementsEvidenceMatrix
from structured_artifact_generator import AnthropicArtifactProvider
from vacancy_policy import VerificationState, verification_state


HOSTED_PREPARATION_INPUT_VERSION = "job-agent.hosted-preparation-input.v1"
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_CANONICAL_APPLICATION_ID = re.compile(r"approved-[0-9a-f]{16}")
_SAFE_OPPORTUNITY_FIELDS = frozenset(
    {"artifact_family", "requirements_evidence_matrix"}
)


@dataclass(frozen=True)
class HostedPreparationInput:
    stable_id: str
    official_vacancy: OfficialVacancy
    opportunity: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.stable_id.strip() or self.stable_id != self.stable_id.strip():
            raise ValueError("Hosted preparation stable id must be canonical")
        if not _CANONICAL_SHA256.fullmatch(self.official_vacancy.version):
            raise ValueError(
                "Hosted preparation vacancy version must be canonical sha256"
            )
        if self.official_vacancy.fingerprint != self.official_vacancy.version:
            raise ValueError("Hosted preparation vacancy fingerprint differs")
        if (
            not self.official_vacancy.available
            or not self.official_vacancy.verified
            or not self.official_vacancy.description.strip()
            or self.official_vacancy.description
            != self.official_vacancy.description.strip()
        ):
            raise ValueError("Hosted preparation requires a verified vacancy")
        try:
            freshness = datetime.fromisoformat(self.official_vacancy.freshness)
        except ValueError as exc:
            raise ValueError(
                "Hosted preparation freshness must be ISO 8601"
            ) from exc
        if freshness.tzinfo is None or freshness.utcoffset() is None:
            raise ValueError(
                "Hosted preparation freshness must be timezone-aware"
            )
        if set(self.opportunity) != _SAFE_OPPORTUNITY_FIELDS:
            raise ValueError(
                "Hosted preparation opportunity must use candidate-safe fields"
            )
        matrix = self.opportunity.get("requirements_evidence_matrix")
        if not isinstance(matrix, Mapping):
            raise ValueError("Hosted preparation requires the persisted matrix")
        parsed = RequirementsEvidenceMatrix.from_dict(matrix)
        if parsed.official_vacancy_version != self.official_vacancy.version:
            raise ValueError("Hosted preparation matrix does not match vacancy")
        try:
            family = ArtifactFamily(str(self.opportunity["artifact_family"]))
        except ValueError as exc:
            raise ValueError(
                "Hosted preparation requires a supported artifact family"
            ) from exc
        object.__setattr__(
            self,
            "opportunity",
            {
                "artifact_family": family.value,
                "requirements_evidence_matrix": parsed.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": HOSTED_PREPARATION_INPUT_VERSION,
            "stable_id": self.stable_id,
            "official_vacancy": asdict(self.official_vacancy),
            "opportunity": dict(self.opportunity),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HostedPreparationInput":
        if set(value) != {
            "version",
            "stable_id",
            "official_vacancy",
            "opportunity",
        }:
            raise ValueError("Hosted preparation input must be canonical")
        if value.get("version") != HOSTED_PREPARATION_INPUT_VERSION:
            raise ValueError("Unsupported hosted preparation input version")
        vacancy = value.get("official_vacancy")
        opportunity = value.get("opportunity")
        if not isinstance(vacancy, Mapping) or not isinstance(opportunity, Mapping):
            raise ValueError("Hosted preparation input is incomplete")
        return cls(
            stable_id=str(value.get("stable_id", "")),
            official_vacancy=OfficialVacancy.from_dict(vacancy),
            opportunity=dict(opportunity),
        )


class HostedPreparationInputStore:
    """Persist candidate-safe inputs by full opportunity/vacancy identity."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save(self, value: HostedPreparationInput) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(value.stable_id, value.official_vacancy.version)
        encoded = (
            json.dumps(value.to_dict(), indent=2, sort_keys=True) + "\n"
        )
        if path.exists():
            if path.read_text(encoding="utf-8") == encoded:
                return
            raise RuntimeError("Hosted preparation input version already differs")
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def capture_graded(
        self, graded_jobs: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]
    ) -> tuple[str, ...]:
        captured = []
        for job in graded_jobs:
            if (
                verification_state(job.get("verification_status"))
                != VerificationState.VERIFIED
            ):
                continue
            stable_id = str(job.get("stable_id") or job.get("dedup_key") or "").strip()
            description = str(job.get("official_description", "")).strip()
            vacancy_version = str(
                job.get("official_vacancy_version", "")
            ).strip()
            evaluation = job.get("portfolio_evaluation")
            matrix = job.get("requirements_evidence_matrix")
            if (
                not stable_id
                or not description
                or not isinstance(evaluation, Mapping)
                or not isinstance(matrix, Mapping)
            ):
                raise ValueError("Graded job lacks hosted preparation inputs")
            if not _CANONICAL_SHA256.fullmatch(vacancy_version):
                raise ValueError(
                    "Graded job vacancy version must be canonical sha256"
                )
            evaluation_opportunity = str(
                evaluation.get("opportunity_id", "")
            ).strip()
            if evaluation_opportunity and evaluation_opportunity != stable_id:
                raise ValueError(
                    "Graded job opportunity does not match portfolio evaluation"
                )
            evaluation_matrix = evaluation.get(
                "requirements_evidence_matrix"
            )
            if (
                evaluation_matrix is not None
                and (
                    not isinstance(evaluation_matrix, Mapping)
                    or dict(evaluation_matrix) != dict(matrix)
                )
            ):
                raise ValueError(
                    "Graded job matrix does not match portfolio evaluation"
                )
            freshness = str(evaluation.get("vacancy_retrieved_at", "")).strip()
            if not freshness:
                raise ValueError("Graded job lacks verified vacancy freshness")
            job_freshness = str(job.get("retrieved_at", "")).strip()
            if job_freshness and job_freshness != freshness:
                raise ValueError(
                    "Graded job freshness does not match portfolio evaluation"
                )
            self.save(
                HostedPreparationInput(
                    stable_id=stable_id,
                    official_vacancy=OfficialVacancy(
                        version=vacancy_version,
                        fingerprint=vacancy_version,
                        freshness=freshness,
                        description=description,
                    ),
                    opportunity={
                        "artifact_family": _artifact_family(job),
                        "requirements_evidence_matrix": dict(matrix),
                    },
                )
            )
            captured.append(vacancy_version)
        return tuple(captured)

    def load(
        self,
        application_id: str,
        official_vacancy_version: str | None = None,
    ) -> HostedPreparationInput:
        if official_vacancy_version is None:
            return self._load_unique_vacancy(application_id)
        if not _CANONICAL_APPLICATION_ID.fullmatch(str(application_id)):
            raise ValueError(
                "Hosted preparation application id must be canonical"
            )
        self._validate_vacancy_version(official_vacancy_version)
        candidates = tuple(
            sorted(self._root.glob(f"{application_id}-*.json"))
        )
        matches = []
        for path in candidates:
            parsed = self._read(path)
            expected_application_id = approved_application_id(
                parsed.stable_id,
                parsed.official_vacancy.version,
            )
            if expected_application_id != application_id:
                raise ValueError(
                    "Hosted preparation input storage identity mismatch"
                )
            if parsed.official_vacancy.version == official_vacancy_version:
                matches.append(parsed)
        if not matches:
            raise KeyError((application_id, official_vacancy_version))
        if len(matches) != 1:
            raise RuntimeError("Hosted preparation application identity is ambiguous")
        return matches[0]

    def _load_unique_vacancy(
        self, official_vacancy_version: str
    ) -> HostedPreparationInput:
        self._validate_vacancy_version(official_vacancy_version)
        matches = [
            parsed
            for path in sorted(self._root.glob("*.json"))
            if (parsed := self._read(path)).official_vacancy.version
            == official_vacancy_version
        ]
        if not matches:
            raise KeyError(official_vacancy_version)
        if len(matches) != 1:
            raise RuntimeError(
                "Hosted preparation vacancy identity is ambiguous; "
                "application id is required"
            )
        return matches[0]

    def _read(self, path: Path) -> HostedPreparationInput:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Hosted preparation input must be an object")
        parsed = HostedPreparationInput.from_dict(value)
        if value != parsed.to_dict():
            raise ValueError("Hosted preparation input must be canonical")
        if path != self._path(
            parsed.stable_id, parsed.official_vacancy.version
        ):
            raise ValueError("Hosted preparation input storage identity mismatch")
        return parsed

    def _path(self, stable_id: str, official_vacancy_version: str) -> Path:
        return self._root / hosted_preparation_input_filename(
            stable_id, official_vacancy_version
        )

    @staticmethod
    def _validate_vacancy_version(official_vacancy_version: str) -> None:
        if not _CANONICAL_SHA256.fullmatch(str(official_vacancy_version)):
            raise ValueError(
                "Hosted preparation vacancy version must be canonical sha256"
            )


def hosted_preparation_input_filename(
    stable_id: str, official_vacancy_version: str
) -> str:
    application_id = approved_application_id(stable_id, official_vacancy_version)
    encoded = json.dumps(
        {
            "stable_id": stable_id,
            "official_vacancy_version": official_vacancy_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity_digest = hashlib.sha256(encoded).hexdigest()
    return f"{application_id}-{identity_digest}.json"


class HostedArtifactPreparationService:
    """Generate one hosted bundle from an exact authoritative input snapshot."""

    def __init__(
        self,
        *,
        repository_root: Path,
        inputs: HostedPreparationInputStore,
        evidence_path: Path,
        canonical_cv_path: Path,
        candidate_name: str,
        provider,
        key: ArtifactHandoffKey,
        authority: ArtifactHandoffAuthority | Mapping[str, str],
    ) -> None:
        self._inputs = inputs
        self._artifact_service = build_application_artifact_service(
            repository_root=Path(repository_root),
            evidence_path=Path(evidence_path),
            canonical_cv_path=Path(canonical_cv_path),
            candidate_name=candidate_name,
            provider=provider,
        )
        self._handoff = HostedArtifactHandoff(key=key, authority=authority)

    def prepare(
        self,
        *,
        identity: ArtifactHandoffIdentity,
        destination: Path,
    ) -> "HostedPreparationResult":
        try:
            value = self._inputs.load(
                identity.application_id,
                identity.official_vacancy_version,
            )
        except KeyError as exc:
            raise ValueError(
                "Hosted preparation application identity does not match snapshot"
            ) from exc
        expected_application_id = approved_application_id(
            value.stable_id,
            value.official_vacancy.version,
        )
        if identity.application_id != expected_application_id:
            raise ValueError(
                "Hosted preparation application identity does not match snapshot"
            )
        artifacts = self._artifact_service.prepare(
            identity.application_id,
            _intent_id(identity),
            value.opportunity,
            value.official_vacancy,
        )
        package = self._handoff.export(
            identity=identity,
            artifacts=artifacts,
            destination=Path(destination),
        )
        return HostedPreparationResult(package=package, artifacts=artifacts)


@dataclass(frozen=True)
class HostedPreparationResult:
    """Encrypted handoff plus the short-lived plaintext review artifacts."""

    package: EncryptedArtifactPackage
    artifacts: PreparedArtifacts

    @property
    def path(self) -> Path:
        return self.package.path

    @property
    def package_hash(self) -> str:
        return self.package.package_hash


def _intent_id(identity: ArtifactHandoffIdentity) -> str:
    encoded = json.dumps(
        identity.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "hosted:" + hashlib.sha256(encoded).hexdigest()


def _artifact_family(job: Mapping[str, Any]) -> str:
    role = " ".join(
        str(job.get(field, ""))
        for field in ("title", "role")
    ).casefold()
    if any(
        marker in role
        for marker in (
            "agentic",
            "large language model",
            "llm",
            "generative ai",
            "nlp",
        )
    ):
        return "agentic_ai"
    if any(
        marker in role
        for marker in (
            "research",
            "scientist",
            "applied science",
        )
    ):
        return "research"
    return "cv_applied_ml"


def _github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--root", type=Path, default=Path("."))
    prepare.add_argument(
        "--inputs",
        type=Path,
        default=Path("data/hosted-preparation-inputs"),
    )
    prepare.add_argument("--evidence", type=Path, required=True)
    prepare.add_argument("--canonical-cv", type=Path, required=True)
    prepare.add_argument("--candidate-name", required=True)
    prepare.add_argument("--application-id", required=True)
    prepare.add_argument("--official-vacancy-version", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--workflow", default="run.yml")
    prepare.add_argument("--branch", required=True)
    args = parser.parse_args(argv)

    key_value = os.environ.get("JOB_AGENT_ARTIFACT_HANDOFF_KEY", "")
    if not key_value:
        raise RuntimeError("JOB_AGENT_ARTIFACT_HANDOFF_KEY is required")
    identity = ArtifactHandoffIdentity(
        application_id=args.application_id,
        official_vacancy_version=args.official_vacancy_version,
    )
    package = HostedArtifactPreparationService(
        repository_root=args.root,
        inputs=HostedPreparationInputStore(args.inputs),
        evidence_path=args.evidence,
        canonical_cv_path=args.canonical_cv,
        candidate_name=args.candidate_name,
        provider=AnthropicArtifactProvider(),
        key=ArtifactHandoffKey.from_base64(key_value),
        authority=ArtifactHandoffAuthority(
            repository=args.repository,
            workflow=args.workflow,
            branch=args.branch,
        ),
    ).prepare(identity=identity, destination=args.output)
    _github_output("artifact_name", identity.artifact_name)
    _github_output("package_hash", package.package_hash)
    _github_output("artifact_version", package.artifacts.version)
    _github_output("cv_path", package.artifacts.cv_path)
    _github_output("cover_letter_path", package.artifacts.cover_letter_path)
    _github_output("cv_hash", package.artifacts.cv_hash)
    _github_output("cover_letter_hash", package.artifacts.cover_letter_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOSTED_PREPARATION_INPUT_VERSION",
    "HostedArtifactPreparationService",
    "HostedPreparationInput",
    "HostedPreparationInputStore",
    "HostedPreparationResult",
    "hosted_preparation_input_filename",
    "main",
]
