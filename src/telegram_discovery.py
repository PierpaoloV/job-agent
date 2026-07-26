"""Production Telegram transport and Mac-side discovery callback consumer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests

from discovery_schedule import (
    BatchNotYetPublished,
    DiscoverySchedule,
    DiscoveryTelegramHandler,
    FileDiscoveryScheduleStore,
)
from actions_state import restore_latest
from telegram_delivery import TelegramDeliveryLedger, TelegramScheduledNotifier
from workflow import SystemClock


class TelegramBotApi:
    """Small Bot API adapter used only by the local Mac callback worker."""

    def __init__(
        self,
        token: str | None = None,
        overflow_notifier: TelegramScheduledNotifier | None = None,
    ) -> None:
        self._token = token or os.environ["TELEGRAM_BOT_TOKEN"]
        self._base = f"https://api.telegram.org/bot{self._token}"
        self._overflow_notifier = overflow_notifier or TelegramScheduledNotifier()

    def poll_updates(
        self, *, offset: int | None, timeout: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        response = requests.get(
            f"{self._base}/getUpdates", params=params, timeout=timeout + 5
        )
        if not response.ok:
            raise RuntimeError("Telegram update polling failed safely")
        value = response.json()
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise RuntimeError("Telegram update polling failed safely")
        return [
            dict(item)
            for item in value.get("result", [])
            if isinstance(item, dict)
        ]

    def send_overflow(
        self, jobs: Sequence[dict[str, Any]], *, batch_id: str
    ) -> None:
        self._overflow_notifier.send_digest(
            [dict(job) for job in jobs],
            overflow_count=0,
            batch_id=batch_id,
            idempotency_key=f"overflow:{batch_id}",
        )

    def acknowledge_callback(self, callback_query_id: str, text: str) -> None:
        response = requests.post(
            f"{self._base}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError("Telegram callback acknowledgement failed safely")


class TelegramUpdateConsumer:
    """Route persisted batch callbacks without running scheduling or mutation."""

    def __init__(
        self,
        *,
        schedule: DiscoverySchedule,
        api: TelegramBotApi,
        ledger: TelegramDeliveryLedger,
        state_sync: Callable[[], None] | None = None,
    ) -> None:
        self._api = api
        self._ledger = ledger
        self._handler = DiscoveryTelegramHandler(schedule, api)
        self._state_sync = state_sync or (lambda: None)
        self._offset: int | None = None

    def consume_once(self, *, timeout: int = 25) -> int:
        updates = self._api.poll_updates(offset=self._offset, timeout=timeout)
        handled = 0
        for update in updates:
            update_id = int(update.get("update_id", -1))
            if update_id >= 0:
                self._offset = max(self._offset or 0, update_id + 1)
            callback = update.get("callback_query")
            if not isinstance(callback, Mapping):
                continue
            self._state_sync()
            update_key = f"telegram-update:{update_id}"
            status = self._ledger.begin_update(update_key)
            if status == "completed":
                self._api.acknowledge_callback(
                    str(callback.get("id", "")), "Azione già completata"
                )
                continue
            if status == "uncertain":
                self._api.acknowledge_callback(
                    str(callback.get("id", "")), "Esito incerto: riprova il pulsante"
                )
                continue
            if status == "pending":
                self._ledger.mark_update(update_key, "uncertain")
                self._api.acknowledge_callback(
                    str(callback.get("id", "")), "Esito incerto: riprova il pulsante"
                )
                continue
            callback_id = str(callback.get("id", ""))
            data = str(callback.get("data", ""))
            try:
                jobs = self._handler.handle_callback_data(data)
            except BatchNotYetPublished:
                self._ledger.mark_update(update_key, "waiting")
                self._api.acknowledge_callback(
                    callback_id, "Stato in pubblicazione: riprova il pulsante"
                )
                continue
            except ValueError:
                self._ledger.mark_update(update_key, "completed")
                self._api.acknowledge_callback(
                    callback_id, "Azione non valida o scaduta"
                )
                continue
            except Exception:
                self._ledger.mark_update(update_key, "uncertain")
                try:
                    self._api.acknowledge_callback(
                        callback_id, "Esito incerto: riprova il pulsante"
                    )
                except Exception:
                    pass
                raise
            self._ledger.mark_update(update_key, "completed")
            self._api.acknowledge_callback(
                callback_id, f"{len(jobs)} opportunità mostrate"
            )
            handled += 1
        return handled


class _NoopNotifier:
    def send_digest(self, jobs, **kwargs) -> None:
        raise RuntimeError("The callback worker cannot dispatch scheduled digests")

    def send_alert(self, job, **kwargs) -> None:
        raise RuntimeError("The callback worker cannot dispatch scheduled alerts")


def build_consumer(
    *,
    state_path: Path = Path("data/discovery-schedule.json"),
    ledger_path: Path = Path("data/telegram-callbacks.sqlite"),
    repository: str | None = None,
    branch: str | None = None,
    github_token: str | None = None,
) -> TelegramUpdateConsumer:
    resolved_repository = (
        repository or os.environ.get("JOB_AGENT_GITHUB_REPOSITORY", "")
    ).strip()
    if not resolved_repository:
        raise RuntimeError(
            "JOB_AGENT_GITHUB_REPOSITORY is required for callback state sync"
        )
    resolved_branch = (
        branch or os.environ.get("JOB_AGENT_GITHUB_BRANCH", "")
    ).strip()
    if not resolved_branch:
        raise RuntimeError(
            "JOB_AGENT_GITHUB_BRANCH is required for callback state sync"
        )
    ledger = TelegramDeliveryLedger(ledger_path)
    token = github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for callback state sync")

    def sync_state() -> None:
        if not restore_latest(
            root=Path("."),
            repository=resolved_repository,
            token=token,
            branch=resolved_branch,
        ):
            raise RuntimeError("No authoritative Actions state is available")

    sync_state()
    schedule = DiscoverySchedule(
        store=FileDiscoveryScheduleStore(state_path),
        notifier=_NoopNotifier(),
        clock=SystemClock(),
    )
    return TelegramUpdateConsumer(
        schedule=schedule,
        api=TelegramBotApi(
            overflow_notifier=TelegramScheduledNotifier(ledger=ledger)
        ),
        ledger=ledger,
        state_sync=sync_state,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("consume-updates", choices=("consume-updates",))
    parser.add_argument(
        "--state", type=Path, default=Path("data/discovery-schedule.json")
    )
    parser.add_argument(
        "--ledger", type=Path, default=Path("data/telegram-callbacks.sqlite")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    consumer = build_consumer(state_path=args.state, ledger_path=args.ledger)
    if args.once:
        consumer.consume_once()
        return 0
    while True:
        consumer.consume_once()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "TelegramBotApi",
    "TelegramUpdateConsumer",
    "build_consumer",
    "main",
]
