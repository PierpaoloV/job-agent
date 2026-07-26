"""Concrete, dependency-injected adapters for watchlist delivery and alerts."""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Callable, Iterable, Protocol
from urllib.parse import urlsplit

from notify_telegram import TelegramMessage
from watchlist_domain import CompanyProposal, JobAlertCandidate, JobAlertProposal
from watchlist_service import SubscriptionDefinitiveError
from watchlist_telegram import WatchlistTelegramHandler


@dataclass(frozen=True)
class CallableSourceSubscriptionAdapter:
    source: str
    subscriber: Callable[[JobAlertCandidate, str], dict]

    def subscribe(
        self, alert: JobAlertCandidate, *, idempotency_key: str
    ) -> dict:
        return self.subscriber(alert, idempotency_key)


class BrowserJobAlertDriver(Protocol):
    def open(self, url: str) -> None: ...

    def fill(self, locator: str, value: str) -> None: ...

    def click(self, locator: str) -> None: ...

    def exists(self, locator: str) -> bool: ...

    def text(self, locator: str) -> str | None: ...

    def current_url(self) -> str: ...


@dataclass(frozen=True)
class CallableBrowserJobAlertDriver:
    """Concrete boundary from injected browser operations to alert strategies."""

    navigate: Callable[[str], None]
    set_value: Callable[[str, str], None]
    activate: Callable[[str], None]
    has_element: Callable[[str], bool]
    read_text: Callable[[str], str | None]
    read_current_url: Callable[[], str]

    def open(self, url: str) -> None:
        self.navigate(url)

    def fill(self, locator: str, value: str) -> None:
        self.set_value(locator, value)

    def click(self, locator: str) -> None:
        self.activate(locator)

    def exists(self, locator: str) -> bool:
        return self.has_element(locator)

    def text(self, locator: str) -> str | None:
        return self.read_text(locator)

    def current_url(self) -> str:
        return self.read_current_url()


@dataclass(frozen=True)
class BrowserJobAlertStrategy:
    source: str
    version: str
    query_locator: str
    location_locator: str
    search_locator: str
    create_alert_locator: str
    confirmation_locator: str
    confirmation_patterns: tuple[str, ...]
    captcha_locator: str
    mfa_locator: str
    allowed_https_origins: tuple[str, ...]


class BrowserInterventionRequired(RuntimeError):
    """The browser flow cannot safely establish its external outcome."""


class BrowserJobAlertExecutor:
    """Execute a versioned source strategy through an injected browser driver."""

    def __init__(
        self,
        *,
        driver: BrowserJobAlertDriver,
        strategies: Iterable[BrowserJobAlertStrategy],
    ) -> None:
        self._driver = driver
        self._strategies = {
            _source_key(strategy.source): strategy for strategy in strategies
        }

    def subscribe(
        self, alert: JobAlertCandidate, *, idempotency_key: str
    ) -> dict:
        strategy = self._strategies.get(_source_key(alert.source))
        if strategy is None:
            raise SubscriptionDefinitiveError(
                f"No versioned browser strategy for {alert.source}"
            )
        try:
            _require_allowed_https_url(alert.source_url, strategy)
        except BrowserInterventionRequired as exc:
            raise SubscriptionDefinitiveError(str(exc)) from exc
        self._driver.open(alert.source_url)
        self._require_trusted_destination(strategy)
        self._raise_for_intervention(strategy)
        self._driver.fill(strategy.query_locator, alert.query)
        self._require_trusted_destination(strategy)
        self._driver.fill(strategy.location_locator, alert.location)
        self._require_trusted_destination(strategy)
        self._driver.click(strategy.search_locator)
        self._require_trusted_destination(strategy)
        self._raise_for_intervention(strategy)
        prior_evidence = self._driver.text(strategy.confirmation_locator)
        self._driver.click(strategy.create_alert_locator)
        self._require_trusted_destination(strategy)
        self._raise_for_intervention(strategy)
        evidence = self._driver.text(strategy.confirmation_locator)
        if not _is_positive_confirmation(
            evidence, prior_evidence=prior_evidence, strategy=strategy
        ):
            raise BrowserInterventionRequired(
                f"No confirmation evidence for {strategy.source} {strategy.version}"
            )
        return {
            "status": "subscribed",
            "external_reference": (
                f"{strategy.source}:{strategy.version}:{idempotency_key}:"
                f"{evidence.strip()}"
            ),
        }

    def _raise_for_intervention(self, strategy: BrowserJobAlertStrategy) -> None:
        if self._driver.exists(strategy.captcha_locator):
            raise BrowserInterventionRequired("CAPTCHA requires human intervention")
        if self._driver.exists(strategy.mfa_locator):
            raise BrowserInterventionRequired("MFA requires human intervention")

    def _require_trusted_destination(
        self, strategy: BrowserJobAlertStrategy
    ) -> None:
        _require_allowed_https_url(self._driver.current_url(), strategy)


