"""Concrete Telegram Bot API transport and callback consumer for applications."""

from __future__ import annotations

from datetime import timedelta
import html
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from application_telegram import (
    TelegramCommandHandler,
    TelegramPreparationCompleted,
    TelegramPreparationResolution,
    TelegramPreSubmitSummary,
)
from application_domain import ActionCommand, WorkflowAction
from application_interventions import InterventionRecord, UncertainSubmissionRecord
from telegram_delivery import TelegramDeliveryLedger


class TelegramApplicationApi:
    def __init__(
        self,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        user_id: str | None = None,
        callback_encoder: Callable[[ActionCommand], str],
        http=requests,
    ) -> None:
        token = token or os.environ["TELEGRAM_BOT_TOKEN"]
        self._chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
        self._user_id = user_id or os.environ["TELEGRAM_USER_ID"]
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = http
        self._callback_encoder = callback_encoder

    def send_pre_submit(
        self, summary: TelegramPreSubmitSummary, command: ActionCommand
    ) -> None:
        callback_data = self._callback_encoder(command)
        for attachment in summary.attachments:
            path = Path(attachment.path)
            if not path.is_file() or _file_hash(path) != attachment.sha256:
                raise RuntimeError("Telegram attachment no longer matches the manifest")
            self._post_document(
                path,
                filename=f"{attachment.kind}.pdf",
                caption=f"{attachment.kind} — {attachment.sha256}",
            )
        attachments = "\n".join(
            f"• {item.kind}: {item.sha256}" for item in summary.attachments
        )
        answers = (
            "\n".join(f"• {key}: {value}" for key, value in summary.principal_answers)
            or "• Nessuna risposta pubblicabile"
        )
        warnings = (
            "\n".join(f"• {warning}" for warning in summary.unresolved_warnings)
            or "• Nessuno"
        )
        message = "\n".join(
            (
                f"<b>Candidatura pronta: {html.escape(summary.title)}</b>",
                f"Azienda: {html.escape(summary.company)}",
                f"Sede: {html.escape(summary.location)}",
                f"Vacancy: {html.escape(summary.official_vacancy_version)}",
                f"Fingerprint: {html.escape(summary.role_fingerprint)}",
                f"Freshness: {html.escape(summary.freshness)}",
                "",
                "<b>Allegati esatti</b>",
                html.escape(attachments),
                "",
                "<b>Risposte principali</b>",
                html.escape(answers),
                "",
                "<b>Warning</b>",
                html.escape(warnings),
            )
        )
        self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Invia",
                                "callback_data": callback_data,
                            }
                        ]
                    ]
                },
            },
        )

    def send_status(self, message: str) -> None:
        self._post("sendMessage", {"chat_id": self._chat_id, "text": message})

    def send_preparation_completed(
        self, summary: TelegramPreparationCompleted, command: ActionCommand
    ) -> None:
        callback_data = self._callback_encoder(command)
        message = "\n".join(
            (
                f"<b>CV completo: {html.escape(summary.title)}</b>",
                f"Azienda: {html.escape(summary.company)}",
                f"Sede: {html.escape(summary.location)}",
                "CV e cover letter sono verificati e pronti.",
            )
        )
        self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": WorkflowAction.FILL.value,
                                "callback_data": callback_data,
                            }
                        ]
                    ]
                },
            },
        )

    def send_preparation_resolution(
        self,
        summary: TelegramPreparationResolution,
        command: ActionCommand | None,
    ) -> None:
        message = "\n".join(
            (
                f"<b>Preparazione non completata: {html.escape(summary.title)}</b>",
                f"Azienda: {html.escape(summary.company)}",
                f"Sede: {html.escape(summary.location)}",
                f"Esito: {html.escape(summary.outcome)}",
                f"Motivo: {html.escape(summary.reason)}",
                "Nessun CV è pronto e Compila non è disponibile.",
                "Nessun nuovo tentativo verrà avviato automaticamente.",
            )
        )
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        if command is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {
                            "text": WorkflowAction.RETRY_PREPARATION.value,
                            "callback_data": self._callback_encoder(command),
                        }
                    ]
                ]
            }
        self._post("sendMessage", payload)

    def send_intervention(
        self, intervention: InterventionRecord, command: ActionCommand
    ) -> None:
        callback_data = self._callback_encoder(command)
        message = "\n".join(
            (
                "<b>Intervento umano richiesto</b>",
                html.escape(intervention.explanation),
                "Il browser Job Applications è pronto e nessuna azione protetta è stata eseguita.",
                "Risolvi il blocco nel browser, poi premi Riprendi.",
            )
        )
        self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Riprendi",
                                "callback_data": callback_data,
                            }
                        ]
                    ]
                },
            },
        )

    def send_uncertain_submission(
        self,
        uncertain: UncertainSubmissionRecord,
        command: ActionCommand | None,
    ) -> None:
        callback_data = None if command is None else self._callback_encoder(command)
        sources = ", ".join(uncertain.inspection.sources_checked)
        unavailable = ", ".join(uncertain.inspection.sources_unavailable)
        message = "\n".join(
            (
                "<b>Esito della candidatura incerto</b>",
                f"Evidenze controllate: {html.escape(sources or 'nessuna')}.",
                f"Evidenze non disponibili: {html.escape(unavailable or 'nessuna')}.",
                "Nessun secondo invio verrà tentato automaticamente.",
                (
                    "Se hai verificato che non è stata inviata, confermalo qui; "
                    "servirà comunque un nuovo Invia."
                    if command is not None
                    else "La verifica non è completa: controlla manualmente ATS e Gmail."
                ),
            )
        )
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        if command is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Conferma non inviata",
                            "callback_data": callback_data,
                        }
                    ]
                ]
            }
        self._post("sendMessage", payload)

    def poll_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = self._http.get(
                f"{self._base}/getUpdates", params=params, timeout=timeout + 5
            )
        except Exception:
            raise RuntimeError("Telegram update polling failed safely") from None
        result = self._result(response, "Telegram update polling failed safely")
        if not isinstance(result, list):
            raise RuntimeError("Telegram update polling failed safely")
        return [dict(item) for item in result if isinstance(item, Mapping)]

    def acknowledge_callback(self, callback_query_id: str, text: str) -> None:
        self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def is_authorized_callback(self, callback: Mapping[str, Any]) -> bool:
        sender = callback.get("from")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        return (
            isinstance(sender, Mapping)
            and isinstance(chat, Mapping)
            and str(sender.get("id")) == str(self._user_id)
            and str(chat.get("id")) == str(self._chat_id)
        )

    def _post(self, method: str, payload: dict[str, Any]) -> None:
        try:
            response = self._http.post(
                f"{self._base}/{method}", json=payload, timeout=15
            )
        except Exception:
            raise RuntimeError("Telegram application delivery failed safely") from None
        self._result(response, "Telegram application delivery failed safely")

    def _post_document(self, path: Path, *, filename: str, caption: str) -> None:
        try:
            with path.open("rb") as document:
                response = self._http.post(
                    f"{self._base}/sendDocument",
                    data={"chat_id": self._chat_id, "caption": caption},
                    files={"document": (filename, document, "application/pdf")},
                    timeout=30,
                )
        except Exception:
            raise RuntimeError("Telegram document delivery failed safely") from None
        self._result(response, "Telegram document delivery failed safely")

    @staticmethod
    def _result(response, error: str):
        try:
            if not response.ok:
                raise RuntimeError(error)
            value = response.json()
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise RuntimeError(error)
            return value.get("result")
        except Exception:
            raise RuntimeError(error) from None


