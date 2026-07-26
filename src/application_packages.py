"""Private, atomic application packages and non-secret central indexes."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from application_domain import ApplicationSnapshot
from application_safety_report import render_safety_state


_INDEX_FIELDS = (
    "application_id",
    "company",
    "title",
    "location",
    "lifecycle",
    "submission_status",
    "updated_at",
)


class LocalApplicationPackageWriter:
    """Publish immutable private packages through one atomic catalog pointer."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._packages = self._root / "packages"
        self._indexes = self._root / "indexes"
        self._catalog_path = self._root / "current.json"
        self._lock_path = self._root / ".write.lock"

    @classmethod
    def for_repository(cls, repository_root: Path) -> "LocalApplicationPackageWriter":
        return cls(Path(repository_root) / "data" / "private" / "job-applications")

    @property
    def markdown_index_path(self) -> Path:
        return self._current_index_path() / "applications.md"

    @property
    def csv_index_path(self) -> Path:
        return self._current_index_path() / "applications.csv"

    def package_path(self, application_id: str) -> Path:
        catalog = self._read_catalog()
        try:
            version = catalog["packages"][application_id]
        except KeyError as exc:
            raise FileNotFoundError(
                f"No published package for application {application_id}"
            ) from exc
        return self._package_version_path(application_id, str(version))

    def write(self, application: ApplicationSnapshot) -> Path:
        self._prepare_directory(self._root)
        self._prepare_directory(self._packages)
        self._prepare_directory(self._indexes)
        with self._exclusive_lock():
            snapshot = application.to_dict()
            package_version = _content_digest(snapshot)
            package = self._build_package(application, snapshot, package_version)

            current = self._read_catalog()
            packages = {
                str(key): str(value)
                for key, value in current.get("packages", {}).items()
            }
            packages[application.application_id] = package_version
            index_version = self._build_indexes(packages)

            # One replace makes the package and both index formats visible together.
            self._write_json(
                self._catalog_path,
                {"packages": packages, "index": index_version},
            )
            self._fsync_directory(self._root)
            return package / "report.md"

    def _build_package(
        self,
        application: ApplicationSnapshot,
        snapshot: dict[str, Any],
        version: str,
    ) -> Path:
        versions = self._package_versions_path(application.application_id)
        self._prepare_directory(versions)
        destination = versions / version
        if destination.exists():
            return destination

        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=versions))
        os.chmod(staging, 0o700)
        try:
            self._write_json(staging / "application.json", snapshot)
            self._write_json(staging / "brief.json", application.opportunity)
            if application.official_vacancy is not None:
                self._write_json(
                    staging / "official-vacancy.json",
                    snapshot["official_vacancy"],
                )
            if application.manifest is not None:
                self._write_json(
                    staging / "answers.json",
                    {
                        "manifest_version": application.manifest.version,
                        "answer_hash": application.manifest.answer_hash,
                        "answers": application.manifest.answers,
                        "answer_disclosures": snapshot["manifest"][
                            "answer_disclosures"
                        ],
                        "unresolved_warnings": list(
                            application.manifest.unresolved_warnings
                        ),
                    },
                )
            self._write_json(
                staging / "audit.json",
                {
                    "history": snapshot["history"],
                    "authorizations": snapshot["authorizations"],
                    "approvals": snapshot["approvals"],
                    "operation_intents": snapshot["operation_intents"],
                    "submission_intents": snapshot["submission_intents"],
                    "capacity_exception": snapshot["capacity_exception"],
                    "preparation_reminders": snapshot["preparation_reminders"],
                    "prior_applications": snapshot["prior_applications"],
                },
            )
            self._write_json(
                staging / "submission-evidence.json",
                {
                    "status": (
                        None
                        if application.outcome is None
                        else application.outcome.status.value
                    ),
                    "recorded_at": (
                        None
                        if application.outcome is None
                        else application.outcome.recorded_at
                    ),
                    "evidence": (
                        None
                        if application.outcome is None
                        or application.outcome.evidence is None
                        else snapshot["outcome"]["evidence"]
                    ),
                },
            )
            self._write_json(
                staging / "correspondence.json",
                snapshot["correspondence"],
            )
            artifact_manifest = self._package_artifacts(application, staging)
            self._write_json(
                staging / "package-manifest.json",
                {
                    "application_id": application.application_id,
                    "artifact_version": (
                        None
                        if application.artifacts is None
                        else application.artifacts.version
                    ),
                    "artifacts": artifact_manifest,
                },
            )
            self._write_text(staging / "report.md", self._render_report(application))
            self._fsync_directory(staging)
            os.replace(staging, destination)
            os.chmod(destination, 0o700)
            self._fsync_directory(versions)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination

    def _package_artifacts(
        self, application: ApplicationSnapshot, package: Path
    ) -> dict[str, dict[str, str]]:
        if application.artifacts is None:
            return {}
        artifacts_dir = package / "artifacts"
        self._prepare_directory(artifacts_dir)
        values = {
            "cv": (
                Path(application.artifacts.cv_path),
                application.artifacts.cv_hash,
                artifacts_dir / "cv.pdf",
            ),
            "cover_letter": (
                Path(application.artifacts.cover_letter_path),
                application.artifacts.cover_letter_hash,
                artifacts_dir / "cover-letter.pdf",
            ),
        }
        manifest: dict[str, dict[str, str]] = {}
        for kind, (source, expected_hash, destination) in values.items():
            if not source.is_file() or _file_hash(source) != expected_hash:
                raise RuntimeError("Exact application artifact is unavailable")
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            if _file_hash(destination) != expected_hash:
                raise RuntimeError("Packaged application artifact did not verify")
            self._fsync_file(destination)
            manifest[kind] = {
                "path": str(destination.relative_to(package)),
                "sha256": expected_hash,
            }
        self._fsync_directory(artifacts_dir)
        return manifest

    def _build_indexes(self, packages: dict[str, str]) -> str:
        rows = [
            _index_row(
                json.loads(
                    (
                        self._package_version_path(application_id, version)
                        / "application.json"
                    ).read_text(encoding="utf-8")
                )
            )
            for application_id, version in sorted(packages.items())
        ]
        version = _content_digest(rows)
        versions = self._indexes / "versions"
        self._prepare_directory(versions)
        destination = versions / version
        if destination.exists():
            return version

        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=versions))
        os.chmod(staging, 0o700)
        try:
            markdown = [
                "# Application index",
                "",
                "| " + " | ".join(_INDEX_FIELDS) + " |",
                "| " + " | ".join("---" for _ in _INDEX_FIELDS) + " |",
            ]
            markdown.extend(
                "| "
                + " | ".join(_markdown_cell(row[field]) for field in _INDEX_FIELDS)
                + " |"
                for row in rows
            )
            self._write_text(staging / "applications.md", "\n".join(markdown) + "\n")

            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=_INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            self._write_text(staging / "applications.csv", output.getvalue())
            self._fsync_directory(staging)
            os.replace(staging, destination)
            os.chmod(destination, 0o700)
            self._fsync_directory(versions)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return version

    def _current_index_path(self) -> Path:
        catalog = self._read_catalog()
        try:
            version = str(catalog["index"])
        except KeyError as exc:
            raise FileNotFoundError("No application index has been published") from exc
        return self._indexes / "versions" / version

    def _package_versions_path(self, application_id: str) -> Path:
        application_root = self._packages / _safe_application_id(application_id)
        self._prepare_directory(application_root)
        return application_root / "versions"

    def _package_version_path(self, application_id: str, version: str) -> Path:
        return self._package_versions_path(application_id) / version

    def _read_catalog(self) -> dict[str, Any]:
        if not self._catalog_path.exists():
            return {"packages": {}}
        return json.loads(self._catalog_path.read_text(encoding="utf-8"))

    def _exclusive_lock(self):
        writer = self

        class Lock:
            def __enter__(self):
                descriptor = os.open(
                    writer._lock_path, os.O_RDWR | os.O_CREAT, 0o600
                )
                os.chmod(writer._lock_path, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self.descriptor = descriptor
                return self

            def __exit__(self, exc_type, exc, traceback):
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
                os.close(self.descriptor)

        return Lock()

    @staticmethod
    def _render_report(application: ApplicationSnapshot) -> str:
        lines = [
            f"# Application package: {application.application_id}",
            "",
            f"- Company: {application.opportunity.get('company', 'unknown')}",
            f"- Position: {application.opportunity.get('title', 'unknown')}",
            f"- State: {application.lifecycle_state.value}",
            f"- Opportunity version: {application.opportunity_version}",
            "- Capacity exception: "
            + (
                "none"
                if application.capacity_exception is None
                else (
                    f"{application.capacity_exception.kind.value} — "
                    f"{application.capacity_exception.reason}"
                )
            ),
            "",
            "## Freshness and prior applications",
            "",
            *(
                f"- Preparation reminder: {reminder.priority.value} at "
                f"{reminder.emitted_at}; deadline "
                f"{reminder.deadline_at or 'none'}"
                for reminder in application.preparation_reminders
            ),
            *(
                f"- Prior application: {prior.application_id} — "
                f"{prior.lifecycle_state.value}; changes "
                + (", ".join(prior.material_changes) or "none")
                for prior in application.prior_applications
            ),
            "",
            "## Transitions",
            "",
            *(f"- {item.occurred_at}: {item.state.value}" for item in application.history),
            "",
            "## Exact answers",
            "",
        ]
        if application.manifest is not None:
            lines.extend(
                f"- {key}: {value}"
                for key, value in sorted(application.manifest.answers.items())
            )
        lines.extend(["", "## Submission evidence", ""])
        if application.outcome is None:
            lines.append("- Pending")
        elif application.outcome.evidence is None:
            lines.extend(
                [
                    f"- Status: {application.outcome.status.value}",
                    f"- Recorded: {application.outcome.recorded_at or 'unavailable'}",
                    "- Positive evidence: unavailable",
                ]
            )
        else:
            evidence = application.outcome.evidence
            lines.extend(
                [
                    f"- Status: {application.outcome.status.value}",
                    f"- Captured: {evidence.captured_at}",
                    "- Verified by: "
                    + ", ".join(item.value for item in evidence.verified_by),
                    f"- Confirmation: {evidence.confirmation_id or 'unavailable'}",
                    f"- ATS application: {evidence.ats_application_id or 'unavailable'}",
                    f"- ATS status: {evidence.ats_status or 'unavailable'}",
                    f"- Email receipt: {evidence.email_receipt_id or 'unavailable'}",
                ]
            )
        lines.extend(["", "## Safety state", ""])
        lines.extend(render_safety_state(application))
        lines.extend(["", "## Career correspondence", ""])
        if not application.correspondence:
            lines.append("- None")
        else:
            for event in application.correspondence:
                transition = (
                    f"{event.previous_state.value} -> {event.resulting_state.value}"
                    if event.previous_state is not None
                    and event.resulting_state is not None
                    else "not applied"
                )
                evidence = event.evidence_role or "message classification"
                if event.evidence_role == "application_receipt_only":
                    evidence = (
                        "application receipt only; not submission verification"
                    )
                lines.extend(
                    [
                        f"- Message: {event.message_id}",
                        f"  - Received: {event.received_at}",
                        f"  - Classification: {event.classification.value}",
                        f"  - Lifecycle: {transition}",
                        f"  - Evidence: {evidence}",
                        f"  - Summary: {event.summary}",
                        f"  - Draft: {event.draft_id or 'none'}",
                        "  - Telegram classification request: "
                        f"{event.classification_request_id or 'none'}",
                    ]
                )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        LocalApplicationPackageWriter._write_text(
            path, json.dumps(value, indent=2, sort_keys=True) + "\n"
        )

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _index_row(value: dict[str, Any]) -> dict[str, str]:
    opportunity = value.get("opportunity", {})
    outcome = value.get("outcome")
    history = value.get("history", [])
    updated_at = str(history[-1]["occurred_at"]) if history else "unknown"
    evidence = outcome.get("evidence") if isinstance(outcome, dict) else None
    if isinstance(outcome, dict) and outcome.get("recorded_at"):
        updated_at = str(outcome["recorded_at"])
    if isinstance(evidence, dict) and evidence.get("captured_at"):
        updated_at = str(evidence["captured_at"])
    correspondence = value.get("correspondence", [])
    if correspondence:
        updated_at = max(
            updated_at,
            *(str(item.get("recorded_at", "unknown")) for item in correspondence),
        )
    return {
        "application_id": str(value.get("application_id", "unknown")),
        "company": str(opportunity.get("company", "unknown")),
        "title": str(opportunity.get("title", "unknown")),
        "location": str(opportunity.get("location", "unknown")),
        "lifecycle": str(value.get("lifecycle_state", "unknown")),
        "submission_status": (
            "pending" if outcome is None else str(outcome.get("status", "unknown"))
        ),
        "updated_at": updated_at,
    }


def _safe_application_id(application_id: str) -> str:
    readable = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in application_id
    ) or "application"
    identity = hashlib.sha256(application_id.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{identity}"


def _content_digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _markdown_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


__all__ = ["LocalApplicationPackageWriter"]
