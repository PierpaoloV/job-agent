"""Production-shaped composition for the local application workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from application_artifacts import TruthfulApplicationArtifactService
from application_packages import LocalApplicationPackageWriter
from application_storage import JsonApplicationStore
from application_workflow import ApplicationWorkflowCoordinator
from hosted_artifact_github import GitHubHostedArtifactClient
from hosted_artifact_handoff import (
    ArtifactHandoffAuthority,
    ArtifactHandoffKey,
    LocalArtifactHandoff,
)
from hosted_tailoring import HostedTailoringAdapter, HostedTailoringStateStore
from pdf_artifact_renderer import LocalPdfArtifactRenderer
from structured_artifact_generator import (
    AnthropicArtifactProvider,
    DeterministicClaimAuditor,
    StructuredArtifactGenerator,
)
from yaml_evidence_source import YamlEvidenceSource


class HostedPreparationVacancyAdapter:
    """Read only the exact verified vacancy captured by hosted grading."""

    def __init__(self, inputs) -> None:
        self._inputs = inputs

    def retrieve(self, opportunity: Mapping[str, Any]):
        application_id, version = self._identity(opportunity)
        return self._inputs.load(application_id, version).official_vacancy

    def revalidate(self, opportunity: Mapping[str, Any], previous):
        del previous
        return self.retrieve(opportunity)

    @staticmethod
    def _identity(opportunity: Mapping[str, Any]) -> tuple[str, str]:
        application_id = str(opportunity.get("application_id", "")).strip()
        version = str(
            opportunity.get("official_vacancy_version", "")
        ).strip()
        if not application_id or not version:
            raise ValueError("Hosted vacancy identity is incomplete")
        return application_id, version


class UnsupportedAtsAdapter:
    """Keep ATS effects disabled until a verified browser adapter is bound."""

    def fill(self, application_id, intent_id, artifacts):
        raise RuntimeError("No supported ATS adapter is bound")

    def submit(self, application_id, manifest):
        raise RuntimeError("No supported ATS adapter is bound")

    def validate_submit(self, application_id, manifest) -> bool:
        return False

    def intervention_is_resolved(self, application_id, intervention) -> bool:
        return False

    def inspect_submission(self, application_id, manifest):
        raise RuntimeError("No supported ATS adapter is bound")


_HOSTED_APPLICATION_CONFIG_FIELDS = frozenset(
    {
        "repository",
        "branch",
        "workflow",
        "github_token_keychain_service",
        "github_token_keychain_account",
        "handoff_key_keychain_service",
        "handoff_key_keychain_account",
    }
)


@dataclass(frozen=True)
class HostedApplicationConfig:
    """Non-secret coordinates for the encrypted Actions-to-Mac handoff."""

    repository: str
    branch: str
    workflow: str
    github_token_keychain_service: str
    github_token_keychain_account: str
    handoff_key_keychain_service: str
    handoff_key_keychain_account: str

    def __post_init__(self) -> None:
        values = (
            self.repository,
            self.branch,
            self.workflow,
            self.github_token_keychain_service,
            self.github_token_keychain_account,
            self.handoff_key_keychain_service,
            self.handoff_key_keychain_account,
        )
        if any(not str(value).strip() for value in values):
            raise ValueError("Hosted application configuration is incomplete")
        if any(str(value) != str(value).strip() for value in values):
            raise ValueError("Hosted application configuration must be canonical")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HostedApplicationConfig":
        if set(value) != _HOSTED_APPLICATION_CONFIG_FIELDS:
            raise ValueError("Hosted application configuration schema is invalid")
        return cls(**{name: str(value[name]) for name in sorted(value)})


def build_application_artifact_service(
    *,
    repository_root: Path,
    evidence_path: Path,
    canonical_cv_path: Path,
    candidate_name: str,
    provider=None,
) -> TruthfulApplicationArtifactService:
    """Wire the one-call tailoring path to owner-local, immutable sources."""

    name = str(candidate_name).strip()
    if not name:
        raise ValueError("Candidate name is required for application artifacts")
    root = Path(repository_root)
    generation_provider = provider or AnthropicArtifactProvider()
    return TruthfulApplicationArtifactService(
        evidence_source=YamlEvidenceSource(
            Path(evidence_path), Path(canonical_cv_path)
        ),
        generator=StructuredArtifactGenerator(
            generation_provider,
            candidate_name=name,
        ),
        claim_auditor=DeterministicClaimAuditor(structural_lines=(name,)),
        renderer=LocalPdfArtifactRenderer(
            root / "data" / "private" / "application-artifacts"
        ),
    )


def build_hosted_tailoring_adapter(
    *,
    repository_root: Path,
    config: HostedApplicationConfig,
    github_token: str,
    handoff_key: str,
    source_version_loader: Callable[[], str],
    hosted_client=None,
) -> HostedTailoringAdapter:
    """Compose the authenticated hosted generation path from injected secrets."""

    token = str(github_token).strip()
    if not token:
        raise ValueError("GitHub hosted artifact token is required")
    if not callable(source_version_loader):
        raise TypeError("Hosted source version loader is required")
    root = Path(repository_root)
    private = root / "data" / "private"
    authority = ArtifactHandoffAuthority(
        repository=config.repository,
        workflow=config.workflow,
        branch=config.branch,
    )
    key = ArtifactHandoffKey.from_base64(handoff_key)
    return HostedTailoringAdapter(
        client=(
            GitHubHostedArtifactClient(
                repository=config.repository,
                token=token,
                branch=config.branch,
                workflow=config.workflow,
            )
            if hosted_client is None
            else hosted_client
        ),
        handoff=LocalArtifactHandoff(
            key=key,
            expected_authority=authority,
            root=private / "application-artifacts",
        ),
        transfer_root=private / "application-transfers",
        state_store=HostedTailoringStateStore(
            private / "hosted-tailoring-state"
        ),
        source_version_loader=source_version_loader,
    )


def build_application_workflow_coordinator(
    *,
    repository_root: Path,
    tailoring,
    ats,
    official_vacancies,
    clock,
    token_factory=None,
) -> ApplicationWorkflowCoordinator:
    """Wire durable local storage while keeping sensitive capabilities injected."""

    root = Path(repository_root)
    if not callable(getattr(ats, "validate_submit", None)):
        raise TypeError("ATS adapter must provide fail-closed validate_submit")
    return ApplicationWorkflowCoordinator(
        store=JsonApplicationStore(
            root / "data" / "private" / "application-state"
        ),
        tailoring=tailoring,
        ats=ats,
        report_writer=LocalApplicationPackageWriter.for_repository(root),
        official_vacancies=official_vacancies,
        clock=clock,
        token_factory=token_factory,
    )


def build_hosted_application_workflow_coordinator(
    *,
    repository_root: Path,
    config: HostedApplicationConfig,
    github_token: str,
    handoff_key: str,
    source_version_loader: Callable[[], str],
    ats,
    official_vacancies,
    clock,
    token_factory=None,
    hosted_client=None,
) -> ApplicationWorkflowCoordinator:
    """Bind hosted tailoring only when the local ATS capabilities are explicit."""

    tailoring = build_hosted_tailoring_adapter(
        repository_root=repository_root,
        config=config,
        github_token=github_token,
        handoff_key=handoff_key,
        source_version_loader=source_version_loader,
        hosted_client=hosted_client,
    )
    return build_application_workflow_coordinator(
        repository_root=repository_root,
        tailoring=tailoring,
        ats=ats,
        official_vacancies=official_vacancies,
        clock=clock,
        token_factory=token_factory,
    )


__all__ = [
    "HostedPreparationVacancyAdapter",
    "HostedApplicationConfig",
    "UnsupportedAtsAdapter",
    "build_application_artifact_service",
    "build_hosted_application_workflow_coordinator",
    "build_hosted_tailoring_adapter",
    "build_application_workflow_coordinator",
]
