"""Locked local persistence and derived reporting for applications."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
from typing import Callable, Iterator, Mapping

from application_domain import ApplicationSnapshot
from application_safety_report import render_safety_state
from requirements_evidence import RequirementsEvidenceMatrix


class JsonApplicationStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def load(self, application_id: str) -> ApplicationSnapshot:
        path = self._path(application_id)
        if not path.exists():
            raise KeyError(application_id)
        return ApplicationSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, application: ApplicationSnapshot) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        path = self._path(application.application_id)
        temporary = path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(
                json.dumps(application.to_dict(), indent=2, sort_keys=True) + "\n"
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def transact(
        self,
        application_id: str,
        operation: Callable[[ApplicationSnapshot], ApplicationSnapshot],
    ) -> ApplicationSnapshot:
        self._root.mkdir(parents=True, exist_ok=True)
        lock_path = self._path(application_id).with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self.load(application_id)
                updated = operation(current)
                if updated != current:
                    self.save(updated)
                return updated
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def capacity_lock(self) -> Iterator[None]:
        """Serialize workload admission across per-application transactions."""

        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self._root / ".preparation-capacity.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def list_authorizations(self):
        return tuple(
            authorization
            for application in self.list()
            for authorization in application.authorizations
        )

    def list(self) -> tuple[ApplicationSnapshot, ...]:
        if not self._root.exists():
            return ()
        return tuple(
            ApplicationSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self._root.glob("*.json"))
        )

    def _path(self, application_id: str) -> Path:
        return self._root / f"{safe_application_id(application_id)}.json"


class MarkdownApplicationReportWriter:
    def __init__(self, root: Path):
        self._root = Path(root)

    def write(self, application: ApplicationSnapshot) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{safe_application_id(application.application_id)}.md"
        lines = [
            f"# Synthetic application report: {application.application_id}",
            "",
            f"- Company: {application.opportunity.get('company', 'unknown')}",
            f"- Position: {application.opportunity.get('title', 'unknown')}",
            f"- Current state: {application.lifecycle_state.value}",
            f"- Authorization version: {application.authorization_version}",
            "- Capacity exception: "
            + (
                "none"
                if application.capacity_exception is None
                else (
                    f"{application.capacity_exception.kind.value} — "
                    f"{application.capacity_exception.reason}"
                )
            ),
            *(
                f"- Preparation reminder: {reminder.priority.value} at "
                f"{reminder.emitted_at} (deadline: {reminder.deadline_at or 'none'})"
                for reminder in application.preparation_reminders
            ),
            *(
                f"- Prior application: {prior.application_id} — "
                f"{prior.lifecycle_state.value} — "
                + (
                    "active ATS application"
                    if prior.is_active
                    else "historical application"
                )
                + " — changes: "
                + (", ".join(prior.material_changes) or "none")
                for prior in application.prior_applications
            ),
            "",
            "## States",
            "",
            *(f"- {event.occurred_at}: {event.state.value}" for event in application.history),
            "",
            "## Approvals",
            "",
            *(
                f"- {approval.scope.action.value}: {approval.scope.version} by {approval.actor}"
                for approval in application.approvals
            ),
            "",
            "## Artifacts",
            "",
        ]
        if application.artifacts:
            lines.extend(
                [
                    f"- Version: {application.artifacts.version}",
                    f"- CV: {application.artifacts.cv_path}",
                    f"- Cover letter: {application.artifacts.cover_letter_path}",
                    f"- CV hash: {application.artifacts.cv_hash}",
                    f"- Cover letter hash: {application.artifacts.cover_letter_hash}",
                    "- Evidence source: "
                    f"{application.artifacts.evidence_source_version or 'legacy/unknown'}",
                    "- Deep-grading matrix: "
                    f"{application.artifacts.matrix_version or 'legacy/unknown'}",
                    "- CV family: "
                    + (
                        application.artifacts.family.value
                        if application.artifacts.family
                        else "legacy/unknown"
                    ),
                    "- Fit decision: "
                    + (
                        f"stretch — {application.artifacts.stretch_decision.explanation}"
                        if application.artifacts.stretch_decision.is_stretch
                        else "standard"
                    ),
                ]
            )
            lines.extend(
                f"- Claim [{claim.kind.value}]: {claim.statement} "
                f"(evidence: {', '.join(claim.evidence_ids)})"
                for claim in application.artifacts.claims
            )
        matrix = application.opportunity.get("requirements_evidence_matrix")
        lines.extend(["", "## Requirements evidence matrix", ""])
        if isinstance(matrix, Mapping):
            canonical_matrix = RequirementsEvidenceMatrix.from_dict(matrix)
            lines.append(f"- Version: {canonical_matrix.version}")
            lines.extend(
                "- {id}: {requirement} [{status}] (evidence: {evidence})".format(
                    **row,
                    evidence=", ".join(row["evidence_ids"]) or "none",
                )
                for row in canonical_matrix.report_projection()
            )
        lines.extend(["", "## Answers", ""])
        if application.manifest:
            lines.extend(
                [
                    f"- Manifest: {application.manifest.version}",
                    f"- Role: {application.manifest.role_fingerprint}",
                    f"- Vacancy freshness: {application.manifest.vacancy_freshness}",
                    f"- Answer hash: {application.manifest.answer_hash}",
                    f"- Form snapshot hash: {application.manifest.form_snapshot_hash}",
                    f"- ATS page: {application.manifest.review_page}",
                    *(
                        f"- {key}: {value}"
                        for key, value in sorted(application.manifest.answers.items())
                    ),
                ]
            )
        lines.extend(["", "## Outcome", ""])
        if application.outcome:
            lines.extend(
                [
                    f"- Status: {application.outcome.status.value}",
                    f"- Confirmation: {application.outcome.confirmation_id or 'unavailable'}",
                ]
            )
        else:
            lines.append("- Pending")
        lines.extend(["", "## Safety state", ""])
        lines.extend(render_safety_state(application))
        lines.extend(["", "## Career correspondence", ""])
        if not application.correspondence:
            lines.append("- None")
        else:
            lines.extend(
                f"- {event.received_at}: {event.classification.value} "
                f"({event.message_id}) — {event.previous_state.value} -> "
                f"{event.resulting_state.value}"
                for event in application.correspondence
                if event.previous_state is not None
                and event.resulting_state is not None
            )
        lines.extend(["", "## Operation intents", ""])
        lines.extend(
            f"- {intent.intent_id}: {intent.action.value} "
            f"({intent.completed_at or intent.cancelled_at or 'pending'})"
            for intent in application.operation_intents
        )
        lines.extend(["", "## Submission intents", ""])
        lines.extend(
            f"- {intent.intent_id}: {intent.manifest_version} at {intent.created_at}"
            for intent in application.submission_intents
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def safe_application_id(application_id: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in application_id
    )