class TelegramApplicationConsumer:
    def __init__(
        self,
        *,
        coordinator,
        api: TelegramApplicationApi,
        ledger: TelegramDeliveryLedger,
    ) -> None:
        self._api = api
        self._handler = TelegramCommandHandler(coordinator, transport=api)
        self._ledger = ledger
        self._offset: int | None = None

    def present_submit(self, application_id: str, *, actor: str):
        return self._handler.present_submit(application_id, actor=actor)

    def consume_once(self, *, timeout: int = 25) -> int:
        handled = 0
        for update in self._api.poll_updates(offset=self._offset, timeout=timeout):
            update_id = int(update.get("update_id", -1))
            if update_id >= 0:
                self._offset = max(self._offset or 0, update_id + 1)
            callback = update.get("callback_query")
            if not isinstance(callback, Mapping):
                continue
            key = f"application-telegram-update:{update_id}"
            state = self._ledger.begin_update(key)
            callback_id = str(callback.get("id", ""))
            if state != "new":
                self._api.acknowledge_callback(callback_id, "Azione già elaborata")
                continue
            if not self._api.is_authorized_callback(callback):
                self._ledger.mark_update(key, "completed")
                self._api.acknowledge_callback(
                    callback_id, "Utente o chat non autorizzati"
                )
                continue
            try:
                result = self._handler.handle_callback_data(
                    str(callback.get("data", ""))
                )
            except Exception:
                self._ledger.mark_update(key, "uncertain")
                raise
            self._ledger.mark_update(key, "completed")
            self._api.acknowledge_callback(callback_id, result.status.value)
            handled += 1
        return handled


