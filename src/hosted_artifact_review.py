"""Protected Telegram review publication for hosted application artifacts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import re
from typing import Callable, Mapping, Sequence

import requests

from application_domain import PreparedArtifacts
from notify_telegram import (
    TelegramDocument,
    TelegramMessage,
    TelegramReceipt,
    TelegramSendUncertain,
    send_protected_document_group,
    send_protected_message,
)


_APPLICATION_ID = re.compile(r"approved-[0-9a-f]{16}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_RUN_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*"
)
_CALLBACK = re.compile(r"jar1:[A-Za-z0-9_-]{8,48}")
_BUTTON_LABELS = ("✅ Approva", "🔄 Rigenera")


@dataclass(frozen=True)
class ArtifactReviewReceipt:
    review_id: str
    document_message_ids: tuple[int, int]
    control_message_id: int
    expires_at: str


@dataclass(frozen=True)
class _ReviewAuthorization:
    review_id: str
    expires_at: str
    buttons: tuple[dict[str, str], dict[str, str]]


class GatewayArtifactReviewPublisher:
    """Publish two protected PDFs and bind their receipts to cloud cleanup."""

    def __init__(
        self,
        *,
        endpoint: str,
        internal_token: str,
        actor_id: str,
        chat_id: str,
        session=requests,
        document_sender: Callable[
            [Sequence[TelegramDocument]], tuple[TelegramReceipt, ...]
        ] = send_protected_document_group,
        control_sender: Callable[[TelegramMessage], TelegramReceipt] = (
            send_protected_message
        ),
    ) -> None:
        self._endpoint = str(endpoint).rstrip("/")
        self._internal_token = str(internal_token).strip()
        self._actor_id = str(actor_id).strip()
        self._chat_id = str(chat_id).strip()
        self._session = session
        self._document_sender = document_sender
        self._control_sender = control_sender
        if not self._endpoint.startswith("https://") or not all(
            (self._internal_token, self._actor_id, self._chat_id)
        ):
            raise ValueError("Artifact review gateway configuration is incomplete")

    def publish(
        self,
        *,
        application_id: str,
        official_vacancy_version: str,
        package_hash: str,
        run_url: str,
        artifacts: PreparedArtifacts,
    ) -> ArtifactReviewReceipt:
        _validate_identity(
            application_id,
            official_vacancy_version,
            package_hash,
            run_url,
        )
        authorization = self._authorize(
            application_id,
            official_vacancy_version,
            package_hash,
        )
        documents = (
            TelegramDocument(
                path=artifacts.cv_path,
                filename=f"CV-{application_id}.pdf",
                caption="CV su misura",
                sha256=artifacts.cv_hash,
            ),
            TelegramDocument(
                path=artifacts.cover_letter_path,
                filename=f"Lettera-{application_id}.pdf",
                caption="Lettera di presentazione",
                sha256=artifacts.cover_letter_hash,
            ),
        )
        document_receipts = tuple(self._document_sender(documents))
        if len(document_receipts) != 2:
            raise TelegramSendUncertain(
                "Telegram review omitted protected document receipts"
            )
        document_message_ids = tuple(
            item.message_id for item in document_receipts
        )
        self._bind(
            authorization.review_id,
            document_message_ids,
            None,
        )
        try:
            control_receipt = self._control_sender(
                TelegramMessage(
                    "🔒 <b>Revisione candidatura</b>\n\n"
                    "Controlla entrambi i PDF, poi approva o chiedi una nuova "
                    "generazione. I documenti scadono automaticamente dopo 24 "
                    "ore. Nessun modulo ATS verrà compilato prima "
                    "dell'approvazione.\n\n"
                    f'<a href="{run_url}">Apri la run cifrata</a>.',
                    reply_markup={
                        "inline_keyboard": [[dict(item) for item in authorization.buttons]]
                    },
                )
            )
            self._bind(
                authorization.review_id,
                document_message_ids,
                control_receipt.message_id,
            )
        except Exception as exc:
            raise TelegramSendUncertain(
                "Protected review publication is incomplete after document send"
            ) from exc
        return ArtifactReviewReceipt(
            review_id=authorization.review_id,
            document_message_ids=(
                document_receipts[0].message_id,
                document_receipts[1].message_id,
            ),
            control_message_id=control_receipt.message_id,
            expires_at=authorization.expires_at,
        )

    def _authorize(
        self,
        application_id: str,
        vacancy_version: str,
        package_hash: str,
    ) -> _ReviewAuthorization:
        response = self._session.post(
            f"{self._endpoint}/v1/review-authorizations",
            headers=self._headers(),
            json={
                "event_id": f"review:{application_id}:{package_hash}",
                "application_id": application_id,
                "official_vacancy_version": vacancy_version,
                "package_hash": package_hash,
                "actor_id": self._actor_id,
                "chat_id": self._chat_id,
            },
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(
                "Artifact review gateway rejected authorization "
                f"(HTTP {response.status_code})"
            )
        value = response.json()
        if not isinstance(value, Mapping):
            raise RuntimeError("Artifact review gateway returned invalid JSON")
        review_id = str(value.get("review_id", ""))
        expires_at = str(value.get("expires_at", ""))
        buttons = value.get("buttons")
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{8,48}", review_id)
            or not expires_at
            or not isinstance(buttons, list)
            or len(buttons) != 2
        ):
            raise RuntimeError("Artifact review gateway returned invalid controls")
        normalized = []
        for expected, button in zip(_BUTTON_LABELS, buttons, strict=True):
            if not isinstance(button, Mapping):
                raise RuntimeError("Artifact review gateway returned invalid controls")
            item = {
                "text": str(button.get("text", "")),
                "callback_data": str(button.get("callback_data", "")),
            }
            if item["text"] != expected or not _CALLBACK.fullmatch(
                item["callback_data"]
            ):
                raise RuntimeError("Artifact review gateway returned invalid controls")
            normalized.append(item)
        return _ReviewAuthorization(
            review_id=review_id,
            expires_at=expires_at,
            buttons=(normalized[0], normalized[1]),
        )

    def _bind(
        self,
        review_id: str,
        document_message_ids: tuple[int, ...],
        control_message_id: int | None,
    ) -> None:
        payload: dict[str, object] = {
            "document_message_ids": list(document_message_ids),
        }
        expected_status = "documents_sent"
        if control_message_id is not None:
            payload["control_message_id"] = control_message_id
            expected_status = "pending"
        response = self._session.post(
            f"{self._endpoint}/v1/artifact-reviews/{review_id}/messages",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        if not response.ok or response.json() != {"status": expected_status}:
            raise RuntimeError("Artifact review gateway did not bind messages")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._internal_token}",
            "Content-Type": "application/json",
        }


def _validate_identity(
    application_id: str,
    vacancy_version: str,
    package_hash: str,
    run_url: str,
) -> None:
    if not _APPLICATION_ID.fullmatch(str(application_id)):
        raise ValueError("Artifact review application id must be canonical")
    if not _SHA256.fullmatch(str(vacancy_version)) or not _SHA256.fullmatch(
        str(package_hash)
    ):
        raise ValueError("Artifact review hashes must be canonical")
    if not _RUN_URL.fullmatch(str(run_url)):
        raise ValueError("Artifact review run URL must be canonical")


def acknowledge_gateway_artifact_review(
    *,
    endpoint: str,
    internal_token: str,
    review_id: str,
    action: str,
    application_id: str,
    official_vacancy_version: str,
    package_hash: str,
    session=requests,
) -> str:
    """Confirm that an exact review decision reached authoritative state."""

    endpoint = str(endpoint).rstrip("/")
    internal_token = str(internal_token).strip()
    expected_status = {
        "approve_artifacts": "approved",
        "regenerate_artifacts": "regenerate_requested",
    }.get(action)
    if (
        not endpoint.startswith("https://")
        or not internal_token
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,48}", review_id)
        or expected_status is None
        or not _APPLICATION_ID.fullmatch(application_id)
        or not _SHA256.fullmatch(official_vacancy_version)
        or not _SHA256.fullmatch(package_hash)
    ):
        raise ValueError("Artifact review acknowledgement is not canonical")
    response = session.post(
        f"{endpoint}/v1/artifact-reviews/{review_id}/decision-ack",
        headers={
            "Authorization": f"Bearer {internal_token}",
            "Content-Type": "application/json",
        },
        json={
            "action": action,
            "application_id": application_id,
            "official_vacancy_version": official_vacancy_version,
            "package_hash": package_hash,
        },
        timeout=15,
    )
    if not response.ok or response.json() != {"status": expected_status}:
        raise RuntimeError("Artifact review acknowledgement was rejected")
    return expected_status


def recover_gateway_artifact_review_dispatch(
    *,
    endpoint: str,
    internal_token: str,
    review_id: str,
    action: str,
    application_id: str,
    official_vacancy_version: str,
    package_hash: str,
    confirmed_absent: bool,
    session=requests,
) -> str:
    """Explicitly retry only after an operator proves no GitHub run exists."""

    if not confirmed_absent:
        raise ValueError("GitHub run absence must be explicitly confirmed")
    endpoint = str(endpoint).rstrip("/")
    internal_token = str(internal_token).strip()
    if (
        not endpoint.startswith("https://")
        or not internal_token
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,48}", review_id)
        or action not in {"approve_artifacts", "regenerate_artifacts"}
        or not _APPLICATION_ID.fullmatch(application_id)
        or not _SHA256.fullmatch(official_vacancy_version)
        or not _SHA256.fullmatch(package_hash)
    ):
        raise ValueError("Artifact review dispatch recovery is not canonical")
    response = session.post(
        f"{endpoint}/v1/artifact-reviews/{review_id}/dispatch-recovery",
        headers={
            "Authorization": f"Bearer {internal_token}",
            "Content-Type": "application/json",
        },
        json={
            "confirmed_absent": True,
            "action": action,
            "application_id": application_id,
            "official_vacancy_version": official_vacancy_version,
            "package_hash": package_hash,
        },
        timeout=15,
    )
    if not response.ok or response.json() != {"status": "dispatch_accepted"}:
        raise RuntimeError("Artifact review dispatch recovery was rejected")
    return "dispatch_accepted"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("acknowledge", "recover-dispatch")
    )
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--official-vacancy-version", required=True)
    parser.add_argument("--package-hash", required=True)
    parser.add_argument("--confirmed-absent", action="store_true")
    args = parser.parse_args(argv)
    kwargs = {
        "endpoint": os.environ["JOB_AGENT_CALLBACK_GATEWAY_URL"],
        "internal_token": os.environ["JOB_AGENT_CALLBACK_GATEWAY_TOKEN"],
        "review_id": args.review_id,
        "action": args.action,
        "application_id": args.application_id,
        "official_vacancy_version": args.official_vacancy_version,
        "package_hash": args.package_hash,
    }
    if args.command == "acknowledge":
        acknowledge_gateway_artifact_review(**kwargs)
    else:
        recover_gateway_artifact_review_dispatch(
            **kwargs,
            confirmed_absent=args.confirmed_absent,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ArtifactReviewReceipt",
    "GatewayArtifactReviewPublisher",
    "acknowledge_gateway_artifact_review",
    "recover_gateway_artifact_review_dispatch",
    "main",
]
