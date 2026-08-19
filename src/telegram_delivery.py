"""Transactional Telegram delivery claims and resumable outbound messages."""

from __future__ import annotations

from pathlib import Path
import secrets
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from discovery_schedule import DiscoveryTelegramHandler
from notify_telegram import (
    TelegramMessage,
    TelegramSendRejected,
    digest_messages,
    send_message,
)


class TelegramDeliveryLedger:
    VERSION = "job-agent.telegram-delivery.v1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def claim(self, key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO claims(key, status) VALUES (?, 'claimed')",
                (key,),
            )
            return cursor.rowcount == 1

    def begin_update(self, key: str) -> str:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO claims(key, status) VALUES (?, 'pending')",
                (key,),
            )
            if cursor.rowcount == 1:
                return "new"
            row = connection.execute(
                "SELECT status FROM claims WHERE key = ?", (key,)
            ).fetchone()
            if row[0] == "waiting":
                connection.execute(
                    "UPDATE claims SET status = 'pending' WHERE key = ?", (key,)
                )
                return "new"
            return str(row[0])

    def mark_update(self, key: str, status: str) -> None:
        if status not in {"completed", "uncertain", "waiting"}:
            raise ValueError("Unsupported Telegram update status")
        with self._connect() as connection:
            connection.execute(
                "UPDATE claims SET status = ? WHERE key = ?", (status, key)
            )

    def stage_outbound(self, key: str) -> str:
        """Persist one outbound intent without changing an existing outcome."""

        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO claims(key, status) VALUES (?, 'pending')",
                (key,),
            )
            row = connection.execute(
                "SELECT status FROM claims WHERE key = ?", (key,)
            ).fetchone()
            assert row is not None
            return str(row[0])

    def claim_outbound(self, key: str) -> str | None:
        """Claim a staged or abandoned pre-send delivery.

        The local worker is the singleton owner of this ledger.  ``claimed``
        therefore means that no external request has started yet and may be
        recovered after the previous worker process stopped.  Once a delivery
        enters ``sending`` it is never reclaimed automatically.
        """

        token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE claims SET status = 'claimed', claim_token = ? "
                "WHERE key = ? AND status IN ('pending', 'claimed')",
                (token, key),
            )
            return token if cursor.rowcount == 1 else None

    def release_outbound(self, key: str, claim_token: str) -> bool:
        """Release a claim only when no external send boundary was crossed."""

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE claims SET status = 'pending', claim_token = NULL "
                "WHERE key = ? AND status = 'claimed' AND claim_token = ?",
                (key, claim_token),
            )
            return cursor.rowcount == 1

    def mark_outbound_sending(self, key: str, claim_token: str) -> bool:
        """Persist crossing the non-repeatable external-send boundary."""

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE claims SET status = 'sending' "
                "WHERE key = ? AND status = 'claimed' AND claim_token = ?",
                (key, claim_token),
            )
            return cursor.rowcount == 1

    def mark_outbound_sent(self, key: str, claim_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE claims SET status = 'sent', claim_token = NULL "
                "WHERE key = ? AND status = 'sending' AND claim_token = ?",
                (key, claim_token),
            )
            return cursor.rowcount == 1

    def mark_outbound_uncertain(self, key: str, claim_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE claims SET status = 'uncertain', claim_token = NULL "
                "WHERE key = ? AND status = 'sending' AND claim_token = ?",
                (key, claim_token),
            )
            return cursor.rowcount == 1

    def requeue_outbound_rejected(self, key: str, claim_token: str) -> bool:
        """Retry a request that Telegram definitively rejected."""

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE claims SET status = 'pending', claim_token = NULL "
                "WHERE key = ? AND status = 'sending' AND claim_token = ?",
                (key, claim_token),
            )
            return cursor.rowcount == 1

    def outbound_status(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM claims WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row[0])

    def outbound_claim_is_sending(self, key: str, claim_token: str) -> bool:
        """Verify exact ownership before crossing the Telegram boundary."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM claims "
                "WHERE key = ? AND status = 'sending' AND claim_token = ?",
                (key, claim_token),
            ).fetchone()
            return row is not None

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS claims("
                "key TEXT PRIMARY KEY, status TEXT NOT NULL, claim_token TEXT)"
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(claims)")
            }
            if "claim_token" not in columns:
                connection.execute(
                    "ALTER TABLE claims ADD COLUMN claim_token TEXT"
                )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('version', ?)",
                (self.VERSION,),
            )
            value = connection.execute(
                "SELECT value FROM metadata WHERE key = 'version'"
            ).fetchone()
            if value is None or value[0] != self.VERSION:
                raise ValueError("Unsupported Telegram delivery ledger version")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level="IMMEDIATE")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


class TelegramScheduledNotifier:
    def __init__(
        self,
        ledger: TelegramDeliveryLedger | None = None,
        message_sender: Callable[[TelegramMessage], None] = send_message,
        role_button_factory: (
            Callable[[Mapping[str, Any]], Sequence[Mapping[str, str]]] | None
        ) = None,
    ) -> None:
        self._ledger = ledger or TelegramDeliveryLedger(
            Path("data/telegram-deliveries.sqlite")
        )
        self._message_sender = message_sender
        self._role_button_factory = role_button_factory

    def send_digest(
        self,
        jobs: Sequence[dict[str, Any]],
        *,
        overflow_count: int,
        batch_id: str,
        idempotency_key: str,
        before_send: Callable[[], Any] | None = None,
    ) -> None:
        messages = digest_messages(
            [dict(job) for job in jobs],
            overflow_count=overflow_count,
            overflow_callback_data=(
                DiscoveryTelegramHandler.callback_data(batch_id)
                if overflow_count
                else None
            ),
            role_button_factory=self._role_button_factory,
        )
        self._send_messages(
            messages, idempotency_key, before_send=before_send
        )

    def send_alert(
        self,
        job: Mapping[str, Any],
        *,
        reason: str,
        idempotency_key: str,
        before_send: Callable[[], Any] | None = None,
    ) -> None:
        label = {
            "top_tier": "Top-tier",
            "imminent_deadline": "Scadenza imminente",
            "refreshed": "Scheda aggiornata",
        }.get(reason, str(reason))
        rendered = digest_messages(
            [{**job, "rationale": f"Alert immediato: {label}"}],
            role_button_factory=self._role_button_factory,
        )
        if len(rendered) != 3:
            raise RuntimeError("A single-role alert did not render one role card")
        messages = (rendered[1],)
        self._send_messages(
            messages, idempotency_key, before_send=before_send
        )

    def _send_messages(
        self,
        messages: Sequence[TelegramMessage],
        event_key: str,
        *,
        before_send: Callable[[], Any] | None = None,
    ) -> None:
        for index, message in enumerate(messages):
            message_key = f"{event_key}:message:{index}"
            self._ledger.stage_outbound(message_key)
            claim_token = self._ledger.claim_outbound(message_key)
            if claim_token is None:
                continue
            if not self._ledger.mark_outbound_sending(
                message_key, claim_token
            ):
                raise RuntimeError(
                    "Telegram delivery claim could not enter sending state"
                )
            try:
                if before_send is not None:
                    before_send()
                self._message_sender(message)
            except TelegramSendRejected:
                self._ledger.requeue_outbound_rejected(
                    message_key, claim_token
                )
                raise
            except Exception:
                self._ledger.mark_outbound_uncertain(
                    message_key, claim_token
                )
                raise
            if not self._ledger.mark_outbound_sent(
                message_key, claim_token
            ):
                raise RuntimeError(
                    "Telegram delivery acknowledgement could not be persisted"
                )


__all__ = ["TelegramDeliveryLedger", "TelegramScheduledNotifier"]