class TelegramPreparationCompletionNotifier:
    """Deliver one durable completion notice for an exact artifact version."""

    def __init__(
        self,
        *,
        coordinator,
        api: TelegramApplicationApi,
        ledger: TelegramDeliveryLedger,
        actor: str,
    ) -> None:
        if not str(actor).strip():
            raise ValueError("Telegram completion actor is required")
        self._coordinator = coordinator
        self._api = api
        self._ledger = ledger
        self._actor = str(actor)

    def needs_delivery(self, application_id: str) -> bool:
        """Return whether the current exact completion notice can be attempted."""

        key = self._delivery_key(application_id)
        if key is None:
            return False
        return self._ledger.outbound_status(key) in {
            None,
            "pending",
            "claimed",
        }

    def notify(
        self,
        application_id: str,
        *,
        before_send: Callable[[], Any] | None = None,
    ) -> bool:
        application = self._coordinator.get(application_id)
        key = self._delivery_key_for_application(application)
        if key is None:
            return False
        artifacts = application.artifacts
        vacancy = application.official_vacancy
        self._ledger.stage_outbound(key)
        claim_token = self._ledger.claim_outbound(key)
        if claim_token is None:
            return False
        try:
            command = self._coordinator.issue_authorization(
                application.application_id,
                WorkflowAction.FILL,
                actor=self._actor,
                ttl=timedelta(minutes=30),
            )
            summary = TelegramPreparationCompleted(
                application_id=application.application_id,
                company=str(application.opportunity.get("company", "unknown")),
                title=str(application.opportunity.get("title", "unknown")),
                location=str(application.opportunity.get("location", "unknown")),
                official_vacancy_version=vacancy.version,
                artifact_version=artifacts.version,
            )
        except Exception:
            self._ledger.release_outbound(key, claim_token)
            raise
        try:
            if before_send is not None:
                before_send()
        except Exception:
            self._ledger.release_outbound(key, claim_token)
            raise
        if not self._ledger.mark_outbound_sending(key, claim_token):
            raise RuntimeError(
                "Telegram completion delivery claim changed before send"
            )
        try:
            self._api.send_preparation_completed(summary, command)
        except Exception:
            self._ledger.mark_outbound_uncertain(key, claim_token)
            raise
        if not self._ledger.mark_outbound_sent(key, claim_token):
            raise RuntimeError(
                "Telegram completion delivery claim changed after possible send"
            )
        return True

    def _delivery_key(self, application_id: str) -> str | None:
        return self._delivery_key_for_application(
            self._coordinator.get(application_id)
        )

    @staticmethod
    def _delivery_key_for_application(application) -> str | None:
        artifacts = application.artifacts
        vacancy = application.official_vacancy
        if (
            artifacts is None
            or vacancy is None
            or application.next_action != WorkflowAction.FILL
        ):
            return None
        return preparation_completion_delivery_key(
            application.application_id, artifacts.version
        )


