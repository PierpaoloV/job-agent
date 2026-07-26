"""Production composition for private, read-only career correspondence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from career_correspondence import CareerCorrespondenceMonitor
from career_correspondence_adapters import (
    GmailCareerMailboxReader,
    TelegramCorrespondenceOutboxDispatcher,
)
from career_correspondence_domain import (
    CareerMailboxConnection,
    MailboxPollResult,
    MailboxPollStatus,
    TelegramDispatchResult,
)
from career_correspondence_store import JsonCareerCorrespondenceStore


@dataclass(frozen=True)
class CareerCorrespondenceCycleResult:
    mailbox: MailboxPollResult
    telegram: TelegramDispatchResult


@dataclass(frozen=True)
class CareerCorrespondenceRuntime:
    monitor: CareerCorrespondenceMonitor
    dispatcher: TelegramCorrespondenceOutboxDispatcher
    store: JsonCareerCorrespondenceStore
    mailbox: GmailCareerMailboxReader
    clock: object
    career_gmail_address: str

    def connect_dedicated_mailbox(self) -> CareerMailboxConnection:
        self.mailbox.verify_account(account_address=self.career_gmail_address)
        connection = CareerMailboxConnection(
            address=self.career_gmail_address,
            connected_at=self.clock.now().isoformat(),
        )
        return self.store.connect(connection)


def build_career_correspondence_runtime(
    *,
    repository_root: Path,
    gmail_service,
    applications,
    telegram_transport,
    clock,
    candidate_name: str,
    career_gmail_address: str,
) -> CareerCorrespondenceRuntime:
    root = Path(repository_root) / "data" / "private" / "career-correspondence"
    store = JsonCareerCorrespondenceStore(root)
    mailbox = GmailCareerMailboxReader(gmail_service)
    return CareerCorrespondenceRuntime(
        monitor=CareerCorrespondenceMonitor(
            mailbox=mailbox,
            store=store,
            applications=applications,
            clock=clock,
            candidate_name=candidate_name,
        ),
        dispatcher=TelegramCorrespondenceOutboxDispatcher(
            store=store,
            transport=telegram_transport,
            clock=clock,
        ),
        store=store,
        mailbox=mailbox,
        clock=clock,
        career_gmail_address=career_gmail_address,
    )


def run_career_correspondence_cycle(
    runtime: CareerCorrespondenceRuntime,
) -> CareerCorrespondenceCycleResult:
    """Run one production worker cycle without implicitly activating Gmail."""

    connection = runtime.store.connection()
    if connection is not None:
        runtime.mailbox.verify_account(account_address=connection.address)
    mailbox = runtime.monitor.poll()
    telegram = (
        runtime.dispatcher.dispatch_pending()
        if mailbox.status == MailboxPollStatus.COMPLETED
        else TelegramDispatchResult()
    )
    return CareerCorrespondenceCycleResult(mailbox=mailbox, telegram=telegram)


__all__ = [
    "CareerCorrespondenceCycleResult",
    "CareerCorrespondenceRuntime",
    "build_career_correspondence_runtime",
    "run_career_correspondence_cycle",
]