@dataclass(frozen=True)
class BrowserSourceSubscriptionAdapter:
    source: str
    executor: BrowserJobAlertExecutor

    def subscribe(
        self, alert: JobAlertCandidate, *, idempotency_key: str
    ) -> dict:
        return self.executor.subscribe(alert, idempotency_key=idempotency_key)


LINKEDIN_JOB_ALERT_V1 = BrowserJobAlertStrategy(
    source="LinkedIn",
    version="linkedin-job-alert.v1",
    query_locator='input[aria-label="Search by title, skill, or company"]',
    location_locator='input[aria-label="City, state, or zip code"]',
    search_locator='button[aria-label="Search"]',
    create_alert_locator='button[aria-label^="Set alert for"]',
    confirmation_locator=".artdeco-toast-item__message",
    confirmation_patterns=(
        r"alert created(?:: [\w.-]+)?[.!]?",
        r"job alert created(?:: [\w.-]+)?[.!]?",
        r"alert set(?:: [\w.-]+)?[.!]?",
    ),
    captcha_locator='iframe[src*="captcha"]',
    mfa_locator='input[name="verificationCode"]',
    allowed_https_origins=("linkedin.com",),
)

INDEED_JOB_ALERT_V1 = BrowserJobAlertStrategy(
    source="Indeed",
    version="indeed-job-alert.v1",
    query_locator='input[name="q"]',
    location_locator='input[name="l"]',
    search_locator='button[type="submit"]',
    create_alert_locator='button[data-testid="job-alert-cta"]',
    confirmation_locator='[data-testid="job-alert-confirmation"]',
    confirmation_patterns=(
        r"alert created(?:: [\w.-]+)?[.!]?",
        r"job alert created(?:: [\w.-]+)?[.!]?",
        r"alert is active(?:: [\w.-]+)?[.!]?",
    ),
    captcha_locator='iframe[src*="captcha"]',
    mfa_locator='input[name="verificationCode"]',
    allowed_https_origins=("indeed.com",),
)


def browser_subscription_registry(
    driver: BrowserJobAlertDriver,
    *,
    strategies: Iterable[BrowserJobAlertStrategy] = (
        LINKEDIN_JOB_ALERT_V1,
        INDEED_JOB_ALERT_V1,
    ),
) -> "SourceSubscriptionRegistry":
    strategy_values = tuple(strategies)
    executor = BrowserJobAlertExecutor(driver=driver, strategies=strategy_values)
    return SourceSubscriptionRegistry(
        BrowserSourceSubscriptionAdapter(strategy.source, executor)
        for strategy in strategy_values
    )


class SourceSubscriptionRegistry:
    """Route an approved alert to an explicitly registered source adapter."""

    def __init__(
        self,
        adapters: Iterable[
            CallableSourceSubscriptionAdapter | BrowserSourceSubscriptionAdapter
        ],
    ) -> None:
        self._adapters = {}
        for adapter in adapters:
            key = _source_key(adapter.source)
            if key in self._adapters:
                raise ValueError(f"Duplicate subscription adapter: {adapter.source}")
            self._adapters[key] = adapter

    def subscribe(
        self, alert: JobAlertCandidate, *, idempotency_key: str
    ) -> dict:
        adapter = self._adapters.get(_source_key(alert.source))
        if adapter is None:
            raise SubscriptionDefinitiveError(
                f"No subscription adapter registered for {alert.source}"
            )
        return adapter.subscribe(alert, idempotency_key=idempotency_key)