class TelegramPreparationResolutionNotifier:
    """Durably report one failed intent and optionally expose its safe retry."""

    def __init__(
        self,
        *,
        coordinator,
        api: TelegramApplicationApi,
        ledger: TelegramDeliveryLedger,
        actor: str,
    ) -> None:
        if not str(actor).strip():
            raise ValueError("Telegram resolution actor is required")
        self._coordinator = coordinator
        self._api = api
        self._ledger = ledger
        self._actor = str(actor)

    def needs_delivery(self, application_id: str) -> bool:
        """Return whether the current exact resolution notice can be attempted."""

        context = self._delivery_context(application_id)
        if context is None:
            return False
        return self._ledger.outbound_status(context["key"]) in {
            None,
            "pending",
            "claimed",
        }

    def notify(
        self,
        application_id: str,
        *,
        before_send: Callable[[], Any] | None = None,
    ) -> bool:
        context = self._delivery_context(application_id)
        if context is None:
            return False
        application = context["application"]
        vacancy = context["vacancy"]
        resolution = context["resolution"]
        intent = context["intent"]
        outcome = context["outcome"]
        reason = context["reason"]
        key = context["key"]
        self._ledger.stage_outbound(key)
        claim_token = self._ledger.claim_outbound(key)
        if claim_token is None:
            return False
        try:
            command = None
            if resolution is not None and resolution.retry_safe:
                command = (
                    self._coordinator.issue_preparation_retry_authorization(
                        application.application_id,
                        actor=self._actor,
                        ttl=timedelta(minutes=30),
                    )
                )
            summary = TelegramPreparationResolution(
                application_id=application.application_id,
                company=str(application.opportunity.get("company", "unknown")),
                title=str(application.opportunity.get("title", "unknown")),
                location=str(application.opportunity.get("location", "unknown")),
                official_vacancy_version=vacancy.version,
                intent_id=intent.intent_id,
                outcome=outcome,
                reason=reason,
            )
            if before_send is not None:
                before_send()
        except Exception:
            self._ledger.release_outbound(key, claim_token)
            raise
        if not self._ledger.mark_outbound_sending(key, claim_token):
            raise RuntimeError(
                "Telegram resolution delivery claim changed before send"
            )
        try:
            self._api.send_preparation_resolution(summary, command)
        except Exception:
            self._ledger.mark_outbound_uncertain(key, claim_token)
            raise
        if not self._ledger.mark_outbound_sent(key, claim_token):
            raise RuntimeError(
                "Telegram resolution delivery claim changed after possible send"
            )
        return True

    def reissue_expired_retry(
        self,
        expired: ActionCommand,
        *,
        before_send: Callable[[], Any] | None = None,
    ) -> bool:
        """Issue one replacement button after a fresh exact safety check.

        This method is callback-driven only.  Its delivery identity includes
        the expired application authorization token, so the same stale control
        can never generate more than one visible replacement.
        """

        if expired.scope.action != WorkflowAction.RETRY_PREPARATION:
            return False
        context = self._delivery_context(expired.scope.application_id)
        if context is None:
            return False
        resolution = context["resolution"]
        intent = context["intent"]
        if (
            intent.intent_id != expired.scope.version
            or resolution is None
            or not resolution.retry_safe
        ):
            return False
        key = preparation_retry_reissue_delivery_key(expired)
        self._ledger.stage_outbound(key)
        claim_token = self._ledger.claim_outbound(key)
        if claim_token is None:
            return False
        try:
            replacement = (
                self._coordinator.issue_preparation_retry_authorization(
                    expired.scope.application_id,
                    actor=self._actor,
                    ttl=timedelta(minutes=30),
                )
            )
            summary = TelegramPreparationResolution(
                application_id=context["application"].application_id,
                company=str(
                    context["application"].opportunity.get(
                        "company", "unknown"
                    )
                ),
                title=str(
                    context["application"].opportunity.get("title", "unknown")
                ),
                location=str(
                    context["application"].opportunity.get(
                        "location", "unknown"
                    )
                ),
                official_vacancy_version=context["vacancy"].version,
                intent_id=intent.intent_id,
                outcome=context["outcome"],
                reason=context["reason"],
            )
            if before_send is not None:
                before_send()
        except ValueError:
            self._ledger.release_outbound(key, claim_token)
            return False
        except Exception:
            self._ledger.release_outbound(key, claim_token)
            raise
        if not self._ledger.mark_outbound_sending(key, claim_token):
            raise RuntimeError(
                "Telegram retry reissue delivery claim changed before send"
            )
        try:
            self._api.send_preparation_resolution(summary, replacement)
        except Exception:
            self._ledger.mark_outbound_uncertain(key, claim_token)
            raise
        if not self._ledger.mark_outbound_sent(key, claim_token):
            raise RuntimeError(
                "Telegram retry reissue delivery claim changed after possible send"
            )
        return True

    def _delivery_context(self, application_id: str) -> dict[str, Any] | None:
        application = self._coordinator.get(application_id)
        vacancy = application.official_vacancy
        if vacancy is None:
            return None
        resolution = self._coordinator.preparation_resolution(application_id)
        pending = tuple(
            intent
            for intent in application.operation_intents
            if intent.is_pending and intent.action == WorkflowAction.PREPARE
        )
        if len(pending) != 1:
            return None
        intent = pending[0]
        outcome = "failed" if resolution is None else resolution.phase
        reason = (
            "preparation failed locally; inspect the owner-local logs"
            if resolution is None
            else resolution.reason
        )
        key = preparation_resolution_delivery_key(
            application.application_id,
            intent.intent_id,
            outcome,
            reason,
            retry_available=bool(
                resolution is not None and resolution.retry_safe
            ),
        )
        return {
            "application": application,
            "vacancy": vacancy,
            "resolution": resolution,
            "intent": intent,
            "outcome": outcome,
            "reason": reason,
            "key": key,
        }


