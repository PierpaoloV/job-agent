"""Transport-neutral Telegram callback mapping for opportunity decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from opportunity_domain import (
    DecisionAction,
    DecisionCommand,
    DecisionResult,
    DecisionStatus,
)


@dataclass(frozen=True)
class TelegramButton:
    label: str
    callback_data: str


class TelegramTransport(Protocol):
    def send_role_card(self, card, buttons: tuple[TelegramButton, ...]) -> None: ...

    def send_details(self, details) -> None: ...

    def send_status(self, message: str) -> None: ...


_STATUS_MESSAGES = {
    "approved": "Candidatura approvata per questa versione verificata.",
    "discarded": "Opportunità scartata con motivo condizionale salvato.",
    "expired": "Questo pulsante è scaduto: riapri la scheda del ruolo.",
    "replayed": "Questa decisione è già stata elaborata.",
    "mismatched": "Il pulsante non corrisponde a questa opportunità.",
    "stale": "La vacancy è cambiata: controlla la nuova versione.",
    "invalid_state": "Questa decisione non è valida nello stato corrente.",
    "not_verified": "Serve prima una vacancy ufficiale verificata.",
    "needs_reason": "Scrivi il motivo dello scarto per completare la decisione.",
    "details": "Dettagli ufficiali mostrati senza avviare la candidatura.",
}


class OpportunityTelegramHandler:
    def __init__(self, workflow, transport: TelegramTransport | None = None) -> None:
        self._workflow = workflow
        self._transport = transport

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(action.value for action in DecisionAction)

    def create_callback(
        self,
        stable_id: str,
        verified_version: str,
        label: str,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ) -> DecisionCommand:
        try:
            action = DecisionAction(label)
        except ValueError as exc:
            raise ValueError(f"Unsupported Telegram action: {label}") from exc
        return self._workflow.issue_decision_authorization(
            stable_id,
            verified_version,
            action,
            actor=actor,
            ttl=ttl,
        )

    def handle_callback(self, command: DecisionCommand) -> DecisionResult:
        result = self._workflow.decide(command)
        self._publish_result(result)
        return result

    def present_role(
        self,
        stable_id: str,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ):
        record = self._workflow.get(stable_id)
        snapshot = record.latest_snapshot
        if snapshot is None:
            raise ValueError("Opportunity has no verified official vacancy")
        card = self._workflow.role_card(stable_id)
        buttons = tuple(
            TelegramButton(
                action.value,
                self.encode_callback(
                    self._workflow.issue_decision_authorization(
                        stable_id,
                        snapshot.version,
                        action,
                        actor=actor,
                        ttl=ttl,
                    )
                ),
            )
            for action in DecisionAction
        )
        if self._transport is not None:
            self._transport.send_role_card(card, buttons)
        return card, buttons

    @staticmethod
    def encode_callback(command: DecisionCommand) -> str:
        return f"opp:{command.token}"

    def handle_callback_data(
        self, callback_data: str, *, reason: str | None = None
    ) -> DecisionResult:
        if not callback_data.startswith("opp:"):
            result = DecisionResult(DecisionStatus.MISMATCHED)
            self._publish_result(result)
            return result
        command = self._workflow.command_for_token(
            callback_data.removeprefix("opp:"), reason=reason
        )
        if command is None:
            result = DecisionResult(DecisionStatus.MISMATCHED)
            self._publish_result(result)
            return result
        return self.handle_callback(command)

    def _publish_result(self, result: DecisionResult) -> None:
        if self._transport is None:
            return
        if result.details is not None:
            self._transport.send_details(result.details)
        self._transport.send_status(
            result.message or _STATUS_MESSAGES[result.status.value]
        )