class WatchlistTelegramNotifier:
    """Queue watchlist proposal messages through the shared delivery ledger."""

    def __init__(
        self,
        *,
        handler: WatchlistTelegramHandler,
        outbox: "WatchlistDeliveryOutbox",
        message_sender: Callable[[TelegramMessage], None],
    ) -> None:
        self._handler = handler
        self._outbox = outbox
        self._message_sender = message_sender
        self._delivery_owner = "watchlist-notifier:" + secrets.token_urlsafe(12)

    def send_company_proposal(
        self,
        proposal: CompanyProposal,
        *,
        intended_actor: str,
        intended_chat_id: str,
    ) -> bool:
        key = f"watchlist-company:{proposal.proposal_id}:{proposal.evidence_version}"
        def build_message() -> TelegramMessage:
            callback = self._handler.company_callback(
                proposal,
                intended_actor=intended_actor,
                intended_chat_id=intended_chat_id,
            )
            return TelegramMessage(
                text=(
                    "🏢 <b>Nuova azienda verificata</b>\n"
                    f"{html.escape(proposal.name)}\n"
                    f"Ownership: {html.escape(proposal.ownership.classification)}\n"
                    f"Fonte ownership: {html.escape(proposal.ownership.source_url)} "
                    f"({html.escape(proposal.ownership.verified_at)})\n"
                    "Sponsorship: "
                    f"{html.escape(proposal.sponsorship.classification)}\n"
                    f"Fonte sponsorship: {html.escape(proposal.sponsorship.source_url)} "
                    f"({html.escape(proposal.sponsorship.verified_at)})"
                ),
                reply_markup={"inline_keyboard": [[{
                    "text": "Approva monitoraggio",
                    "callback_data": callback,
                }]]},
            )

        return self._deliver(key, build_message)

    def send_alert_proposal(
        self,
        proposal: JobAlertProposal,
        *,
        intended_actor: str,
        intended_chat_id: str,
    ) -> bool:
        key = f"watchlist-alert:{proposal.proposal_id}:{proposal.version}"
        def build_message() -> TelegramMessage:
            callback = self._handler.subscription_callback(
                proposal,
                intended_actor=intended_actor,
                intended_chat_id=intended_chat_id,
            )
            alert = proposal.alert
            return TelegramMessage(
                text=(
                    "🔔 <b>Nuova job alert proposta</b>\n"
                    f"Fonte: {html.escape(alert.source)}\n"
                    f"Destinazione: {html.escape(alert.source_url)}\n"
                    f"Copertura: {html.escape(alert.expected_coverage)}\n"
                    f"Query: {html.escape(alert.query)} · "
                    f"{html.escape(alert.location)}"
                ),
                reply_markup={"inline_keyboard": [[{
                    "text": "Conferma job alert",
                    "callback_data": callback,
                }]]},
            )

        return self._deliver(key, build_message)

    def _deliver(
        self, key: str, build_message: Callable[[], TelegramMessage]
    ) -> bool:
        self._outbox.stage(key)
        lease_token = self._outbox.claim(key, owner=self._delivery_owner)
        if lease_token is None:
            return False
        payload = self._outbox.payload_for(key)
        try:
            if payload:
                message = _telegram_message_from_payload(payload)
            else:
                message = build_message()
                payload = _telegram_message_payload(message)
                if not self._outbox.store_payload(
                    key,
                    lease_token,
                    payload,
                    owner=self._delivery_owner,
                ):
                    raise DeliveryOutcomeUncertain(
                        "Delivery lease expired before payload was persisted"
                    )
        except Exception:
            self._outbox.release(
                key, lease_token, owner=self._delivery_owner
            )
            raise
        try:
            self._message_sender(message)
        except DeliveryDefinitiveError:
            self._outbox.release(
                key, lease_token, owner=self._delivery_owner
            )
            raise
        except Exception:
            self._outbox.mark_uncertain(
                key, lease_token, owner=self._delivery_owner
            )
            raise
        if not self._outbox.mark_sent(
            key, lease_token, owner=self._delivery_owner
        ):
            raise DeliveryOutcomeUncertain(
                "Transport returned after delivery lease ownership was lost"
            )
        return True