def preparation_completion_delivery_key(
    application_id: str, artifact_version: str
) -> str:
    if not application_id.strip() or not artifact_version.strip():
        raise ValueError("Preparation completion identity is incomplete")
    return (
        "application-preparation-completed:"
        f"{application_id}:{artifact_version}"
    )


def preparation_resolution_delivery_key(
    application_id: str,
    intent_id: str,
    outcome: str,
    reason: str,
    *,
    retry_available: bool,
) -> str:
    fields = tuple(str(value).strip() for value in (
        application_id,
        intent_id,
        outcome,
        reason,
    ))
    if any(not value for value in fields):
        raise ValueError("Preparation resolution identity is incomplete")
    digest = hashlib.sha256(
        json.dumps(
            (*fields, retry_available),
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"application-preparation-resolution:{digest}"


def preparation_retry_reissue_delivery_key(expired: ActionCommand) -> str:
    if expired.scope.action != WorkflowAction.RETRY_PREPARATION:
        raise ValueError("Only a preparation retry can be reissued")
    fields = tuple(
        str(value).strip()
        for value in (
            expired.scope.application_id,
            expired.scope.version,
            expired.token,
        )
    )
    if any(not value for value in fields):
        raise ValueError("Preparation retry reissue identity is incomplete")
    digest = hashlib.sha256(
        json.dumps(fields, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"application-preparation-retry-reissue:{digest}"


def build_application_consumer(
    coordinator,
    *,
    ledger_path: Path = Path("data/application-telegram-callbacks.sqlite"),
    token: str | None = None,
    chat_id: str | None = None,
    user_id: str | None = None,
    callback_encoder: Callable[[ActionCommand], str],
) -> TelegramApplicationConsumer:
    return TelegramApplicationConsumer(
        coordinator=coordinator,
        api=TelegramApplicationApi(
            token=token,
            chat_id=chat_id,
            user_id=user_id,
            callback_encoder=callback_encoder,
        ),
        ledger=TelegramDeliveryLedger(ledger_path),
    )


__all__ = [
    "TelegramApplicationApi",
    "TelegramApplicationConsumer",
    "TelegramPreparationCompletionNotifier",
    "TelegramPreparationResolutionNotifier",
    "build_application_consumer",
    "preparation_completion_delivery_key",
    "preparation_resolution_delivery_key",
    "preparation_retry_reissue_delivery_key",
]


def _file_hash(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
