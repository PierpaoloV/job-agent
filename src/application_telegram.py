"""Transport-neutral Telegram callback mapping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from application_domain import (
    ActionCommand,
    ApplicationSnapshot,
    CommandResult,
    CommandStatus,
    PreparationReminder,
    WorkflowAction,
)
from application_interventions import InterventionRecord, UncertainSubmissionRecord


@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    path: str
    sha256: str


@dataclass(frozen=True)
class TelegramPreSubmitSummary:
    application_id: str
    manifest_version: str
    company: str
    title: str
    location: str
    official_vacancy_version: str
    role_fingerprint: str
    attachments: tuple[TelegramAttachment, ...]
    principal_answers: tuple[tuple[str, str], ...]
    unresolved_warnings: tuple[str, ...]
    freshness: str
    capacity_exception: str | None
    prior_applications: tuple[str, ...]


@dataclass(frozen=True)
class TelegramPreparationReminder:
    reminder_id: str
    application_id: str
    company: str
    title: str
    priority: str
    deadline_at: str | None
    preparation_expires_at: str


@dataclass(frozen=True)
class TelegramPreparationCompleted:
    application_id: str
    company: str
    title: str
    location: str
    official_vacancy_version: str
    artifact_version: str


@dataclass(frozen=True)
class TelegramPreparationResolution:
    application_id: str
    company: str
    title: str
    location: str
    official_vacancy_version: str
    intent_id: str
    outcome: str
    reason: str


class ApplicationTelegramTransport(Protocol):
    def send_pre_submit(
        self, summary: TelegramPreSubmitSummary, command: ActionCommand
    ) -> None: ...

    def send_status(self, message: str) -> None: ...

    def send_intervention(
        self, intervention: InterventionRecord, command: ActionCommand
    ) -> None: ...

    def send_uncertain_submission(
        self,
        uncertain: UncertainSubmissionRecord,
        command: ActionCommand | None,
    ) -> None: ...

    def send_preparation_reminder(
        self, reminder: TelegramPreparationReminder
    ) -> None: ...

    def send_preparation_completed(
        self, summary: TelegramPreparationCompleted, command: ActionCommand
    ) -> None: ...

    def send_preparation_resolution(
        self,
        summary: TelegramPreparationResolution,
        command: ActionCommand | None,
    ) -> None: ...


class HumanGateCoordinator(Protocol):
    def get(self, application_id: str) -> ApplicationSnapshot: ...

    def issue_authorization(
        self,
        application_id: str,
        action: WorkflowAction,
        *,
        actor: str,
        ttl: timedelta,
    ) -> ActionCommand: ...

    def handle(self, command: ActionCommand) -> CommandResult: ...

    def command_for_token(self, token: str) -> ActionCommand | None: ...

    def reload_master_cv(self) -> str: ...

    def emit_due_preparation_reminders(self) -> tuple[PreparationReminder, ...]: ...
    def acknowledge_preparation_reminder(
        self, application_id: str, reminder_id: str
    ) -> PreparationReminder: ...


class TelegramCommandHandler:
    _ACTIONS = {
        action.value: action
        for action in (
            WorkflowAction.PREPARE,
            WorkflowAction.FILL,
            WorkflowAction.SUBMIT,
        )
    }
    _COMMANDS = ("Rileggi CV master",)

    def __init__(
        self,
        coordinator: HumanGateCoordinator,
        *,
        transport: ApplicationTelegramTransport | None = None,
    ):
        self._coordinator = coordinator
        self._transport = transport

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._ACTIONS)

    @property
    def command_labels(self) -> tuple[str, ...]:
        return self._COMMANDS

    def create_callback(
        self,
        application_id: str,
        label: str,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ) -> ActionCommand:
        try:
            action = self._ACTIONS[label]
        except KeyError as exc:
            raise ValueError(f"Unsupported Telegram action: {label}") from exc
        if action == WorkflowAction.SUBMIT:
            _, command = self.present_submit(
                application_id, actor=actor, ttl=ttl
            )
            return command
        return self._coordinator.issue_authorization(
            application_id, action, actor=actor, ttl=ttl
        )

    def present_submit(
        self,
        application_id: str,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ) -> tuple[TelegramPreSubmitSummary, ActionCommand]:
        application = self._coordinator.get(application_id)
        active_prior = next(
            (prior for prior in application.prior_applications if prior.is_active),
            None,
        )
        if active_prior is not None:
            message = (
                "Candidatura ATS attiva già nota: "
                f"{active_prior.application_id} "
                f"({active_prior.lifecycle_state.value}). "
                "Nessun nuovo Invia autorizzato."
            )
            if self._transport is not None:
                self._transport.send_status(message)
            raise ValueError(
                f"{active_prior.application_id} "
                f"{active_prior.lifecycle_state.value}: {message}"
            )
        command = self._coordinator.issue_authorization(
            application_id, WorkflowAction.SUBMIT, actor=actor, ttl=ttl
        )
        application = self._coordinator.get(application_id)
        summary = self._pre_submit_summary(application, command)
        if self._transport is not None:
            self._transport.send_pre_submit(summary, command)
        return summary, command

    def handle_callback(self, command: ActionCommand) -> CommandResult:
        result = self._coordinator.handle(command)
        if self._transport is not None:
            application = (
                None
                if result.lifecycle_state is None
                else self._coordinator.get(command.scope.application_id)
            )
            if (
                result.status == CommandStatus.INTERVENTION_REQUIRED
                and application is not None
                and application.intervention is not None
            ):
                resume = self._coordinator.command_for_token(
                    application.intervention.resume_token
                )
                send = getattr(self._transport, "send_intervention", None)
                if resume is not None and callable(send):
                    send(application.intervention, resume)
                else:
                    self._transport.send_status(
                        f"Intervento richiesto: {application.intervention.explanation}"
                    )
            elif (
                result.status == CommandStatus.UNCERTAIN
                and application is not None
                and application.uncertain_submission is not None
            ):
                token = application.uncertain_submission.resolution_token
                resolution = (
                    None
                    if token is None
                    else self._coordinator.command_for_token(token)
                )
                send = getattr(
                    self._transport, "send_uncertain_submission", None
                )
                if callable(send):
                    send(application.uncertain_submission, resolution)
                else:
                    self._transport.send_status(
                        _STATUS_MESSAGES[result.status.value]
                    )
            else:
                self._transport.send_status(_STATUS_MESSAGES[result.status.value])
        return result

    @staticmethod
    def encode_callback(command: ActionCommand) -> str:
        return f"app:{command.token}"

    def handle_callback_data(self, callback_data: str) -> CommandResult:
        if not callback_data.startswith("app:"):
            return CommandResult(CommandStatus.MISMATCHED, None, None)
        command = self._coordinator.command_for_token(
            callback_data.removeprefix("app:")
        )
        if command is None:
            return CommandResult(CommandStatus.MISMATCHED, None, None)
        return self.handle_callback(command)

    def handle_command(self, label: str) -> str:
        if label not in self._COMMANDS:
            raise ValueError(f"Unsupported Telegram command: {label}")
        return self._coordinator.reload_master_cv()

    def emit_due_preparation_reminders(
        self,
    ) -> tuple[TelegramPreparationReminder, ...]:
        summaries = []
        for reminder in self._coordinator.emit_due_preparation_reminders():
            application = self._coordinator.get(reminder.application_id)
            summary = TelegramPreparationReminder(
                reminder_id=reminder.reminder_id,
                application_id=reminder.application_id,
                company=str(application.opportunity.get("company", "unknown")),
                title=str(application.opportunity.get("title", "unknown")),
                priority=reminder.priority.value,
                deadline_at=reminder.deadline_at,
                preparation_expires_at=reminder.preparation_expires_at,
            )
            summaries.append(summary)
            if self._transport is None:
                continue
            send = getattr(self._transport, "send_preparation_reminder", None)
            if callable(send):
                send(summary)
            else:
                self._transport.send_status(
                    f"Promemoria candidatura {summary.company} — {summary.title} "
                    f"({summary.priority})."
                )
            self._coordinator.acknowledge_preparation_reminder(
                summary.application_id, summary.reminder_id
            )
        return tuple(summaries)

    def _pre_submit_summary(
        self, application: ApplicationSnapshot, command: ActionCommand
    ) -> TelegramPreSubmitSummary:
        manifest = application.manifest
        vacancy = application.official_vacancy
        artifacts = application.artifacts
        if manifest is None or vacancy is None or artifacts is None:
            raise ValueError("Pre-submit manifest is unavailable")
        if (
            command.scope.application_id != application.application_id
            or command.scope.version != manifest.version
            or command.scope.action != WorkflowAction.SUBMIT
        ):
            raise ValueError("Pre-submit manifest changed before presentation")
        opportunity = application.opportunity
        return TelegramPreSubmitSummary(
            application_id=application.application_id,
            manifest_version=manifest.version,
            company=str(opportunity.get("company", "unknown")),
            title=str(opportunity.get("title", "unknown")),
            location=str(opportunity.get("location", "unknown")),
            official_vacancy_version=vacancy.version,
            role_fingerprint=manifest.role_fingerprint,
            attachments=tuple(
                TelegramAttachment(
                    kind,
                    (
                        artifacts.cv_path
                        if kind == "cv"
                        else artifacts.cover_letter_path
                    ),
                    digest,
                )
                for kind in ("cv", "cover_letter")
                if (digest := manifest.artifact_hashes.get(kind)) is not None
            ),
            principal_answers=manifest.public_summary_answers,
            unresolved_warnings=manifest.unresolved_warnings,
            freshness=manifest.vacancy_freshness,
            capacity_exception=(
                None
                if application.capacity_exception is None
                else (
                    f"{application.capacity_exception.kind.value}: "
                    f"{application.capacity_exception.reason}"
                )
            ),
            prior_applications=tuple(
                f"{prior.application_id}: {prior.lifecycle_state.value}; changes: "
                + (", ".join(prior.material_changes) or "none")
                for prior in application.prior_applications
            ),
        )


__all__ = [
    "ApplicationTelegramTransport",
    "TelegramAttachment",
    "TelegramCommandHandler",
    "TelegramPreparationReminder",
    "TelegramPreparationResolution",
    "TelegramPreSubmitSummary",
]


_STATUS_MESSAGES = {
    "completed": "Candidatura inviata e verificata.",
    "replayed": "Questa azione è già stata elaborata.",
    "expired": "Il pulsante è scaduto: riapri la candidatura.",
    "stale": "La vacancy o gli allegati sono cambiati: serve una nuova revisione.",
    "mismatched": "Il pulsante non corrisponde a questa candidatura.",
    "failed": "Invio non completato: controlla il report locale.",
    "uncertain": "Esito incerto: verifica manualmente prima di qualsiasi altra azione.",
    "reconciliation_required": "Esito da riconciliare manualmente.",
    "intervention_required": "Intervento umano richiesto nel browser dedicato.",
    "resolved": "Esito incerto risolto: serve un nuovo Invia.",
    "accepted": "Azione accettata.",
    "capacity_reached": "Capacità di preparazione raggiunta: completa o scarta prima un lavoro attivo.",
}
