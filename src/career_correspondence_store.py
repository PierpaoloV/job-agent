"""Owner-only durable state and outboxes for career correspondence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import secrets
from typing import Any, Callable

from career_correspondence_domain import (
    CareerDraft,
    CareerMailboxConnection,
    CareerMessageClaim,
    CareerProcessingPlan,
    MessageClaimStatus,
    TelegramClassificationRequest,
    TelegramOutboxClaim,
    TelegramOutboxItem,
    TelegramOutboxKind,
    TelegramDraftReviewRequest,
)


class JsonCareerCorrespondenceStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._state_path = self._root / "state.json"
        self._lock_path = self._root / ".state.lock"

    def connection(self) -> CareerMailboxConnection | None:
        value = self._read()["connection"]
        return None if value is None else CareerMailboxConnection.from_dict(value)

    def connect(
        self, connection: CareerMailboxConnection
    ) -> CareerMailboxConnection:
        def update(state: dict[str, Any]) -> None:
            state["connection"] = connection.to_dict()

        self._mutate(update)
        return connection

    def claim_message(
        self,
        message_id: str,
        *,
        claimed_at: str,
        lease: timedelta = timedelta(minutes=10),
    ) -> CareerMessageClaim:
        if lease <= timedelta(0):
            raise ValueError("Career-message claim lease must be positive")
        claim_time = _aware_datetime(claimed_at, "Career-message claim timestamp")
        claim_token = secrets.token_urlsafe(24)
        claim_status = MessageClaimStatus.CLAIMED
        recovery_plan = None

        def update(state: dict[str, Any]) -> None:
            nonlocal claim_status, recovery_plan
            messages = state["messages"]
            current = messages.get(message_id)
            if current is not None and current.get("status") == "completed":
                claim_status = MessageClaimStatus.COMPLETED
                return
            now = claim_time
            if current is not None and current.get("status") == "processing":
                expires_at = datetime.fromisoformat(str(current["lease_expires_at"]))
                if now < expires_at:
                    claim_status = MessageClaimStatus.BUSY
                    return
            if current is not None and current.get("plan") is not None:
                recovery_plan = CareerProcessingPlan.from_dict(current["plan"])
            messages[message_id] = {
                "status": "processing",
                "claim_token": claim_token,
                "claimed_at": claimed_at,
                "lease_expires_at": (now + lease).isoformat(),
            }
            if recovery_plan is not None:
                messages[message_id]["plan"] = recovery_plan.to_dict()

        self._mutate(update)
        return CareerMessageClaim(
            claim_status,
            claim_token if claim_status == MessageClaimStatus.CLAIMED else None,
            recovery_plan if claim_status == MessageClaimStatus.CLAIMED else None,
        )

    def stage_message_plan(
        self,
        message_id: str,
        *,
        claim_token: str,
        plan: CareerProcessingPlan,
    ) -> None:
        def update(state: dict[str, Any]) -> None:
            current = state["messages"].get(message_id)
            if (
                current is None
                or current.get("status") != "processing"
                or current.get("claim_token") != claim_token
            ):
                raise RuntimeError("Career-message claim is no longer current")
            current["plan"] = plan.to_dict()

        self._mutate(update)

    def complete_message(
        self,
        message_id: str,
        *,
        claim_token: str,
        completed_at: str,
        application_id: str | None,
        classification: str,
        request: TelegramClassificationRequest | None = None,
        draft: CareerDraft | None = None,
        draft_review: TelegramDraftReviewRequest | None = None,
    ) -> None:
        _aware_datetime(completed_at, "Career-message completion timestamp")

        def update(state: dict[str, Any]) -> None:
            current = state["messages"].get(message_id)
            if current is not None and current.get("status") == "completed":
                return
            if (
                current is None
                or current.get("status") != "processing"
                or current.get("claim_token") != claim_token
            ):
                raise RuntimeError("Career-message claim is no longer current")
            state["messages"][message_id] = {
                "status": "completed",
                "completed_at": completed_at,
                "application_id": application_id,
                "classification": classification,
            }
            if request is not None:
                state["classification_requests"][request.request_id] = (
                    request.to_dict()
                )
                state["telegram_outbox"].setdefault(
                    request.request_id,
                    {
                        "status": "pending",
                        "kind": TelegramOutboxKind.CLASSIFICATION.value,
                        "reason": request.reason,
                    },
                )
            if draft is not None:
                state["drafts"][draft.draft_id] = draft.to_dict()
            if draft_review is not None:
                state["draft_review_requests"][draft_review.request_id] = (
                    draft_review.to_dict()
                )
                state["telegram_outbox"].setdefault(
                    draft_review.request_id,
                    {
                        "status": "pending",
                        "kind": TelegramOutboxKind.DRAFT_REVIEW.value,
                        "reason": None,
                    },
                )

        self._mutate(update)

    def fail_message(
        self, message_id: str, *, claim_token: str, failed_at: str
    ) -> None:
        _aware_datetime(failed_at, "Career-message failure timestamp")

        def update(state: dict[str, Any]) -> None:
            current = state["messages"].get(message_id)
            if current is not None and current.get("status") == "completed":
                return
            if current is None or current.get("claim_token") != claim_token:
                return
            failed = {
                "status": "retryable_failure",
                "failed_at": failed_at,
            }
            if current.get("plan") is not None:
                failed["plan"] = current["plan"]
            state["messages"][message_id] = failed

        self._mutate(update)

    def classification_requests(
        self,
    ) -> tuple[TelegramClassificationRequest, ...]:
        values = self._read()["classification_requests"]
        return tuple(
            TelegramClassificationRequest.from_dict(values[key])
            for key in sorted(values)
        )

    def drafts(self) -> tuple[CareerDraft, ...]:
        values = self._read()["drafts"]
        return tuple(CareerDraft.from_dict(values[key]) for key in sorted(values))

    def draft_review_requests(self) -> tuple[TelegramDraftReviewRequest, ...]:
        values = self._read()["draft_review_requests"]
        return tuple(
            TelegramDraftReviewRequest.from_dict(values[key])
            for key in sorted(values)
        )

    def claim_next_telegram_delivery(
        self,
        *,
        claimed_at: str,
        lease: timedelta = timedelta(minutes=10),
    ) -> TelegramOutboxClaim:
        if lease <= timedelta(0):
            raise ValueError("Telegram delivery claim lease must be positive")
        now = _aware_datetime(claimed_at, "Telegram delivery claim timestamp")
        token = secrets.token_urlsafe(24)
        claimed_item = None

        def update(state: dict[str, Any]) -> None:
            nonlocal claimed_item
            for delivery_id in sorted(state["telegram_outbox"]):
                current = state["telegram_outbox"][delivery_id]
                if current.get("status") == "processing":
                    expires_at = datetime.fromisoformat(
                        str(current["lease_expires_at"])
                    )
                    if now >= expires_at:
                        current["status"] = "uncertain"
                        current["uncertain_at"] = claimed_at
                    continue
                if current.get("status") != "pending":
                    continue
                current.update(
                    {
                        "status": "processing",
                        "claim_token": token,
                        "claimed_at": claimed_at,
                        "lease_expires_at": (now + lease).isoformat(),
                    }
                )
                claimed_item = TelegramOutboxItem(
                    delivery_id=delivery_id,
                    kind=TelegramOutboxKind(str(current["kind"])),
                    reason=(
                        None
                        if current.get("reason") is None
                        else str(current["reason"])
                    ),
                )
                return

        self._mutate(update)
        return TelegramOutboxClaim(
            item=claimed_item,
            token=token if claimed_item is not None else None,
        )

    def complete_telegram_delivery(
        self, delivery_id: str, *, claim_token: str, delivered_at: str
    ) -> None:
        _aware_datetime(delivered_at, "Telegram delivery timestamp")

        def update(state: dict[str, Any]) -> None:
            current = state["telegram_outbox"].get(delivery_id)
            if (
                current is None
                or current.get("status") != "processing"
                or current.get("claim_token") != claim_token
            ):
                raise RuntimeError("Telegram delivery claim is no longer current")
            state["telegram_outbox"][delivery_id] = {
                "status": "delivered",
                "kind": current["kind"],
                "reason": current.get("reason"),
                "delivered_at": delivered_at,
            }

        self._mutate(update)

    def fail_telegram_delivery(
        self, delivery_id: str, *, claim_token: str, uncertain_at: str
    ) -> None:
        _aware_datetime(uncertain_at, "Telegram uncertainty timestamp")

        def update(state: dict[str, Any]) -> None:
            current = state["telegram_outbox"].get(delivery_id)
            if current is None or current.get("claim_token") != claim_token:
                return
            state["telegram_outbox"][delivery_id] = {
                "status": "uncertain",
                "kind": current["kind"],
                "reason": current.get("reason"),
                "uncertain_at": uncertain_at,
            }

        self._mutate(update)

    def retry_or_fail_telegram_delivery(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        failed_at: str,
        max_attempts: int = 3,
    ) -> str:
        _aware_datetime(failed_at, "Telegram failure timestamp")
        if max_attempts < 1:
            raise ValueError("Telegram maximum attempts must be positive")
        resulting_status = "failed"

        def update(state: dict[str, Any]) -> None:
            nonlocal resulting_status
            current = state["telegram_outbox"].get(delivery_id)
            if (
                current is None
                or current.get("status") != "processing"
                or current.get("claim_token") != claim_token
            ):
                raise RuntimeError("Telegram delivery claim is no longer current")
            attempts = int(current.get("attempts", 0)) + 1
            resulting_status = "pending" if attempts < max_attempts else "failed"
            state["telegram_outbox"][delivery_id] = {
                "status": resulting_status,
                "kind": current["kind"],
                "reason": current.get("reason"),
                "attempts": attempts,
                "last_failed_at": failed_at,
            }

        self._mutate(update)
        return resulting_status

    def requeue_telegram_delivery(
        self, delivery_id: str, *, requeued_at: str
    ) -> None:
        _aware_datetime(requeued_at, "Telegram requeue timestamp")

        def update(state: dict[str, Any]) -> None:
            current = state["telegram_outbox"].get(delivery_id)
            if current is None or current.get("status") != "failed":
                raise RuntimeError(
                    "Only a definitively failed Telegram delivery may be requeued"
                )
            state["telegram_outbox"][delivery_id] = {
                "status": "pending",
                "kind": current["kind"],
                "reason": current.get("reason"),
                "attempts": 0,
                "requeued_at": requeued_at,
            }

        self._mutate(update)

    def reconcile_telegram_delivery(
        self,
        delivery_id: str,
        *,
        delivered: bool,
        reconciled_at: str,
    ) -> None:
        _aware_datetime(reconciled_at, "Telegram reconciliation timestamp")

        def update(state: dict[str, Any]) -> None:
            current = state["telegram_outbox"].get(delivery_id)
            if current is None or current.get("status") != "uncertain":
                raise RuntimeError("Telegram delivery is not awaiting reconciliation")
            state["telegram_outbox"][delivery_id] = {
                "status": "delivered" if delivered else "pending",
                "kind": current["kind"],
                "reason": current.get("reason"),
                "attempts": 0,
                "reconciled_at": reconciled_at,
            }

        self._mutate(update)

    def telegram_delivery_status(self, delivery_id: str) -> str | None:
        current = self._read()["telegram_outbox"].get(delivery_id)
        return None if current is None else str(current.get("status"))

    def _mutate(self, operation: Callable[[dict[str, Any]], None]) -> None:
        self._prepare_root()
        with self._lock():
            state = self._read_unlocked()
            operation(state)
            self._write_unlocked(state)

    def _read(self) -> dict[str, Any]:
        self._prepare_root()
        with self._lock(shared=True):
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {
                "connection": None,
                "messages": {},
                "classification_requests": {},
                "drafts": {},
                "draft_review_requests": {},
                "telegram_outbox": {},
            }
        value = json.loads(self._state_path.read_text(encoding="utf-8"))
        state = {
            "connection": value.get("connection"),
            "messages": dict(value.get("messages", {})),
            "classification_requests": dict(
                value.get("classification_requests", {})
            ),
            "drafts": dict(value.get("drafts", {})),
            "draft_review_requests": dict(
                value.get("draft_review_requests", {})
            ),
            "telegram_outbox": dict(value.get("telegram_outbox", {})),
        }
        for request_id, request in state["classification_requests"].items():
            state["telegram_outbox"].setdefault(
                request_id,
                {
                    "status": "pending",
                    "kind": TelegramOutboxKind.CLASSIFICATION.value,
                    "reason": request.get("reason"),
                },
            )
        for request_id in state["draft_review_requests"]:
            state["telegram_outbox"].setdefault(
                request_id,
                {
                    "status": "pending",
                    "kind": TelegramOutboxKind.DRAFT_REVIEW.value,
                    "reason": None,
                },
            )
        return state

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        temporary = self._state_path.with_suffix(".json.tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._state_path)
        os.chmod(self._state_path, 0o600)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _prepare_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)

    @contextmanager
    def _lock(self, *, shared: bool = False):
        descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(self._lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


__all__ = ["JsonCareerCorrespondenceStore"]
