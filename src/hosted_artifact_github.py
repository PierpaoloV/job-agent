"""GitHub transport for encrypted hosted application artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping
import zipfile

import requests

from hosted_artifact_handoff import (
    ArtifactHandoffIdentity,
    encrypted_package_has_supported_header,
)


@dataclass(frozen=True)
class HostedWorkflowRun:
    """Minimal authenticated Actions state used by local reconciliation."""

    run_id: int
    status: str
    conclusion: str | None

    def __post_init__(self) -> None:
        if not _positive_integer(self.run_id):
            raise ValueError("GitHub workflow run id is invalid")
        if self.status not in {
            "requested",
            "pending",
            "queued",
            "in_progress",
            "completed",
            "waiting",
        }:
            raise ValueError("GitHub workflow run status is invalid")
        if self.status != "completed" and self.conclusion is not None:
            raise ValueError("Incomplete GitHub workflow run has a conclusion")


class HostedDispatchRejected(RuntimeError):
    """GitHub definitively rejected repository_dispatch before accepting a run."""


class HostedDispatchAmbiguous(RuntimeError):
    """GitHub may have accepted repository_dispatch; reconciliation is required."""


def hosted_workflow_run_name(identity: ArtifactHandoffIdentity) -> str:
    """Return the exact authenticated Actions title for one preparation identity."""

    return (
        f"prepare-application|{identity.application_id}|"
        f"{identity.official_vacancy_version}"
    )


class GitHubHostedArtifactClient:
    """Dispatch hosted generation and retrieve its encrypted result."""

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        branch: str,
        workflow: str = "run.yml",
    ) -> None:
        self._repository = str(repository).strip()
        self._branch = str(branch).strip()
        self._workflow = str(workflow).strip()
        if (
            not self._repository
            or not self._branch
            or not self._workflow
            or not str(token).strip()
        ):
            raise ValueError(
                "GitHub hosted artifact client configuration is incomplete"
            )
        self._headers = {
            "Authorization": f"Bearer {str(token).strip()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def transport_scope(self) -> Mapping[str, str]:
        return {
            "workflow": self._workflow,
            "branch": self._branch,
            "event": "repository_dispatch",
        }

    def dispatch(self, identity: ArtifactHandoffIdentity) -> None:
        try:
            response = requests.post(
                f"https://api.github.com/repos/{self._repository}/dispatches",
                headers=self._headers,
                json={
                    "event_type": "prepare-application",
                    "client_payload": identity.to_dict(),
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise HostedDispatchAmbiguous(
                "GitHub hosted artifact dispatch outcome is ambiguous"
            ) from error
        status_code = getattr(response, "status_code", None)
        if (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 100 <= status_code <= 599
        ):
            raise HostedDispatchAmbiguous(
                "GitHub hosted artifact dispatch outcome is ambiguous"
            )
        if 200 <= status_code < 300:
            return
        if 400 <= status_code < 500:
            raise HostedDispatchRejected(
                "GitHub hosted artifact dispatch was rejected safely"
            )
        raise HostedDispatchAmbiguous(
            "GitHub hosted artifact dispatch outcome is ambiguous"
        )

    def workflow_runs(
        self,
        identity: ArtifactHandoffIdentity,
        *,
        exclude_run_ids: frozenset[int] = frozenset(),
    ) -> tuple[HostedWorkflowRun, ...]:
        """Return workflow-scoped runs, excluding the durable pre-dispatch baseline."""

        return tuple(
            run
            for run in self._available_workflow_runs(identity)
            if run.run_id not in exclude_run_ids
        )

    def workflow_run_ids(
        self, identity: ArtifactHandoffIdentity
    ) -> frozenset[int]:
        return frozenset(run.run_id for run in self.workflow_runs(identity))

    def package_for_run(
        self,
        identity: ArtifactHandoffIdentity,
        *,
        workflow_run_id: int,
    ) -> bytes | None:
        """Download only the identity artifact produced by the bound workflow run."""

        if not _positive_integer(workflow_run_id):
            raise ValueError("GitHub workflow run id is invalid")
        artifacts = [
            item
            for item in self._available_artifacts(identity)
            if int(item["workflow_run"]["id"]) == workflow_run_id
        ]
        if not artifacts:
            return None
        if len(artifacts) != 1:
            raise ValueError("GitHub workflow run has ambiguous hosted artifacts")
        artifact = artifacts[0]
        archive_url = str(artifact.get("archive_download_url", ""))
        expected_url = (
            f"https://api.github.com/repos/{self._repository}/actions/artifacts/"
            f"{_artifact_id(artifact)}/zip"
        )
        if archive_url != expected_url:
            raise ValueError("GitHub artifact download authority is invalid")
        download = requests.get(
            archive_url,
            headers=self._headers,
            timeout=60,
        )
        if not download.ok:
            raise RuntimeError("GitHub hosted artifact download failed safely")
        return _encrypted_file_from_actions_archive(download.content)

    def _available_workflow_runs(
        self,
        identity: ArtifactHandoffIdentity,
    ) -> list[HostedWorkflowRun]:
        runs: list[object] = []
        page = 1
        expected_total: int | None = None
        while True:
            response = requests.get(
                f"https://api.github.com/repos/{self._repository}/actions/"
                f"workflows/{self._workflow}/runs",
                headers=self._headers,
                params={
                    "branch": self._branch,
                    "event": "repository_dispatch",
                    "per_page": 100,
                    "page": page,
                },
                timeout=30,
            )
            if not response.ok:
                raise RuntimeError("GitHub workflow run listing failed safely")
            try:
                payload = response.json()
            except (TypeError, ValueError):
                raise ValueError("GitHub workflow run listing is invalid") from None
            if not isinstance(payload, Mapping) or not isinstance(
                payload.get("workflow_runs"), list
            ):
                raise ValueError("GitHub workflow run listing is invalid")
            total = payload.get("total_count")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("GitHub workflow run listing count is invalid")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise ValueError(
                    "GitHub workflow run listing changed during pagination"
                )
            page_items = payload["workflow_runs"]
            runs.extend(page_items)
            if len(runs) >= expected_total:
                if len(runs) != expected_total:
                    raise ValueError(
                        "GitHub workflow run listing count is inconsistent"
                    )
                break
            if not page_items:
                raise ValueError("GitHub workflow run listing is incomplete")
            page += 1
            if page > 1000:
                raise ValueError("GitHub workflow run listing is unreasonably large")
        available = []
        expected_title = hosted_workflow_run_name(identity)
        for item in runs:
            if (
                not isinstance(item, Mapping)
                or not _positive_integer(item.get("id"))
                or item.get("event") != "repository_dispatch"
                or item.get("head_branch") != self._branch
                or item.get("display_title") != expected_title
            ):
                continue
            status = str(item.get("status", ""))
            conclusion_value = item.get("conclusion")
            conclusion = (
                None if conclusion_value is None else str(conclusion_value)
            )
            available.append(
                HostedWorkflowRun(
                    run_id=int(item["id"]),
                    status=status,
                    conclusion=conclusion,
                )
            )
        return available

    def _available_artifacts(
        self, identity: ArtifactHandoffIdentity
    ) -> list[Mapping[str, Any]]:
        artifacts: list[object] = []
        page = 1
        expected_total: int | None = None
        while True:
            response = requests.get(
                f"https://api.github.com/repos/{self._repository}/actions/artifacts",
                headers=self._headers,
                params={
                    "name": identity.artifact_name,
                    "per_page": 100,
                    "page": page,
                },
                timeout=30,
            )
            if not response.ok:
                raise RuntimeError("GitHub hosted artifact listing failed safely")
            try:
                payload = response.json()
            except (TypeError, ValueError):
                raise ValueError("GitHub artifact listing is invalid") from None
            if not isinstance(payload, Mapping) or not isinstance(
                payload.get("artifacts"), list
            ):
                raise ValueError("GitHub artifact listing is invalid")
            total = payload.get("total_count")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("GitHub artifact listing count is invalid")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise ValueError("GitHub artifact listing changed during pagination")
            page_items = payload["artifacts"]
            artifacts.extend(page_items)
            if len(artifacts) >= expected_total:
                if len(artifacts) != expected_total:
                    raise ValueError("GitHub artifact listing count is inconsistent")
                break
            if not page_items:
                raise ValueError("GitHub artifact listing is incomplete")
            page += 1
            if page > 1000:
                raise ValueError("GitHub artifact listing is unreasonably large")
        return [
            item
            for item in artifacts
            if isinstance(item, Mapping)
            and item.get("name") == identity.artifact_name
            and not item.get("expired", False)
            and isinstance(item.get("workflow_run"), Mapping)
            and str(item["workflow_run"].get("head_branch", "")) == self._branch
            and _positive_integer(item["workflow_run"].get("id"))
            and _artifact_id(item) > 0
        ]

def _artifact_id(value: Mapping[str, Any]) -> int:
    candidate = value.get("id")
    if not _positive_integer(candidate):
        return 0
    return candidate


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _encrypted_file_from_actions_archive(value: bytes) -> bytes:
    try:
        with zipfile.ZipFile(BytesIO(value)) as archive:
            files = archive.infolist()
            if (
                len(files) != 1
                or files[0].is_dir()
                or files[0].filename != "application-artifacts.enc"
            ):
                raise ValueError
            encrypted = archive.read(files[0])
    except (ValueError, zipfile.BadZipFile, KeyError):
        raise ValueError("GitHub artifact archive is invalid") from None
    if not encrypted_package_has_supported_header(encrypted):
        raise ValueError("GitHub artifact does not contain an encrypted package")
    return encrypted


__all__ = [
    "GitHubHostedArtifactClient",
    "HostedDispatchAmbiguous",
    "HostedDispatchRejected",
    "HostedWorkflowRun",
    "hosted_workflow_run_name",
]
