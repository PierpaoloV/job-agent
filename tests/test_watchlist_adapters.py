from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from watchlist_adapters import (
    BrowserInterventionRequired,
    DeliveryDefinitiveError,
    CallableSourceSubscriptionAdapter,
    SourceSubscriptionRegistry,
    WatchlistTelegramNotifier,
    WatchlistDeliveryOutbox,
    browser_subscription_registry,
)
from watchlist_domain import CompanyCandidate, EligibilityEvidence, JobAlertCandidate
from watchlist_service import WatchlistService
from watchlist_service import SubscriptionDefinitiveError
from watchlist_store import JsonWatchlistStore
from watchlist_telegram import WatchlistTelegramHandler


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 12, tzinfo=timezone.utc)


def _evidence(classification):
    return EligibilityEvidence(
        classification=classification,
        source_url="https://registry.example/evidence",
        verified_at="2026-07-16T08:00:00+00:00",
    )


def test_telegram_notifier_queues_company_and_alert_proposals_once(tmp_path):
    calls = []
    registry = SourceSubscriptionRegistry([])
    service = WatchlistService(
        store=JsonWatchlistStore(tmp_path / "watchlist.json"),
        clock=FixedClock(),
        subscription_executor=registry,
    )
    company = service.propose_companies([CompanyCandidate(
        name="Example AI",
        careers_url="https://example.test/careers",
        jurisdiction="Switzerland",
        jurisdiction_country_code="CH",
        ownership=_evidence("verified_control"),
        sponsorship=_evidence("not_required_eu"),
        discovery_source="https://research.example/watchlist",
    )])[0]
    alert = service.propose_job_alert(JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI research roles in Zurich",
        query="AI Research Scientist",
        location="Zurich",
    ))
    notifier = WatchlistTelegramNotifier(
        handler=WatchlistTelegramHandler(service),
        outbox=WatchlistDeliveryOutbox(tmp_path / "watchlist-outbox.sqlite"),
        message_sender=calls.append,
    )

    assert notifier.send_company_proposal(
        company, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is True
    assert notifier.send_company_proposal(
        company, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is False
    assert notifier.send_alert_proposal(
        alert, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is True

    assert len(calls) == 2
    assert calls[0].reply_markup["inline_keyboard"][0][0]["callback_data"].startswith(
        "wc:"
    )
    assert "https://registry.example/evidence" in calls[0].text
    assert "2026-07-16T08:00:00+00:00" in calls[0].text
    assert "English AI research roles in Zurich" in calls[1].text
    assert "https://linkedin.com/jobs" in calls[1].text


def test_telegram_outbox_retries_pre_send_failure_and_never_resends_sent(tmp_path):
    service = WatchlistService(
        store=JsonWatchlistStore(tmp_path / "watchlist.json"),
        clock=FixedClock(),
        subscription_executor=SourceSubscriptionRegistry([]),
    )
    proposal = service.propose_companies([CompanyCandidate(
        name="Retry AI",
        careers_url="https://retry.example/careers",
        jurisdiction="Switzerland",
        jurisdiction_country_code="CH",
        ownership=_evidence("verified_control"),
        sponsorship=_evidence("not_required_eu"),
        discovery_source="https://research.example/watchlist",
    )])[0]
    outbox = WatchlistDeliveryOutbox(tmp_path / "outbox.sqlite")
    calls = []

    def flaky(message):
        calls.append(message)
        if len(calls) == 1:
            raise DeliveryDefinitiveError("failed before transport accepted payload")

    notifier = WatchlistTelegramNotifier(
        handler=WatchlistTelegramHandler(service),
        outbox=outbox,
        message_sender=flaky,
    )
    try:
        notifier.send_company_proposal(
            proposal, intended_actor="synthetic-owner", intended_chat_id="42"
        )
    except DeliveryDefinitiveError:
        pass
    else:
        raise AssertionError("fixture must fail once")

    delivery_key = (
        f"watchlist-company:{proposal.proposal_id}:{proposal.evidence_version}"
    )
    assert outbox.status_for(delivery_key) == "pending"
    assert notifier.send_company_proposal(
        proposal, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is True
    assert notifier.send_company_proposal(
        proposal, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is False
    assert len(calls) == 2
    first_callback = calls[0].reply_markup["inline_keyboard"][0][0]["callback_data"]
    second_callback = calls[1].reply_markup["inline_keyboard"][0][0]["callback_data"]
    assert first_callback != second_callback


def test_outbox_migrates_legacy_schema_and_preserves_rows(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE deliveries (key TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO deliveries(key, status) VALUES ('legacy', 'pending')"
        )

    outbox = WatchlistDeliveryOutbox(path)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
        }
    assert {
        "key",
        "status",
        "lease_expires",
        "lease_owner",
        "lease_token",
        "payload",
        "payload_created_at",
    } <= columns
    assert outbox.status_for("legacy") == "pending"


def test_outbox_upgrade_discards_undated_pending_callback_payload(tmp_path):
    path = tmp_path / "old-current.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE deliveries ("
            "key TEXT PRIMARY KEY, status TEXT NOT NULL, lease_expires REAL, "
            "lease_owner TEXT, lease_token TEXT, payload TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "INSERT INTO deliveries(key, status, payload) "
            "VALUES ('old-pending', 'pending', '{\"expired_callback\":true}')"
        )

    outbox = WatchlistDeliveryOutbox(path)

    assert outbox.status_for("old-pending") == "pending"
    assert outbox.payload_for("old-pending") is None


def test_expired_delivery_lease_becomes_uncertain_until_explicit_reconciliation(
    tmp_path,
):
    now = [100.0]
    outbox = WatchlistDeliveryOutbox(
        tmp_path / "outbox.sqlite", now=lambda: now[0], lease_seconds=30
    )
    outbox.stage("delivery", '{"callback":"stable"}')
    first = outbox.claim("delivery", owner="worker-a")

    assert first is not None
    assert outbox.mark_sent("delivery", first, owner="worker-b") is False
    now[0] += 31
    restarted = WatchlistDeliveryOutbox(
        tmp_path / "outbox.sqlite", now=lambda: now[0], lease_seconds=30
    )
    assert restarted.status_for("delivery") == "uncertain"
    assert restarted.mark_sent("delivery", first, owner="worker-a") is False
    assert restarted.release("delivery", first, owner="worker-a") is False
    assert restarted.payload_for("delivery") == '{"callback":"stable"}'

    assert restarted.reconcile("delivery", outcome="requeue") is True
    second = restarted.claim("delivery", owner="worker-b")
    assert second is not None and second != first
    assert restarted.mark_sent("delivery", second, owner="worker-b") is True
    assert restarted.reconcile("delivery", outcome="sent") is True


def test_callback_build_failure_is_retryable_but_post_send_timeout_is_uncertain(
    tmp_path,
):
    service = WatchlistService(
        store=JsonWatchlistStore(tmp_path / "watchlist.json"),
        clock=FixedClock(),
        subscription_executor=SourceSubscriptionRegistry([]),
    )
    proposal = service.propose_companies([CompanyCandidate(
        name="Boundary AI",
        careers_url="https://boundary.example/careers",
        jurisdiction="Switzerland",
        jurisdiction_country_code="CH",
        ownership=_evidence("verified_control"),
        sponsorship=_evidence("not_required_eu"),
        discovery_source="https://research.example/watchlist",
    )])[0]
    outbox = WatchlistDeliveryOutbox(tmp_path / "outbox.sqlite")

    class FailsCallbackOnce:
        def __init__(self):
            self.calls = 0

        def company_callback(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("invalid callback configuration")
            return "wc:stable"

    handler = FailsCallbackOnce()
    sent = []
    notifier = WatchlistTelegramNotifier(
        handler=handler, outbox=outbox, message_sender=sent.append
    )
    try:
        notifier.send_company_proposal(
            proposal, intended_actor="synthetic-owner", intended_chat_id="42"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("callback fixture must fail before transport")
    key = f"watchlist-company:{proposal.proposal_id}:{proposal.evidence_version}"
    assert outbox.status_for(key) == "pending"
    assert notifier.send_company_proposal(
        proposal, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is True

    alert = service.propose_job_alert(JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI roles",
        query="AI",
        location="Zurich",
    ))
    alert_key = f"watchlist-alert:{alert.proposal_id}:{alert.version}"
    timeout_notifier = WatchlistTelegramNotifier(
        handler=WatchlistTelegramHandler(service),
        outbox=outbox,
        message_sender=lambda message: (_ for _ in ()).throw(TimeoutError()),
    )
    try:
        timeout_notifier.send_alert_proposal(
            alert, intended_actor="synthetic-owner", intended_chat_id="42"
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("transport fixture must time out after I/O begins")
    assert outbox.status_for(alert_key) == "uncertain"


def test_source_registry_is_a_concrete_offline_safe_subscription_executor():
    calls = []
    adapter = CallableSourceSubscriptionAdapter(
        source="LinkedIn",
        subscriber=lambda alert, key: calls.append((alert, key)) or {
            "status": "subscribed",
            "external_reference": "fixture-alert-1",
        },
    )
    registry = SourceSubscriptionRegistry([adapter])
    alert = JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI research roles in Zurich",
        query="AI Research Scientist",
        location="Zurich",
    )

    outcome = registry.subscribe(alert, idempotency_key="stable-key")

    assert outcome["status"] == "subscribed"
    assert calls == [(alert, "stable-key")]


class FixtureBrowserDriver:
    def __init__(
        self,
        *,
        blocked: str | None = None,
        redirect_after_search: str | None = None,
    ):
        self.blocked = blocked
        self.redirect_after_search = redirect_after_search
        self.events = []
        self.url = ""
        self.alert_clicked = False
        self.confirmation_text = "Alert created: fixture-42"

    def open(self, url):
        self.url = url
        self.events.append(("open", url))

    def fill(self, locator, value):
        self.events.append(("fill", locator, value))

    def click(self, locator):
        self.events.append(("click", locator))
        if "alert" in locator.casefold():
            self.alert_clicked = True
        if self.redirect_after_search and "Search" in locator:
            self.url = self.redirect_after_search

    def exists(self, locator):
        return self.blocked is not None and self.blocked in locator.casefold()

    def text(self, locator):
        self.events.append(("text", locator))
        return self.confirmation_text if self.alert_clicked else None

    def current_url(self):
        return self.url


def test_versioned_browser_strategies_execute_linkedin_and_indeed_offline():
    for source, url in (
        ("LinkedIn", "https://linkedin.com/jobs"),
        ("Indeed", "https://indeed.com/jobs"),
    ):
        driver = FixtureBrowserDriver()
        registry = browser_subscription_registry(driver)
        alert = JobAlertCandidate(
            source=source,
            source_url=url,
            expected_coverage="English AI research roles",
            query="AI Research Scientist",
            location="Zurich",
        )

        outcome = registry.subscribe(alert, idempotency_key="semantic-key")

        assert outcome["status"] == "subscribed"
        assert "semantic-key" in outcome["external_reference"]
        assert driver.events[0] == ("open", url)
        assert driver.events[1] == (
            "fill",
            driver.events[1][1],
            "AI Research Scientist",
        )
        assert ("fill", driver.events[2][1], "Zurich") == driver.events[2]


def test_browser_strategy_rejects_stale_or_error_confirmation_toast():
    alert = JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI research roles",
        query="AI Research Scientist",
        location="Zurich",
    )
    for text in (
        "Old notification",
        "Error: unable to create alert",
        "Alert settings could not be saved",
        "No alert created",
    ):
        driver = FixtureBrowserDriver()
        driver.confirmation_text = text
        registry = browser_subscription_registry(driver)

        try:
            registry.subscribe(alert, idempotency_key="semantic-key")
        except BrowserInterventionRequired:
            pass
        else:
            raise AssertionError("Untyped confirmation must not prove subscription")


def test_requeued_delivery_builds_fresh_nonexpired_callback(tmp_path):
    class Adjustable:
        def __init__(self):
            self.current = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)

        def now(self):
            return self.current

    clock = Adjustable()
    service = WatchlistService(
        store=JsonWatchlistStore(tmp_path / "watchlist.json"),
        clock=clock,
        subscription_executor=SourceSubscriptionRegistry([]),
    )
    proposal = service.propose_companies([CompanyCandidate(
        name="Fresh Callback AI",
        careers_url="https://fresh.example/careers",
        jurisdiction="Switzerland",
        jurisdiction_country_code="CH",
        ownership=_evidence("verified_control"),
        sponsorship=_evidence("not_required_eu"),
        discovery_source="https://research.example/watchlist",
    )])[0]
    outbox = WatchlistDeliveryOutbox(tmp_path / "outbox.sqlite")
    first_messages = []

    def ambiguous_sender(message):
        first_messages.append(message)
        raise TimeoutError("Telegram outcome unknown")

    notifier = WatchlistTelegramNotifier(
        handler=WatchlistTelegramHandler(service),
        outbox=outbox,
        message_sender=ambiguous_sender,
    )
    try:
        notifier.send_company_proposal(
            proposal, intended_actor="synthetic-owner", intended_chat_id="42"
        )
    except TimeoutError:
        pass
    key = f"watchlist-company:{proposal.proposal_id}:{proposal.evidence_version}"
    old_callback = first_messages[0].reply_markup["inline_keyboard"][0][0][
        "callback_data"
    ]

    clock.current += timedelta(minutes=31)
    assert outbox.reconcile(key, outcome="requeue") is True
    sent = []
    retry = WatchlistTelegramNotifier(
        handler=WatchlistTelegramHandler(service),
        outbox=outbox,
        message_sender=sent.append,
    )
    assert retry.send_company_proposal(
        proposal, intended_actor="synthetic-owner", intended_chat_id="42"
    ) is True
    new_callback = sent[0].reply_markup["inline_keyboard"][0][0][
        "callback_data"
    ]

    assert new_callback != old_callback
    assert WatchlistTelegramHandler(service).handle_callback(
        old_callback, actor="synthetic-owner", chat_id="42"
    ).status == "expired"
    assert WatchlistTelegramHandler(service).handle_callback(
        new_callback, actor="synthetic-owner", chat_id="42"
    ).status == "monitoring_activated"


def test_browser_strategy_fails_closed_on_captcha_without_success():
    driver = FixtureBrowserDriver(blocked="captcha")
    registry = browser_subscription_registry(driver)
    alert = JobAlertCandidate(
        source="LinkedIn",
        source_url="https://linkedin.com/jobs",
        expected_coverage="English AI research roles",
        query="AI Research Scientist",
        location="Zurich",
    )

    try:
        registry.subscribe(alert, idempotency_key="semantic-key")
    except BrowserInterventionRequired as exc:
        assert "CAPTCHA" in str(exc)
    else:
        raise AssertionError("CAPTCHA must never be reported as success")


def test_browser_strategy_rejects_untrusted_url_and_redirect():
    alert = JobAlertCandidate(
        source="LinkedIn",
        source_url="https://evil.example/jobs",
        expected_coverage="English AI research roles",
        query="AI Research Scientist",
        location="Zurich",
    )
    driver = FixtureBrowserDriver()
    registry = browser_subscription_registry(driver)

    try:
        registry.subscribe(alert, idempotency_key="semantic-key")
    except SubscriptionDefinitiveError as exc:
        assert "Untrusted destination" in str(exc)
    else:
        raise AssertionError("Untrusted source URL must never be opened")
    assert driver.events == []

    redirecting = FixtureBrowserDriver(
        redirect_after_search="https://evil.example/redirect"
    )
    registry = browser_subscription_registry(redirecting)
    trusted = JobAlertCandidate(
        source="LinkedIn",
        source_url="https://www.linkedin.com/jobs",
        expected_coverage="English AI research roles",
        query="AI Research Scientist",
        location="Zurich",
    )
    try:
        registry.subscribe(trusted, idempotency_key="semantic-key")
    except BrowserInterventionRequired as exc:
        assert "Untrusted destination" in str(exc)
    else:
        raise AssertionError("Cross-origin redirect must never continue")
