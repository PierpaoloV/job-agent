"""Production composition for approved company monitoring and job alerts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from notify_telegram import TelegramMessage
from watchlist_adapters import (
    BrowserJobAlertDriver,
    DeliveryDefinitiveError,
    WatchlistDeliveryOutbox,
    WatchlistTelegramNotifier,
    browser_subscription_registry,
)
from watchlist_domain import JobAlertCandidate
from watchlist_service import (
    Clock,
    CompanyEligibilityPolicy,
    SubscriptionDefinitiveError,
    WatchlistService,
)
from watchlist_store import ImportedSeed, JsonWatchlistStore, TargetedCompanySeed
from watchlist_telegram import WatchlistTelegramHandler


class _UnconfiguredSubscriptionExecutor:
    def subscribe(
        self, alert: JobAlertCandidate, *, idempotency_key: str
    ) -> dict:
        raise SubscriptionDefinitiveError(
            "Job-alert browser integration is not configured"
        )


def _unconfigured_telegram_sender(message: TelegramMessage) -> None:
    raise DeliveryDefinitiveError("Watchlist Telegram delivery is not configured")


@dataclass(frozen=True)
class WatchlistCallbackRegistration:
    handler: WatchlistTelegramHandler
    prefixes: tuple[str, ...] = ("wc:", "wa:")

    def handles(self, callback: str) -> bool:
        return callback.startswith(self.prefixes)

    def handle(self, callback: str, *, actor: str, chat_id: str):
        if not self.handles(callback):
            raise ValueError("Callback is not registered for the watchlist runtime")
        return self.handler.handle_callback(callback, actor=actor, chat_id=chat_id)


@dataclass(frozen=True)
class WatchlistRuntime:
    store: JsonWatchlistStore
    service: WatchlistService
    handler: WatchlistTelegramHandler
    notifier: WatchlistTelegramNotifier
    callback_registration: WatchlistCallbackRegistration
    imported_seed: ImportedSeed


def build_watchlist_runtime(
    *,
    repository_root: Path,
    clock: Clock,
    browser_driver: BrowserJobAlertDriver | None = None,
    telegram_sender: Callable[[TelegramMessage], None] | None = None,
    eligibility_policy: CompanyEligibilityPolicy = CompanyEligibilityPolicy(),
) -> WatchlistRuntime:
    """Wire durable local state while leaving external capabilities explicit."""

    root = Path(repository_root)
    data_root = root / "data" / "private" / "watchlist"
    store = JsonWatchlistStore(data_root / "state.json")
    imported_seed = store.import_seed(
        TargetedCompanySeed.read(root / "watchlist" / "targeted-companies.md")
    )
    executor = (
        _UnconfiguredSubscriptionExecutor()
        if browser_driver is None
        else browser_subscription_registry(browser_driver)
    )
    service = WatchlistService(
        store=store,
        clock=clock,
        subscription_executor=executor,
        eligibility_policy=eligibility_policy,
    )
    handler = WatchlistTelegramHandler(service)
    notifier = WatchlistTelegramNotifier(
        handler=handler,
        outbox=WatchlistDeliveryOutbox(data_root / "telegram-outbox.sqlite"),
        message_sender=(
            _unconfigured_telegram_sender
            if telegram_sender is None
            else telegram_sender
        ),
    )
    return WatchlistRuntime(
        store=store,
        service=service,
        handler=handler,
        notifier=notifier,
        callback_registration=WatchlistCallbackRegistration(handler),
        imported_seed=imported_seed,
    )


__all__ = [
    "WatchlistCallbackRegistration",
    "WatchlistRuntime",
    "build_watchlist_runtime",
]
