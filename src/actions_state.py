"""Versioned, immutable GitHub Actions state bundles for cross-run recovery."""

from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Mapping, Sequence
import zipfile

import requests

from deep_grading_contract import DeepGradeResult, GradingContractError
from hosted_artifact_preparation import (
    HostedPreparationInput,
    hosted_preparation_input_filename,
)
from hosted_preparation_completion import HostedApplicationState
from workflow import ShortlistArtifact


STATE_BUNDLE_VERSION = "job-agent.actions-state.v1"
STATE_ARTIFACT_PREFIX = "discovery-state-"
_FIXED_STATE_FILES = (
    Path("data/seen.sqlite"),
    Path("data/discovery-schedule.json"),
    Path("data/telegram-deliveries.sqlite"),
    Path("data/pending-shortlist.json"),
    Path("data/opportunity-decisions.json"),
)


class StateBundle:
    """Stage and validate the complete authoritative cross-run state."""

    def __init__(
        self, root: Path, *, expected_authority: Mapping[str, str] | None = None
    ) -> None:
        self.root = Path(root)
        self._expected_authority = dict(expected_authority or {})
        self.package_dir = self.root / "data" / "actions-state"
        self.manifest_path = self.package_dir / "manifest.json"
        self.head_path = self.root / "data" / "actions-state-head.json"

    def write_manifest(
        self, authority: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self._clear_package()
        files: dict[str, str] = {}
        for source in self._state_files():
            relative = source.relative_to(self.root)
            destination = self.package_dir / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files[relative.as_posix()] = _file_hash(destination)
        head = self._read_head()
        lineage = list(head.get("lineage", [])) if head else []
        parent = None if head is None else str(head["manifest_digest"])
        if parent:
            lineage.append(parent)
        manifest = {
            "version": STATE_BUNDLE_VERSION,
            "authority": dict(authority or _environment_authority()),
            "parent_manifest": parent,
            "lineage": lineage,
            "files": files,
        }
        manifest["manifest_digest"] = _manifest_digest(manifest)
        self.package_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.validate_manifest()
        return manifest

    def validate_manifest(self, package_dir: Path | None = None) -> dict[str, Any]:
        package = Path(package_dir or self.package_dir)
        manifest_path = package / "manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != STATE_BUNDLE_VERSION:
            actual = value.get("version") if isinstance(value, dict) else None
            raise ValueError(f"Unsupported Actions state version: {actual}")
        if value.get("manifest_digest") != _manifest_digest(value):
            raise ValueError("Actions state manifest digest mismatch")
        authority = value.get("authority")
        if not isinstance(authority, Mapping):
            raise ValueError("Actions state authority must be an object")
        for key, expected in self._expected_authority.items():
            if str(authority.get(key, "")) != expected:
                raise ValueError(f"Actions state authority mismatch: {key}")
        files = value.get("files")
        if not isinstance(files, dict):
            raise ValueError("Actions state manifest files must be an object")
        for raw_path, expected_hash in files.items():
            relative = _safe_relative_path(str(raw_path))
            if not _allowed_state_path(relative):
                raise ValueError(f"Unsupported Actions state path: {relative}")
            staged = package / "files" / relative
            if not staged.is_file() or _file_hash(staged) != str(expected_hash):
                raise ValueError(f"Actions state hash mismatch: {relative}")
            _validate_embedded_version(relative, staged)
        return value

    def install_package(self, package_dir: Path) -> None:
        value = self.validate_manifest(package_dir)
        head = self._read_head()
        digest = str(value["manifest_digest"])
        if head and digest != head.get("manifest_digest"):
            if _generation(value) <= tuple(head["generation"]):
                raise ValueError("Actions state generation is not monotonic")
            if str(head["manifest_digest"]) not in value.get("lineage", []):
                raise ValueError("Actions state parent lineage is unrelated")
        authoritative = {
            _safe_relative_path(str(raw_path)) for raw_path in value["files"]
        }
        for existing in self._state_files():
            relative = existing.relative_to(self.root)
            if relative not in authoritative:
                existing.unlink()
        for raw_path in value["files"]:
            relative = _safe_relative_path(str(raw_path))
            source = Path(package_dir) / "files" / relative
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".restore")
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        authority = value["authority"]
        self.head_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_head = self.head_path.with_suffix(".tmp")
        temporary_head.write_text(
            json.dumps(
                {
                    "manifest_digest": digest,
                    "generation": list(_generation(value)),
                    "lineage": list(value.get("lineage", [])),
                    "authority": dict(authority),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_head.replace(self.head_path)

    def _state_files(self) -> tuple[Path, ...]:
        files = [self.root / relative for relative in _FIXED_STATE_FILES]
        grades = self.root / "data" / "deep-grades"
        if grades.exists():
            files.extend(
                sorted(path for path in grades.rglob("*.json") if path.is_file())
            )
        preparation_inputs = self.root / "data" / "hosted-preparation-inputs"
        if preparation_inputs.exists():
            files.extend(
                sorted(
                    path
                    for path in preparation_inputs.rglob("*.json")
                    if path.is_file()
                )
            )
        application_states = self.root / "data" / "hosted-application-state"
        if application_states.exists():
            files.extend(
                sorted(
                    path
                    for path in application_states.rglob("*.json")
                    if path.is_file()
                )
            )
        return tuple(path for path in files if path.is_file())

    def _clear_package(self) -> None:
        if self.package_dir.exists():
            shutil.rmtree(self.package_dir)

    def _read_head(self) -> dict[str, Any] | None:
        if not self.head_path.exists():
            return None
        value = json.loads(self.head_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Actions state head must be an object")
        return value


class GitHubActionsStateClient:
    def __init__(self, *, repository: str, token: str, branch: str) -> None:
        self._repository = repository
        self._branch = branch
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def latest_archive(self) -> bytes | None:
        response = requests.get(
            f"https://api.github.com/repos/{self._repository}/actions/artifacts",
            headers=self._headers,
            params={"per_page": 100},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError("GitHub Actions state listing failed safely")
        payload = response.json()
        artifacts = [
            item
            for item in payload.get("artifacts", [])
            if isinstance(item, Mapping)
            and str(item.get("name", "")).startswith(STATE_ARTIFACT_PREFIX)
            and not item.get("expired", False)
            and isinstance(item.get("workflow_run"), Mapping)
            and str(item["workflow_run"].get("head_branch", "")) == self._branch
        ]
        if not artifacts:
            return None
        latest = max(
            artifacts,
            key=lambda item: (
                int(item["workflow_run"].get("id", 0)),
                str(item.get("created_at", "")),
                "-deep-" in str(item.get("name", "")),
            ),
        )
        download = requests.get(
            str(latest["archive_download_url"]), headers=self._headers, timeout=60
        )
        if not download.ok:
            raise RuntimeError("GitHub Actions state download failed safely")
        return download.content


def restore_latest(
    *,
    root: Path,
    repository: str,
    token: str,
    workflow: str = "run.yml",
    branch: str = "main",
) -> bool:
    archive = GitHubActionsStateClient(
        repository=repository, token=token, branch=branch
    ).latest_archive()
    if archive is None:
        return False
    with tempfile.TemporaryDirectory(prefix="job-agent-state-") as directory:
        extracted = Path(directory)
        _extract_safe(archive, extracted)
        manifests = list(extracted.rglob("manifest.json"))
        if len(manifests) != 1:
            raise ValueError("Actions state archive must contain one manifest")
        StateBundle(
            root,
            expected_authority={
                "repository": repository,
                "workflow": workflow,
                "branch": branch,
            },
        ).install_package(manifests[0].parent)
    return True


def _extract_safe(archive: bytes, destination: Path) -> None:
    with zipfile.ZipFile(BytesIO(archive)) as source:
        for item in source.infolist():
            relative = _safe_relative_path(item.filename)
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as input_file, target.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe path in Actions state archive")
    return path


def _allowed_state_path(path: Path) -> bool:
    return path in _FIXED_STATE_FILES or (
        len(path.parts) == 3
        and path.parts[:2] == ("data", "deep-grades")
        and path.suffix == ".json"
    ) or (
        len(path.parts) == 3
        and path.parts[:2] == ("data", "hosted-preparation-inputs")
        and path.suffix == ".json"
    ) or (
        len(path.parts) == 3
        and path.parts[:2] == ("data", "hosted-application-state")
        and path.suffix == ".json"
    )


def _validate_embedded_version(relative: Path, path: Path) -> None:
    expected = None
    if relative == Path("data/discovery-schedule.json"):
        expected = "job-agent.discovery-schedule.v1"
    elif relative == Path("data/pending-shortlist.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        artifact = ShortlistArtifact.from_dict(value)
        if value != artifact.to_dict():
            raise ValueError("Shortlist artifact must use the canonical public schema")
        return
    elif relative == Path("data/opportunity-decisions.json"):
        expected = "job-agent.opportunity-decisions.v2"
    elif _is_deep_grade_path(relative):
        _validate_deep_grade(relative, path)
        return
    elif _is_hosted_preparation_input_path(relative):
        _validate_hosted_preparation_input(relative, path)
        return
    elif _is_hosted_application_state_path(relative):
        _validate_hosted_application_state(relative, path)
        return
    if expected is None:
        if relative == Path("data/telegram-deliveries.sqlite"):
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'version'"
                ).fetchone()
            if row is None or row[0] != "job-agent.telegram-delivery.v1":
                raise ValueError(f"Unsupported embedded state version: {relative}")
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != expected:
        raise ValueError(f"Unsupported embedded state version: {relative}")


def _is_deep_grade_path(relative: Path) -> bool:
    return (
        len(relative.parts) == 3
        and relative.parts[:2] == ("data", "deep-grades")
        and relative.suffix == ".json"
    )


def _validate_deep_grade(relative: Path, path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise GradingContractError("deep-grade artifact must be an object")
        result = DeepGradeResult.from_dict(value)
    except (json.JSONDecodeError, GradingContractError, TypeError) as exc:
        raise ValueError(f"Invalid deep-grade artifact: {relative}") from exc
    if value != result.to_dict():
        raise ValueError(
            f"Invalid deep-grade artifact: non-canonical public fields in {relative}"
        )
    expected_name = sha256(result.opportunity_id.encode("utf-8")).hexdigest() + ".json"
    if relative.name != expected_name:
        raise ValueError(f"Invalid deep-grade artifact identity: {relative}")


def _is_hosted_preparation_input_path(relative: Path) -> bool:
    return (
        len(relative.parts) == 3
        and relative.parts[:2] == ("data", "hosted-preparation-inputs")
        and relative.suffix == ".json"
    )


def _validate_hosted_preparation_input(relative: Path, path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError
        parsed = HostedPreparationInput.from_dict(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid hosted preparation input: {relative}"
        ) from exc
    if value != parsed.to_dict():
        raise ValueError(
            f"Invalid hosted preparation input: non-canonical fields in {relative}"
        )
    expected_name = hosted_preparation_input_filename(
        parsed.stable_id,
        parsed.official_vacancy.version,
    )
    if relative.name != expected_name:
        raise ValueError(
            f"Invalid hosted preparation input identity: {relative}"
        )


def _is_hosted_application_state_path(relative: Path) -> bool:
    return (
        len(relative.parts) == 3
        and relative.parts[:2] == ("data", "hosted-application-state")
        and relative.suffix == ".json"
    )


def _validate_hosted_application_state(relative: Path, path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        parsed = HostedApplicationState.from_dict(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid hosted application state: {relative}"
        ) from exc
    if value != parsed.to_dict():
        raise ValueError(
            "Invalid hosted application state: non-canonical fields in "
            f"{relative}"
        )
    if relative.name != f"{parsed.application_id}.json":
        raise ValueError(
            f"Invalid hosted application state identity: {relative}"
        )


def _file_hash(path: Path) -> str:
    return f"sha256:{sha256(path.read_bytes()).hexdigest()}"


def _environment_authority() -> dict[str, Any]:
    return {
        "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
        "workflow": os.environ.get("JOB_AGENT_WORKFLOW", "run.yml"),
        "branch": os.environ.get("GITHUB_REF_NAME", "main"),
        "run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
        "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
        "stage": os.environ.get("JOB_AGENT_STATE_STAGE", "local"),
    }


def _generation(manifest: Mapping[str, Any]) -> tuple[int, int, int]:
    authority = manifest["authority"]
    stage_order = {
        "local": 0,
        "ingest": 1,
        "deep": 2,
        "decision": 3,
        "prepare": 4,
    }
    return (
        int(authority.get("run_id", 0)),
        int(authority.get("run_attempt", 0)),
        stage_order.get(str(authority.get("stage", "local")), 0),
    )


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    value = {key: item for key, item in manifest.items() if key != "manifest_digest"}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("write-manifest", "validate-manifest"):
        sub = commands.add_parser(command)
        sub.add_argument("--root", type=Path, default=Path("."))
        sub.add_argument("--package", type=Path)
        sub.add_argument("--workflow", default="run.yml")
        sub.add_argument("--branch", default="main")
        sub.add_argument(
            "--stage",
            choices=("local", "ingest", "deep", "decision", "prepare"),
            default="local",
        )
    install = commands.add_parser("install-package")
    install.add_argument("--root", type=Path, default=Path("."))
    install.add_argument("--package", type=Path, required=True)
    restore = commands.add_parser("restore-latest")
    restore.add_argument("--root", type=Path, default=Path("."))
    restore.add_argument("--repository", required=True)
    restore.add_argument("--workflow", default="run.yml")
    restore.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    bundle = StateBundle(args.root)
    if args.command == "write-manifest":
        authority = _environment_authority()
        authority.update(
            {"workflow": args.workflow, "branch": args.branch, "stage": args.stage}
        )
        bundle.write_manifest(authority)
    elif args.command == "validate-manifest":
        bundle.validate_manifest(args.package)
    elif args.command == "install-package":
        bundle.install_package(args.package)
    elif args.command == "restore-latest":
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required to restore Actions state")
        restore_latest(
            root=args.root,
            repository=args.repository,
            token=token,
            workflow=args.workflow,
            branch=args.branch,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GitHubActionsStateClient",
    "STATE_ARTIFACT_PREFIX",
    "STATE_BUNDLE_VERSION",
    "StateBundle",
    "main",
    "restore_latest",
]