class WatchlistDeliveryOutbox:
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        lease_seconds: float = 300,
    ):
        if lease_seconds <= 0:
            raise ValueError("Delivery lease must be positive")
        self._path = Path(path)
        self._now = now
        self._lease_seconds = float(lease_seconds)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                "key TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "lease_expires REAL, lease_owner TEXT, lease_token TEXT, "
                "payload TEXT NOT NULL DEFAULT '', payload_created_at REAL)"
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(deliveries)")
            }
            migrations = {
                "lease_expires": "REAL",
                "lease_owner": "TEXT",
                "lease_token": "TEXT",
                "payload": "TEXT NOT NULL DEFAULT ''",
                "payload_created_at": "REAL",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE deliveries ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                "UPDATE deliveries SET payload = '' "
                "WHERE status = 'pending' AND payload != '' "
                "AND payload_created_at IS NULL"
            )

    def stage(self, key: str, payload: str = "") -> str:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO deliveries"
                "(key, status, payload, payload_created_at) "
                "VALUES (?, 'pending', ?, ?)",
                (key, payload, self._now() if payload else None),
            )
            if payload:
                connection.execute(
                    "UPDATE deliveries SET payload = ?, payload_created_at = ? "
                    "WHERE key = ? AND payload = ''",
                    (payload, self._now(), key),
                )
            row = connection.execute(
                "SELECT payload FROM deliveries WHERE key = ?", (key,)
            ).fetchone()
            return str(row[0])

    def claim(self, key: str, *, owner: str = "watchlist-worker") -> str | None:
        if not owner.strip():
            raise ValueError("Delivery lease owner is required")
        now = self._now()
        token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            self._expire_claims(connection, now)
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'claimed', lease_expires = ?, "
                "lease_owner = ?, lease_token = ? "
                "WHERE key = ? AND status = 'pending'",
                (now + self._lease_seconds, owner, token, key),
            )
            return token if cursor.rowcount == 1 else None

    def release(self, key: str, lease_token: str, *, owner: str) -> bool:
        with self._connect() as connection:
            self._expire_claims(connection, self._now())
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'pending', lease_expires = NULL, "
                "lease_owner = NULL, lease_token = NULL, payload = '', "
                "payload_created_at = NULL "
                "WHERE key = ? AND status = 'claimed' "
                "AND lease_owner = ? AND lease_token = ?",
                (key, owner, lease_token),
            )
            return cursor.rowcount == 1

    def mark_sent(self, key: str, lease_token: str, *, owner: str) -> bool:
        with self._connect() as connection:
            self._expire_claims(connection, self._now())
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'sent', lease_expires = NULL, "
                "lease_owner = NULL, lease_token = NULL "
                "WHERE key = ? AND status = 'claimed' "
                "AND lease_owner = ? AND lease_token = ?",
                (key, owner, lease_token),
            )
            return cursor.rowcount == 1

    def mark_uncertain(self, key: str, lease_token: str, *, owner: str) -> bool:
        with self._connect() as connection:
            self._expire_claims(connection, self._now())
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'uncertain', lease_expires = NULL, "
                "lease_owner = NULL, lease_token = NULL "
                "WHERE key = ? AND status = 'claimed' "
                "AND lease_owner = ? AND lease_token = ?",
                (key, owner, lease_token),
            )
            return cursor.rowcount == 1

    def store_payload(
        self,
        key: str,
        lease_token: str,
        payload: str,
        *,
        owner: str,
    ) -> bool:
        if not payload:
            raise ValueError("Delivery payload cannot be empty")
        with self._connect() as connection:
            self._expire_claims(connection, self._now())
            cursor = connection.execute(
                "UPDATE deliveries SET payload = ?, payload_created_at = ? "
                "WHERE key = ? "
                "AND status = 'claimed' AND lease_owner = ? AND lease_token = ? "
                "AND (payload = '' OR payload = ?)",
                (payload, self._now(), key, owner, lease_token, payload),
            )
            return cursor.rowcount == 1

    def payload_for(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM deliveries WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None or not str(row[0]) else str(row[0])

    def reconcile(self, key: str, *, outcome: str) -> bool:
        if outcome not in {"sent", "requeue"}:
            raise ValueError("Reconciliation outcome must be sent or requeue")
        target = "sent" if outcome == "sent" else "pending"
        with self._connect() as connection:
            self._expire_claims(connection, self._now())
            row = connection.execute(
                "SELECT status FROM deliveries WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return False
            status = str(row[0])
            if status == target:
                return True
            if status != "uncertain":
                return False
            if target == "pending":
                connection.execute(
                    "UPDATE deliveries SET status = ?, lease_expires = NULL, "
                    "lease_owner = NULL, lease_token = NULL, payload = '', "
                    "payload_created_at = NULL "
                    "WHERE key = ?",
                    (target, key),
                )
            else:
                connection.execute(
                    "UPDATE deliveries SET status = ?, lease_expires = NULL, "
                    "lease_owner = NULL, lease_token = NULL WHERE key = ?",
                    (target, key),
                )
            return True

    def status_for(self, key: str) -> str | None:
        with self._connect() as connection:
            self._expire_claims(connection, self._now())
            row = connection.execute(
                "SELECT status FROM deliveries WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row[0])

    def _connect(self):
        connection = sqlite3.connect(self._path, isolation_level="IMMEDIATE")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _expire_claims(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "UPDATE deliveries SET status = 'uncertain' "
            "WHERE status = 'claimed' AND "
            "(lease_expires IS NULL OR lease_expires <= ?)",
            (now,),
        )


class DeliveryDefinitiveError(RuntimeError):
    """The transport confirms that it accepted no message."""


class DeliveryOutcomeUncertain(RuntimeError):
    """The transport may have accepted a payload after its lease was lost."""


def _telegram_message_payload(message: TelegramMessage) -> str:
    return json.dumps(
        {"text": message.text, "reply_markup": message.reply_markup},
        sort_keys=True,
        separators=(",", ":"),
    )


def _telegram_message_from_payload(payload: str) -> TelegramMessage:
    value = json.loads(payload)
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError("Invalid persisted Telegram payload")
    reply_markup = value.get("reply_markup")
    if reply_markup is not None and not isinstance(reply_markup, dict):
        raise ValueError("Invalid persisted Telegram reply markup")
    return TelegramMessage(text=value["text"], reply_markup=reply_markup)


def _source_key(source: str) -> str:
    return " ".join(source.casefold().split())


def _is_positive_confirmation(
    evidence: str | None,
    *,
    prior_evidence: str | None,
    strategy: BrowserJobAlertStrategy,
) -> bool:
    current = " ".join(str(evidence or "").casefold().split())
    prior = " ".join(str(prior_evidence or "").casefold().split())
    if not current or current == prior:
        return False
    return any(
        re.fullmatch(pattern, current, flags=re.IGNORECASE) is not None
        for pattern in strategy.confirmation_patterns
    )


def _require_allowed_https_url(
    value: str, strategy: BrowserJobAlertStrategy
) -> None:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserInterventionRequired("Invalid destination port") from exc
    allowed = any(
        hostname == origin or hostname.endswith("." + origin)
        for origin in strategy.allowed_https_origins
    )
    if (
        parsed.scheme.casefold() != "https"
        or not allowed
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise BrowserInterventionRequired(
            f"Untrusted destination for {strategy.source} {strategy.version}"
        )
