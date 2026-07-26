"""Read-only external adapters for dedicated career correspondence."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import parseaddr
import re
from dataclasses import replace
from typing import Any, Mapping, Protocol

from career_correspondence_domain import (
    CareerMessage,
    SenderKind,
    TelegramDispatchResult,
    TelegramOutboxItem,
    TelegramOutboxKind,
)


_GMAIL_SCOPE_PREFIX = "https://www.googleapis.com/auth/gmail."
_GMAIL_READONLY_SCOPE = _GMAIL_SCOPE_PREFIX + "readonly"


class GmailCareerMailboxReader:
    """Translate Gmail ``list/get`` responses without exposing write operations."""

    def __init__(self, service) -> None:
        self._service = service

    def verify_account(self, *, account_address: str) -> None:
        expected = account_address.strip().casefold()
        if not expected:
            raise ValueError("A dedicated career Gmail must be configured")
        credentials = getattr(
            getattr(self._service, "_http", None), "credentials", None
        )
        granted = getattr(credentials, "granted_scopes", None)
        scopes = granted or getattr(credentials, "scopes", None)
        gmail_scopes = {
            str(scope)
            for scope in (scopes or ())
            if str(scope).startswith(_GMAIL_SCOPE_PREFIX)
        }
        if gmail_scopes != {_GMAIL_READONLY_SCOPE}:
            raise ValueError(
                "Career monitoring requires only the read-only Gmail scope"
            )
        profile = self._service.users().getProfile(userId="me").execute()
        authenticated = str(profile.get("emailAddress", "")).strip().casefold()
        if authenticated != expected:
            raise ValueError(
                "The authenticated Gmail account is not the dedicated career mailbox"
            )

    def fetch(self, *, account_address: str) -> tuple[CareerMessage, ...]:
        self.verify_account(account_address=account_address)
        messages = self._service.users().messages()
        values = []
        page_token = None
        while True:
            arguments: dict[str, Any] = {
                "userId": "me",
                "q": "in:anywhere",
                "maxResults": 100,
                "includeSpamTrash": False,
            }
            if page_token:
                arguments["pageToken"] = page_token
            page = messages.list(**arguments).execute()
            for reference in page.get("messages", ()):
                raw = messages.get(
                    userId="me", id=str(reference["id"]), format="full"
                ).execute()
                values.append(_career_message(raw))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return tuple(values)


class TelegramMessageTransport(Protocol):
    def send_message(self, *, delivery_id: str, text: str) -> None: ...


class TelegramDeliveryError(RuntimeError):
    """A transport failure with explicit knowledge of possible external I/O."""

    def __init__(self, message: str, *, may_have_sent: bool) -> None:
        super().__init__(message)
        self.may_have_sent = bool(may_have_sent)


class TelegramOutboxStore(Protocol):
    def claim_next_telegram_delivery(self, *, claimed_at: str): ...
    def complete_telegram_delivery(
        self, delivery_id: str, *, claim_token: str, delivered_at: str
    ) -> None: ...
    def fail_telegram_delivery(
        self, delivery_id: str, *, claim_token: str, uncertain_at: str
    ) -> None: ...
    def retry_or_fail_telegram_delivery(
        self, delivery_id: str, *, claim_token: str, failed_at: str
    ) -> str: ...


class TelegramCorrespondenceOutboxDispatcher:
    """Deliver each durable local request once, then persist its acknowledgement."""

    def __init__(self, *, store: TelegramOutboxStore, transport, clock) -> None:
        self._store = store
        self._transport = transport
        self._clock = clock

    def dispatch_pending(self) -> TelegramDispatchResult:
        result = TelegramDispatchResult()
        while True:
            now = self._clock.now().isoformat()
            claim = self._store.claim_next_telegram_delivery(claimed_at=now)
            if claim.item is None:
                return result
            assert claim.token is not None
            try:
                self._transport.send_message(
                    delivery_id=claim.item.delivery_id,
                    text=_telegram_delivery_text(claim.item),
                )
                self._store.complete_telegram_delivery(
                    claim.item.delivery_id,
                    claim_token=claim.token,
                    delivered_at=now,
                )
                result = replace(result, delivered=result.delivered + 1)
            except TelegramDeliveryError as error:
                if error.may_have_sent:
                    self._store.fail_telegram_delivery(
                        claim.item.delivery_id,
                        claim_token=claim.token,
                        uncertain_at=now,
                    )
                    result = replace(result, uncertain=result.uncertain + 1)
                else:
                    status = self._store.retry_or_fail_telegram_delivery(
                        claim.item.delivery_id,
                        claim_token=claim.token,
                        failed_at=now,
                    )
                    field = "retryable" if status == "pending" else "failed"
                    result = replace(result, **{field: getattr(result, field) + 1})
            except Exception:
                # Untyped transport exceptions cannot prove that no request left
                # this process, so retrying them could duplicate a Telegram send.
                self._store.fail_telegram_delivery(
                    claim.item.delivery_id,
                    claim_token=claim.token,
                    uncertain_at=now,
                )
                result = replace(result, uncertain=result.uncertain + 1)


_PUBLIC_CLASSIFICATION_REASONS = {
    "application_not_linked_unambiguously",
    "conflicting_deterministic_signals",
    "lifecycle_transition_not_safe",
    "message_not_deterministic",
    "message_predates_current_lifecycle",
    "sender_not_authenticated",
    "sender_not_trusted_for_application",
}


def _telegram_delivery_text(item: TelegramOutboxItem) -> str:
    if item.kind == TelegramOutboxKind.DRAFT_REVIEW:
        return "Bozza locale pronta per revisione. Apri il report locale."
    reason = (
        item.reason
        if item.reason in _PUBLIC_CLASSIFICATION_REASONS
        else "human_review_required"
    )
    return f"Classificazione Telegram pronta. Apri il report locale; motivo: {reason}."


def _career_message(value: Mapping[str, Any]) -> CareerMessage:
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise RuntimeError("Gmail career message payload is unavailable")
    raw_headers = tuple(
        item for item in payload.get("headers", ()) if isinstance(item, Mapping)
    )
    headers = {
        str(item.get("name", "")).casefold(): str(item.get("value", ""))
        for item in raw_headers
    }
    authentication_results = tuple(
        str(item.get("value", ""))
        for item in raw_headers
        if str(item.get("name", "")).casefold() == "authentication-results"
    )
    sender_name, sender_address = parseaddr(headers.get("from", ""))
    sender_address = sender_address.strip().casefold()
    authenticated_domain = (
        _authenticated_from_domain(authentication_results[0], sender_address)
        if len(authentication_results) == 1
        else None
    )
    try:
        received_at = datetime.fromtimestamp(
            int(str(value["internalDate"])) / 1000,
            timezone.utc,
        ).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RuntimeError("Gmail career message timestamp is unavailable") from None
    return CareerMessage(
        message_id=str(value.get("id", "")),
        thread_id=str(value.get("threadId", "")),
        sender_address=sender_address,
        sender_name=sender_name.strip(),
        sender_kind=SenderKind.UNKNOWN,
        authenticated_sender=authenticated_domain is not None,
        subject=headers.get("subject", ""),
        body_text=_plain_text(payload),
        received_at=received_at,
        authenticated_domain=authenticated_domain,
    )


def _authenticated_from_domain(value: str, sender_address: str) -> str | None:
    authserv_id, separator, results = value.partition(";")
    if not separator or authserv_id.strip().casefold() != "mx.google.com":
        return None
    sender_domain = sender_address.rsplit("@", 1)[-1].casefold().rstrip(".")
    dmarc = re.search(
        r"\bdmarc\s*=\s*pass\b[^;]*\bheader\.from\s*=\s*@?([a-z0-9.-]+)",
        results,
        re.IGNORECASE,
    )
    aligned_transport = re.search(
        r"\b(?:dkim|spf)\s*=\s*pass\b", results, re.IGNORECASE
    )
    if dmarc is None or aligned_transport is None:
        return None
    authenticated_domain = dmarc.group(1).casefold().rstrip(".")
    if sender_domain != authenticated_domain and not sender_domain.endswith(
        "." + authenticated_domain
    ):
        return None
    return authenticated_domain


def _plain_text(payload: Mapping[str, Any]) -> str:
    if str(payload.get("mimeType", "")).casefold() == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return _decode(str(data))
    for part in payload.get("parts", ()):
        if isinstance(part, Mapping):
            text = _plain_text(part)
            if text:
                return text
    return ""


def _decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode(
            "utf-8", errors="replace"
        )
    except (ValueError, TypeError):
        raise RuntimeError("Gmail career message body is malformed") from None


__all__ = [
    "GmailCareerMailboxReader",
    "TelegramCorrespondenceOutboxDispatcher",
    "TelegramDeliveryError",
    "TelegramMessageTransport",
]
