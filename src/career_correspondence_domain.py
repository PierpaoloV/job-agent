"""Typed records for dedicated career-mail monitoring and local drafting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import ipaddress
import re
from typing import Any, Mapping

from publicsuffixlist import PublicSuffixList

from application_domain import CorrespondenceClassification, CorrespondenceEvent


_PUBLIC_SUFFIX_LIST = PublicSuffixList()


@dataclass(frozen=True)
class TrustedDomain:
    value: str

    def __post_init__(self) -> None:
        normalized = _normalized_hostname(self.value)
        if (
            normalized is None
            or "." not in normalized
            or _PUBLIC_SUFFIX_LIST.privatesuffix(normalized) is None
        ):
            raise ValueError("Trusted domain must identify a registrable domain")
        object.__setattr__(self, "value", normalized)

    @classmethod
    def try_from(cls, value: object) -> "TrustedDomain | None":
        try:
            return cls(str(value))
        except (TypeError, ValueError):
            return None

    def matches(self, candidate: object) -> bool:
        normalized = _normalized_hostname(candidate)
        return normalized == self.value or (
            normalized is not None and normalized.endswith("." + self.value)
        )


def _normalized_hostname(value: object) -> str | None:
    candidate = str(value).strip().rstrip(".")
    if not candidate:
        return None
    try:
        candidate = candidate.encode("idna").decode("ascii").casefold()
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    except UnicodeError:
        return None
    else:
        return None
    if len(candidate) > 253:
        return None
    labels = candidate.split(".")
    if any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        return None
    return candidate


class SenderKind(str, Enum):
    UNKNOWN = "unknown"
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    REFERRAL = "referral"


class MailboxPollStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    COMPLETED = "completed"


class MessageClaimStatus(str, Enum):
    CLAIMED = "claimed"
    BUSY = "busy"
    COMPLETED = "completed"


class TelegramOutboxKind(str, Enum):
    CLASSIFICATION = "classification"
    DRAFT_REVIEW = "draft_review"


@dataclass(frozen=True)
class CareerMessageClaim:
    status: MessageClaimStatus
    token: str | None = None
    plan: "CareerProcessingPlan | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", MessageClaimStatus(self.status))
        if self.status == MessageClaimStatus.CLAIMED and not self.token:
            raise ValueError("A claimed career message requires a claim token")
        if self.status != MessageClaimStatus.CLAIMED and self.token is not None:
            raise ValueError("Only a claimed career message may carry a token")
        if self.status != MessageClaimStatus.CLAIMED and self.plan is not None:
            raise ValueError("Only a claimed career message may carry a recovery plan")


@dataclass(frozen=True)
class TelegramOutboxItem:
    delivery_id: str
    kind: TelegramOutboxKind
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", TelegramOutboxKind(self.kind))


@dataclass(frozen=True)
class TelegramOutboxClaim:
    item: TelegramOutboxItem | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        if (self.item is None) != (self.token is None):
            raise ValueError("Telegram outbox claim scope is incomplete")


@dataclass(frozen=True)
class TelegramDispatchResult:
    delivered: int = 0
    uncertain: int = 0
    retryable: int = 0
    failed: int = 0


@dataclass(frozen=True)
class CareerMailboxConnection:
    address: str
    connected_at: str

    def __post_init__(self) -> None:
        normalized = self.address.strip().casefold()
        if (
            normalized.count("@") != 1
            or not normalized.endswith("@gmail.com")
            or any(character.isspace() for character in normalized)
        ):
            raise ValueError("A dedicated career Gmail must be configured")
        object.__setattr__(self, "address", normalized)
        _require_timestamp(self.connected_at, "Mailbox connection timestamp")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CareerMailboxConnection":
        return cls(
            address=str(value["address"]), connected_at=str(value["connected_at"])
        )

    def to_dict(self) -> dict[str, str]:
        return {"address": self.address, "connected_at": self.connected_at}


@dataclass(frozen=True)
class CareerMessage:
    message_id: str
    thread_id: str
    sender_address: str
    sender_name: str
    sender_kind: SenderKind
    authenticated_sender: bool
    subject: str
    body_text: str
    received_at: str
    authenticated_domain: str | None = None

    def __post_init__(self) -> None:
        if not self.message_id.strip() or not self.thread_id.strip():
            raise ValueError("Gmail message and thread identifiers are required")
        if not self.sender_address.strip():
            raise ValueError("Career-message sender is required")
        object.__setattr__(self, "sender_kind", SenderKind(self.sender_kind))
        if self.authenticated_domain is not None:
            domain = self.authenticated_domain.strip().casefold().rstrip(".")
            object.__setattr__(self, "authenticated_domain", domain or None)
        _require_timestamp(self.received_at, "Career-message received timestamp")


@dataclass(frozen=True)
class TelegramClassificationRequest:
    request_id: str
    message_id: str
    application_id: str | None
    reason: str
    summary: str
    created_at: str
    delivery_channel: str = "telegram"
    status: str = "pending"

    def __post_init__(self) -> None:
        _require_timestamp(self.created_at, "Classification request timestamp")
        if self.delivery_channel != "telegram" or self.status != "pending":
            raise ValueError("Classification requests must enter the Telegram outbox")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TelegramClassificationRequest":
        return cls(
            request_id=str(value["request_id"]),
            message_id=str(value["message_id"]),
            application_id=(
                None
                if value.get("application_id") is None
                else str(value["application_id"])
            ),
            reason=str(value["reason"]),
            summary=str(value["summary"]),
            created_at=str(value["created_at"]),
            delivery_channel=str(value.get("delivery_channel", "telegram")),
            status=str(value.get("status", "pending")),
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "request_id": self.request_id,
            "message_id": self.message_id,
            "application_id": self.application_id,
            "reason": self.reason,
            "summary": self.summary,
            "created_at": self.created_at,
            "delivery_channel": self.delivery_channel,
            "status": self.status,
        }


@dataclass(frozen=True)
class TelegramDraftReviewRequest:
    request_id: str
    draft_id: str
    message_id: str
    application_id: str
    summary: str
    created_at: str
    delivery_channel: str = "telegram"
    status: str = "pending"

    def __post_init__(self) -> None:
        _require_timestamp(self.created_at, "Draft-review request timestamp")
        if self.delivery_channel != "telegram" or self.status != "pending":
            raise ValueError("Draft reviews must enter the Telegram outbox")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TelegramDraftReviewRequest":
        return cls(
            request_id=str(value["request_id"]),
            draft_id=str(value["draft_id"]),
            message_id=str(value["message_id"]),
            application_id=str(value["application_id"]),
            summary=str(value["summary"]),
            created_at=str(value["created_at"]),
            delivery_channel=str(value.get("delivery_channel", "telegram")),
            status=str(value.get("status", "pending")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "draft_id": self.draft_id,
            "message_id": self.message_id,
            "application_id": self.application_id,
            "summary": self.summary,
            "created_at": self.created_at,
            "delivery_channel": self.delivery_channel,
            "status": self.status,
        }


@dataclass(frozen=True)
class CareerDraft:
    draft_id: str
    message_id: str
    application_id: str
    kind: str
    summary: str
    subject: str
    body: str
    created_at: str
    status: str = "local_draft"

    def __post_init__(self) -> None:
        _require_timestamp(self.created_at, "Career-draft timestamp")
        if self.status != "local_draft":
            raise ValueError("Career correspondence may only create local drafts")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CareerDraft":
        return cls(
            draft_id=str(value["draft_id"]),
            message_id=str(value["message_id"]),
            application_id=str(value["application_id"]),
            kind=str(value["kind"]),
            summary=str(value["summary"]),
            subject=str(value["subject"]),
            body=str(value["body"]),
            created_at=str(value["created_at"]),
            status=str(value.get("status", "local_draft")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "draft_id": self.draft_id,
            "message_id": self.message_id,
            "application_id": self.application_id,
            "kind": self.kind,
            "summary": self.summary,
            "subject": self.subject,
            "body": self.body,
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class CareerProcessingPlan:
    application_id: str | None
    classification: CorrespondenceClassification
    event: CorrespondenceEvent | None = None
    request: TelegramClassificationRequest | None = None
    draft: CareerDraft | None = None
    draft_review: TelegramDraftReviewRequest | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "classification",
            CorrespondenceClassification(self.classification),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CareerProcessingPlan":
        return cls(
            application_id=(
                None
                if value.get("application_id") is None
                else str(value["application_id"])
            ),
            classification=CorrespondenceClassification(str(value["classification"])),
            event=(
                None
                if value.get("event") is None
                else CorrespondenceEvent.from_dict(value["event"])
            ),
            request=(
                None
                if value.get("request") is None
                else TelegramClassificationRequest.from_dict(value["request"])
            ),
            draft=(
                None
                if value.get("draft") is None
                else CareerDraft.from_dict(value["draft"])
            ),
            draft_review=(
                None
                if value.get("draft_review") is None
                else TelegramDraftReviewRequest.from_dict(value["draft_review"])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class MailboxPollResult:
    status: MailboxPollStatus
    processed: int = 0
    already_processed: int = 0
    ambiguous: int = 0
    drafted: int = 0
    failed: int = 0


def _require_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "CareerDraft",
    "CareerMailboxConnection",
    "CareerMessage",
    "CareerMessageClaim",
    "CareerProcessingPlan",
    "MailboxPollResult",
    "MailboxPollStatus",
    "MessageClaimStatus",
    "SenderKind",
    "TelegramClassificationRequest",
    "TelegramDispatchResult",
    "TelegramDraftReviewRequest",
    "TelegramOutboxClaim",
    "TelegramOutboxItem",
    "TelegramOutboxKind",
    "TrustedDomain",
]
