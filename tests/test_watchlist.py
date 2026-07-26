from __future__ import annotations

from datetime import datetime, timedelta, timezone
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from watchlist_domain import (
    CompanyCandidate,
    EligibilityEvidence,
    JobAlertCandidate,
)
from watchlist_service import CompanyEligibilityPolicy, WatchlistService
from watchlist_service import SubscriptionDefinitiveError
from watchlist_store import JsonWatchlistStore, TargetedCompanySeed
from watchlist_telegram import WatchlistTelegramHandler


ACTOR = "synthetic-owner"
CHAT_ID = "telegram-chat-42"


class AdjustableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


class RecordingSubscriptionExecutor:
    def __init__(self) -> None:
        self.calls = []

    def subscribe(self, alert, *, idempotency_key: str):
        self.calls.append((alert, idempotency_key))
        return {
            "status": "subscribed",
            "external_reference": "offline-fixture-1",
        }


class FailingSubscriptionExecutor(RecordingSubscriptionExecutor):
    def subscribe(self, alert, *, idempotency_key: str):
        self.calls.append((alert, idempotency_key))
        raise SubscriptionDefinitiveError(
            "offline fixture failure with secret detail"
        )


class UncertainSubscriptionExecutor(RecordingSubscriptionExecutor):
    def subscribe(self, alert, *, idempotency_key: str):
        self.calls.append((alert, idempotency_key))
        raise TimeoutError("outcome is not knowable")


class BlockingSubscriptionExecutor(RecordingSubscriptionExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def subscribe(self, alert, *, idempotency_key: str):
        self.calls.append((alert, idempotency_key))
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test executor was not released")
        return {
            "status": "subscribed",
            "external_reference": "offline-blocking-fixture",
        }


class ProcessBlockingSubscriptionExecutor:
    def __init__(self, started, release) -> None:
        self.started = started
        self.release = release

    def subscribe(self, alert, *, idempotency_key: str):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("process fixture was not released")
        return {
            "status": "subscribed",
            "external_reference": "offline-process-fixture",
        }


def _run_blocked_subscription_process(
    state_path: str,
    current_time: str,
    callback: str,
    started,
    release,
) -> None:
    clock = AdjustableClock(datetime.fromisoformat(current_time))
    service = WatchlistService(
        store=JsonWatchlistStore(Path(state_path)),
        clock=clock,
        subscription_executor=ProcessBlockingSubscriptionExecutor(started, release),
    )
    result = WatchlistTelegramHandler(service).handle_callback(
        callback,
        actor=ACTOR,
        chat_id=CHAT_ID,
    )
    if result.status != "subscribed":
        raise AssertionError(f"unexpected process result: {result.status}")


def evidence(
    classification: str = "eligible",
    *,
    source: str = "https://registry.example/company",
    verified_at: str = "2026-07-16T08:00:00+00:00",
) -> EligibilityEvidence:
    return EligibilityEvidence(
        classification=classification,
        source_url=source,
        verified_at=verified_at,
    )


def company(index: int, **changes) -> CompanyCandidate:
    values = {
        "name": f"Candidate {index}",
        "careers_url": f"https://candidate-{index}.example/careers",
        "jurisdiction": "Switzerland",
        "jurisdiction_country_code": "CH",
        "ownership": evidence("verified_control"),
        "sponsorship": evidence("not_required_eu"),
        "discovery_source": "https://research.example/watchlist",
    }
    values.update(changes)
    return CompanyCandidate(**values)


def build(tmp_path, clock, executor=None):
    return WatchlistService(
        store=JsonWatchlistStore(tmp_path / "watchlist.json"),
        clock=clock,
        subscription_executor=executor or RecordingSubscriptionExecutor(),
    )


def test_existing_targeted_company_source_is_imported_losslessly(tmp_path):
    path = ROOT / "watchlist" / "targeted-companies.md"
    seed = TargetedCompanySeed.read(path)
    store = JsonWatchlistStore(tmp_path / "watchlist.json")

    imported = store.import_seed(seed)
    imported_again = store.import_seed(seed)

    assert len(seed.sha256) == 64
    assert seed.raw_bytes == path.read_bytes()
    assert "Example Research Labs" in imported.company_names
    assert "Example Robotics" in imported.company_names
    assert imported_again == imported
    assert store.active_company_names() == ()


def test_changed_seed_creates_revision_and_only_adds_companies(tmp_path):
    first_path = tmp_path / "first.md"
    first_path.write_text("- Alpha AI — https://alpha.example/\n", encoding="utf-8")
    second_path = tmp_path / "second.md"
    second_path.write_text("- Beta AI — https://beta.example/\n", encoding="utf-8")
    first = TargetedCompanySeed.read(first_path)
    second = TargetedCompanySeed.read(second_path)
    store = JsonWatchlistStore(tmp_path / "watchlist.json")

    imported_first = store.import_seed(first)
    imported_second = store.import_seed(second)
    imported_again = store.import_seed(second)

    assert imported_first.source_hashes == (first.sha256,)
    assert imported_second.source_hashes == (first.sha256, second.sha256)
    assert imported_again == imported_second
    assert set(imported_second.company_names) == {"Alpha AI", "Beta AI"}
    assert store.active_company_names() == ()
    state = store.load()
    assert state["seed"]["revisions"][0]["sha256"] == first.sha256
    assert state["seed"]["revisions"][1]["sha256"] == second.sha256


def test_at_most_five_verified_companies_are_proposed_per_rolling_two_weeks(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)

    first = service.propose_companies([company(index) for index in range(8)])
    clock.advance(days=13)
    second = service.propose_companies([company(index) for index in range(8, 12)])
    clock.advance(days=2)
    third = service.propose_companies([company(index) for index in range(8, 12)])

    assert len(first) == 5
    assert second == ()
    assert len(third) == 4
    assert all(item.ownership.source_url for item in (*first, *third))
    assert all(item.ownership.verified_at for item in (*first, *third))


def test_changed_evidence_is_revisitable_after_stale_or_ineligible_result(
    tmp_path,
):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    stale = company(
        1,
        ownership=evidence("verified_control", verified_at="2025-01-01T00:00:00Z"),
    )
    unverified = company(2, ownership=evidence("unverified"))
    failed = company(3, sponsorship=evidence("verification_failed"))

    assert service.propose_companies([stale, unverified, failed]) == ()

    changed = company(
        2,
        ownership=evidence(
            "verified_control",
            source="https://registry.example/new-owner",
            verified_at="2026-07-17T08:00:00+00:00",
        ),
        sponsorship=evidence(
            "sponsors",
            source="https://candidate-2.example/immigration",
            verified_at="2026-07-17T08:00:00+00:00",
        ),
    )
    clock.advance(days=1)

    assert [item.name for item in service.propose_companies([changed])] == [
        "Candidate 2"
    ]

    recovered = company(
        3,
        sponsorship=evidence(
            "not_stated",
            source="https://candidate-3.example/immigration",
            verified_at="2026-07-17T08:00:00+00:00",
        ),
    )
    assert [item.name for item in service.propose_companies([recovered])] == [
        "Candidate 3"
    ]


def test_ownership_requires_a_verified_classification(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)

    assert len(service.propose_companies([company(
        1, ownership=evidence("Verified Control")
    )])) == 1
    assert service.propose_companies([company(
        2, ownership=evidence("unknown")
    )]) == ()
    assert len(service.propose_companies([company(
        3, ownership=evidence("Alternate Control")
    )])) == 1


def test_private_ownership_policy_filters_proposals_and_active_monitoring(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = WatchlistService(
        store=JsonWatchlistStore(tmp_path / "watchlist.json"),
        clock=clock,
        subscription_executor=RecordingSubscriptionExecutor(),
        eligibility_policy=CompanyEligibilityPolicy.from_values(
            excluded_ownership=["restricted_control"]
        ),
    )

    assert service.propose_companies(
        [company(1, ownership=evidence("restricted_control"))]
    ) == ()
    proposal = service.propose_companies(
        [company(2, ownership=evidence("verified_control"))]
    )[0]
    telegram = WatchlistTelegramHandler(service)
    assert telegram.handle_callback(
        telegram.company_callback(
            proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    ).status == "monitoring_activated"

    clock.advance(days=1)
    assert service.propose_companies(
        [
            company(
                2,
                ownership=evidence(
                    "restricted_control",
                    source="https://registry.example/change",
                    verified_at="2026-07-17T08:00:00+00:00",
                ),
            )
        ]
    ) == ()
    assert service.active_company_names() == ()
    assert service.company_monitoring_status("Candidate 2") == "review_required"


def test_older_or_equal_conflicting_evidence_cannot_supersede_newer_state(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 20, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    newer = company(1, ownership=evidence(
        "verified_control", verified_at="2026-07-20T08:00:00+00:00"
    ))
    proposal = service.propose_companies([newer])[0]
    older = company(1, ownership=evidence(
        "alternate_control", verified_at="2026-07-19T08:00:00+00:00"
    ))

    assert service.propose_companies([older]) == ()
    telegram = WatchlistTelegramHandler(service)
    assert telegram.handle_callback(
        telegram.company_callback(
            proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ), actor=ACTOR, chat_id=CHAT_ID
    ).status == "monitoring_activated"

    equal_conflict = company(1, ownership=evidence(
        "alternate_control", verified_at="2026-07-20T08:00:00+00:00"
    ))
    assert service.propose_companies([equal_conflict]) == ()
    assert service.active_company_names() == ()
    assert service.company_monitoring_status("Candidate 1") == "review_required"


def test_fact_evidence_merges_independently_and_future_facts_do_not_poison_clocks(
    tmp_path,
):
    clock = AdjustableClock(datetime(2026, 7, 20, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    base = company(
        11,
        ownership=evidence(
            "verified_control", verified_at="2026-07-20T08:00:00+00:00"
        ),
        sponsorship=evidence(
            "not_stated", verified_at="2026-07-20T08:00:00+00:00"
        ),
    )
    assert len(service.propose_companies([base])) == 1

    conflict_with_new_sponsorship = company(
        11,
        ownership=evidence(
            "alternate_control", verified_at="2026-07-20T08:00:00+00:00"
        ),
        sponsorship=evidence(
            "sponsors", verified_at="2026-07-20T09:00:00+00:00"
        ),
    )
    assert service.propose_companies([conflict_with_new_sponsorship]) == ()
    state = service._store.load()
    recorded = CompanyCandidate.from_dict(
        state["company_evidence"]["candidate 11"]["candidate"]
    )
    assert recorded.ownership.classification == "verified_control"
    assert recorded.sponsorship.classification == "sponsors"
    assert state["company_evidence"]["candidate 11"]["conflicts"] == {
        "ownership": True,
        "sponsorship": False,
    }

    future_with_newer_sponsorship = company(
        11,
        ownership=evidence(
            "verified_control", verified_at="2027-01-01T00:00:00+00:00"
        ),
        sponsorship=evidence(
            "yes", verified_at="2026-07-20T10:00:00+00:00"
        ),
    )
    assert service.propose_companies([future_with_newer_sponsorship]) == ()
    state = service._store.load()
    recorded = CompanyCandidate.from_dict(
        state["company_evidence"]["candidate 11"]["candidate"]
    )
    assert recorded.ownership.verified_at == "2026-07-20T08:00:00+00:00"
    assert recorded.sponsorship.classification == "yes"

    resolved = company(
        11,
        ownership=evidence(
            "verified_control", verified_at="2026-07-20T11:00:00+00:00"
        ),
        sponsorship=evidence(
            "not_stated", verified_at="2026-07-20T08:30:00+00:00"
        ),
    )
    replacement = service.propose_companies([resolved])
    assert len(replacement) == 1
    assert replacement[0].sponsorship.classification == "yes"


def test_valid_jurisdictions_are_not_subject_to_a_built_in_country_exclusion(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 20, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)

    allowed = service.propose_companies(
        [
            company(27, jurisdiction="Lyon, France", jurisdiction_country_code="FR"),
            company(28, jurisdiction="Berlin, Germany", jurisdiction_country_code="DE"),
        ]
    )
    assert [item.name for item in allowed] == [
        "Candidate 27",
        "Candidate 28",
    ]


def test_new_company_verification_requires_structured_country_code(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 20, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)

    proposals = service.propose_companies([
        company(
            32,
            jurisdiction="Chicago, Illinois",
            jurisdiction_country_code=None,
        ),
        company(
            33,
            jurisdiction="Chicago, Illinois",
            jurisdiction_country_code="US",
        ),
        company(
            35,
            jurisdiction="Unknown jurisdiction",
            jurisdiction_country_code="ZZ",
        ),
    ])

    assert [item.name for item in proposals] == ["Candidate 33"]


def test_company_evidence_requires_real_https_sources(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 20, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    candidates = (
        company(40, careers_url="not-a-url"),
        company(41, discovery_source="file:///tmp/research"),
        company(42, ownership=evidence("verified_control", source="not-a-url")),
        company(43, sponsorship=evidence("not_required_eu", source="http://example.com")),
        company(44, discovery_source="https://user:secret@example.com/source"),
        company(45, discovery_source="https://exa mple.com/source"),
        company(46, discovery_source="https://./source"),
        company(47, discovery_source="https://-/source"),
    )

    assert service.propose_companies(candidates) == ()


def test_new_company_enters_monitoring_only_after_exact_telegram_approval(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    proposal = service.propose_companies([company(1)])[0]
    telegram = WatchlistTelegramHandler(service)
    callback = telegram.company_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )

    assert "Candidate 1" not in service.active_company_names()
    result = telegram.handle_callback(callback, actor=ACTOR, chat_id=CHAT_ID)

    assert result.status == "monitoring_activated"
    assert service.active_company_names() == ("Candidate 1",)
    assert telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    ).status == "replayed"


def test_company_approval_rechecks_that_evidence_is_still_current(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    proposal = service.propose_companies([company(1)])[0]
    telegram = WatchlistTelegramHandler(service)
    clock.advance(days=91)

    result = telegram.handle_callback(
        telegram.company_callback(
            proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    )

    assert result.status == "stale"
    assert service.active_company_names() == ()


def test_active_company_is_suspended_when_identical_evidence_expires(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    candidate = company(1)
    proposal = service.propose_companies([candidate])[0]
    telegram = WatchlistTelegramHandler(service)
    assert telegram.handle_callback(
        telegram.company_callback(
            proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    ).status == "monitoring_activated"

    clock.advance(days=91)
    assert service.propose_companies([candidate]) == ()
    assert service.active_company_names() == ()
    assert service.company_monitoring_status("Candidate 1") == "review_required"


def test_active_company_evidence_expires_during_operational_reads(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    proposal = service.propose_companies([company(1)])[0]
    telegram = WatchlistTelegramHandler(service)
    assert telegram.handle_callback(
        telegram.company_callback(
            proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    ).status == "monitoring_activated"

    clock.advance(days=91)

    assert service.active_company_names() == ()
    assert service.company_monitoring_status("Candidate 1") == "review_required"
    persisted = service._store.load()["active_companies"]["candidate 1"]
    assert persisted["review_reason"] == "expired_evidence"


def test_legacy_active_company_without_country_code_requires_reverification(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    store = JsonWatchlistStore(tmp_path / "watchlist.json")
    service = WatchlistService(
        store=store,
        clock=clock,
        subscription_executor=RecordingSubscriptionExecutor(),
    )
    proposal = service.propose_companies([
        company(
            34,
            jurisdiction="Chicago, Illinois",
            jurisdiction_country_code="US",
        )
    ])[0]
    telegram = WatchlistTelegramHandler(service)
    assert telegram.handle_callback(
        telegram.company_callback(
            proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    ).status == "monitoring_activated"

    state = store.load()
    state["company_evidence"]["candidate 34"]["candidate"].pop(
        "jurisdiction_country_code"
    )
    store.save(state)

    restarted = WatchlistService(
        store=store,
        clock=clock,
        subscription_executor=RecordingSubscriptionExecutor(),
    )
    assert restarted.active_company_names() == ()
    assert restarted.company_monitoring_status("Candidate 34") == "review_required"
    migrated = store.load()["active_companies"]["candidate 34"]
    assert migrated["review_reason"] == "unverified_jurisdiction"


def test_material_ownership_change_supersedes_pending_and_active_monitoring(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    telegram = WatchlistTelegramHandler(service)
    proposal = service.propose_companies([company(1)])[0]
    pending_callback = telegram.company_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )
    changed = company(
        1,
        ownership=evidence(
            "third_party_control",
            source="https://registry.example/acquisition",
            verified_at="2026-07-17T08:00:00+00:00",
        ),
    )
    clock.advance(days=1)

    changed_proposal = service.propose_companies([changed])[0]
    assert telegram.handle_callback(
        pending_callback, actor=ACTOR, chat_id=CHAT_ID
    ).status == "superseded"

    assert telegram.handle_callback(
        telegram.company_callback(
            changed_proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    ).status == "monitoring_activated"
    assert service.active_company_names() == ("Candidate 1",)

    acquired_again = company(
        1,
        ownership=evidence(
            "alternate_control",
            source="https://registry.example/second-acquisition",
            verified_at="2026-07-18T08:00:00+00:00",
        ),
    )
    clock.advance(days=1)
    assert len(service.propose_companies([acquired_again])) == 1
    assert service.active_company_names() == ()
    assert service.company_monitoring_status("Candidate 1") == "review_required"


def test_alert_subscription_requires_specific_confirmation_and_is_idempotent(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = RecordingSubscriptionExecutor()
    service = build(tmp_path, clock, executor)
    alert = JobAlertCandidate(
        source="LinkedIn",
        source_url="https://www.linkedin.com/jobs/",
        expected_coverage="English AI research roles in Zurich",
        query="AI Research Scientist",
        location="Zurich",
    )
    proposal = service.propose_job_alert(alert)
    telegram = WatchlistTelegramHandler(service)

    assert executor.calls == []
    callback = telegram.subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )
    report = telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )
    replay = telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )

    assert len(executor.calls) == 1
    assert executor.calls[0][1] == report.idempotency_key
    assert report.status == "subscribed"
    assert replay == report
    assert report.expected_coverage == "English AI research roles in Zurich"


def test_mismatched_subscription_confirmation_cannot_create_external_alert(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = RecordingSubscriptionExecutor()
    service = build(tmp_path, clock, executor)
    proposal = service.propose_job_alert(JobAlertCandidate(
        source="Indeed",
        source_url="https://www.indeed.com/",
        expected_coverage="Computer vision roles across Switzerland",
        query="Computer Vision",
        location="Switzerland",
    ))
    callback = WatchlistTelegramHandler(service).subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )

    result = WatchlistTelegramHandler(service).handle_callback(
        callback + "-tampered", actor=ACTOR, chat_id=CHAT_ID
    )

    assert result.status == "mismatched"
    assert executor.calls == []


def test_expired_subscription_authorization_cannot_create_external_alert(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = RecordingSubscriptionExecutor()
    service = build(tmp_path, clock, executor)
    proposal = service.propose_job_alert(JobAlertCandidate(
        source="Indeed",
        source_url="https://www.indeed.com/",
        expected_coverage="Computer vision roles across Switzerland",
        query="Computer Vision",
        location="Switzerland",
    ))
    telegram = WatchlistTelegramHandler(service)
    callback = telegram.subscription_callback(
        proposal,
        intended_actor=ACTOR,
        intended_chat_id=CHAT_ID,
        ttl=timedelta(minutes=1),
    )
    clock.advance(minutes=2)

    result = telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )

    assert result.status == "expired"
    assert executor.calls == []


def test_telegram_callbacks_fit_callback_data_limit(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    telegram = WatchlistTelegramHandler(service)
    company_proposal = service.propose_companies([company(1)])[0]
    alert_proposal = service.propose_job_alert(JobAlertCandidate(
        source="LinkedIn",
        source_url="https://www.linkedin.com/jobs/",
        expected_coverage="English AI research roles in Zurich",
        query="AI Research Scientist",
        location="Zurich",
    ))

    assert len(telegram.company_callback(
        company_proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    ).encode("utf-8")) <= 64
    assert len(telegram.subscription_callback(
        alert_proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    ).encode("utf-8")) <= 64


def test_failed_subscription_is_safely_reported_and_not_repeated(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = FailingSubscriptionExecutor()
    service = build(tmp_path, clock, executor)
    proposal = service.propose_job_alert(JobAlertCandidate(
        source="Indeed",
        source_url="https://www.indeed.com/",
        expected_coverage="Computer vision roles across Switzerland",
        query="Computer Vision",
        location="Switzerland",
    ))
    telegram = WatchlistTelegramHandler(service)
    callback = telegram.subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )

    report = telegram.handle_callback(callback, actor=ACTOR, chat_id=CHAT_ID)
    replay = telegram.handle_callback(callback, actor=ACTOR, chat_id=CHAT_ID)

    assert report.status == "failed"
    assert report.error_type == "SubscriptionDefinitiveError"
    assert "secret detail" not in repr(report)
    assert replay == report
    assert len(executor.calls) == 1


def test_failed_alert_can_be_reproposed_but_requires_a_fresh_confirmation(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = FailingSubscriptionExecutor()
    service = build(tmp_path, clock, executor)
    alert = JobAlertCandidate(
        source="Indeed",
        source_url="https://www.indeed.com/",
        expected_coverage="Computer vision roles across Switzerland",
        query="Computer Vision",
        location="Switzerland",
    )
    telegram = WatchlistTelegramHandler(service)
    first = service.propose_job_alert(alert)
    first_report = telegram.handle_callback(
        telegram.subscription_callback(
            first, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    )

    reopened = service.propose_job_alert(alert)
    same_pending = service.propose_job_alert(alert)

    assert first_report.status == "failed"
    assert reopened.proposal_id != first.proposal_id
    assert same_pending == reopened
    assert len(executor.calls) == 1

    second_report = telegram.handle_callback(
        telegram.subscription_callback(
            reopened, intended_actor=ACTOR, intended_chat_id=CHAT_ID
        ),
        actor=ACTOR,
        chat_id=CHAT_ID,
    )

    assert second_report.status == "failed"
    assert second_report.idempotency_key == first_report.idempotency_key
    assert len(executor.calls) == 2


def test_ambiguous_subscription_outcome_is_not_retried_until_reconciled(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = UncertainSubscriptionExecutor()
    service = build(tmp_path, clock, executor)
    alert = JobAlertCandidate(
        source="Indeed",
        source_url="https://www.indeed.com/",
        expected_coverage="Computer vision roles across Switzerland",
        query="Computer Vision",
        location="Switzerland",
    )
    telegram = WatchlistTelegramHandler(service)
    proposal = service.propose_job_alert(alert)
    callback = telegram.subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )

    uncertain = telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )
    still_same = service.propose_job_alert(alert)
    replay = telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )

    assert uncertain.status == "uncertain"
    assert still_same == proposal
    assert replay == uncertain
    assert len(executor.calls) == 1

    service.reconcile_subscription(
        proposal.proposal_id, subscribed=False
    )
    reopened = service.propose_job_alert(alert)

    assert reopened.proposal_id != proposal.proposal_id


def test_missing_or_unknown_subscription_outcome_is_never_completed(tmp_path):
    class OutcomeExecutor(RecordingSubscriptionExecutor):
        def __init__(self, outcome):
            super().__init__()
            self.outcome = outcome

        def subscribe(self, alert, *, idempotency_key: str):
            self.calls.append((alert, idempotency_key))
            return self.outcome

    for index, outcome in enumerate(({}, {"status": "mystery"}), start=30):
        clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
        service = build(tmp_path / str(index), clock, OutcomeExecutor(outcome))
        proposal = service.propose_job_alert(JobAlertCandidate(
            source="Indeed",
            source_url="https://indeed.com/jobs",
            expected_coverage="AI roles",
            query="AI",
            location="Switzerland",
        ))
        report = WatchlistTelegramHandler(service).handle_callback(
            WatchlistTelegramHandler(service).subscription_callback(
                proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
            ),
            actor=ACTOR,
            chat_id=CHAT_ID,
        )
        assert report.status == "uncertain"
        assert report.error_type == "InvalidSubscriptionOutcome"


def test_concurrent_subscription_replay_stays_in_progress_without_duplicate(
    tmp_path,
):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = BlockingSubscriptionExecutor()
    first = build(tmp_path, clock, executor)
    second = build(tmp_path, clock, executor)
    alert = JobAlertCandidate(
        source="Indeed",
        source_url="https://indeed.com/jobs",
        expected_coverage="AI roles",
        query="AI",
        location="Switzerland",
    )
    proposal = first.propose_job_alert(alert)
    callback = WatchlistTelegramHandler(first).subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            WatchlistTelegramHandler(first).handle_callback(
                callback, actor=ACTOR, chat_id=CHAT_ID
            )
        )
    )
    worker.start()
    assert executor.started.wait(timeout=2)

    replay = WatchlistTelegramHandler(second).handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )
    assert replay.status == "in_progress"
    second_callback = WatchlistTelegramHandler(second).subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )
    second_replay = WatchlistTelegramHandler(second).handle_callback(
        second_callback, actor=ACTOR, chat_id=CHAT_ID
    )
    assert second_replay.status == "in_progress"
    assert len(executor.calls) == 1
    assert second.propose_job_alert(alert) == proposal
    try:
        second.reconcile_subscription(proposal.proposal_id, subscribed=False)
    except ValueError as exc:
        assert "active" in str(exc).casefold()
    else:
        raise AssertionError("an active subscription lease cannot be reconciled")

    executor.release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0].status == "subscribed"
    assert WatchlistTelegramHandler(second).handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    ) == results[0]
    assert len(executor.calls) == 1


def test_expired_subscription_lease_cannot_overlap_a_live_external_attempt(
    tmp_path,
):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    executor = BlockingSubscriptionExecutor()
    first = build(tmp_path, clock, executor)
    second = build(tmp_path, clock, executor)
    alert = JobAlertCandidate(
        source="Indeed",
        source_url="https://indeed.com/jobs",
        expected_coverage="AI roles",
        query="AI",
        location="Switzerland",
    )
    proposal = first.propose_job_alert(alert)
    callback = WatchlistTelegramHandler(first).subscription_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            WatchlistTelegramHandler(first).handle_callback(
                callback, actor=ACTOR, chat_id=CHAT_ID
            )
        )
    )
    worker.start()
    assert executor.started.wait(timeout=2)
    clock.advance(minutes=6)

    replay = WatchlistTelegramHandler(second).handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    )
    assert replay.status == "in_progress"
    try:
        second.reconcile_subscription(proposal.proposal_id, subscribed=False)
    except ValueError as exc:
        assert "active" in str(exc).casefold()
    else:
        raise AssertionError("a live external attempt cannot be reconciled")
    assert second.propose_job_alert(alert) == proposal
    assert len(executor.calls) == 1

    executor.release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0].status == "subscribed"
    stored = second._store.load()["alert_proposals"][proposal.proposal_id]
    assert stored["report"] == results[0].to_dict()
    assert len(executor.calls) == 1


def test_expired_subscription_lease_is_fenced_across_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    alert = JobAlertCandidate(
        source="Indeed",
        source_url="https://indeed.com/jobs",
        expected_coverage="AI roles",
        query="AI",
        location="Switzerland",
    )
    proposal = service.propose_job_alert(alert)
    callback = WatchlistTelegramHandler(service).subscription_callback(
        proposal,
        intended_actor=ACTOR,
        intended_chat_id=CHAT_ID,
    )
    started = context.Event()
    release = context.Event()
    process = context.Process(
        target=_run_blocked_subscription_process,
        args=(
            str(tmp_path / "watchlist.json"),
            clock.now().isoformat(),
            callback,
            started,
            release,
        ),
    )
    process.start()
    assert started.wait(timeout=2)
    clock.advance(minutes=6)

    restarted = build(tmp_path, clock)
    replay = WatchlistTelegramHandler(restarted).handle_callback(
        callback,
        actor=ACTOR,
        chat_id=CHAT_ID,
    )
    assert replay.status == "in_progress"
    try:
        restarted.reconcile_subscription(proposal.proposal_id, subscribed=False)
    except ValueError as exc:
        assert "active" in str(exc).casefold()
    else:
        raise AssertionError("a process-held external attempt must remain fenced")

    release.set()
    process.join(timeout=3)
    assert process.exitcode == 0
    completed = WatchlistTelegramHandler(restarted).handle_callback(
        callback,
        actor=ACTOR,
        chat_id=CHAT_ID,
    )
    assert completed.status == "subscribed"


def test_crashed_subscription_process_recovers_as_uncertain(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    proposal = service.propose_job_alert(JobAlertCandidate(
        source="Indeed",
        source_url="https://indeed.com/jobs",
        expected_coverage="AI roles",
        query="AI",
        location="Switzerland",
    ))
    callback = WatchlistTelegramHandler(service).subscription_callback(
        proposal,
        intended_actor=ACTOR,
        intended_chat_id=CHAT_ID,
    )
    script = """
from datetime import datetime
import os
from pathlib import Path
import sys
from watchlist_service import WatchlistService
from watchlist_store import JsonWatchlistStore
from watchlist_telegram import WatchlistTelegramHandler

class Clock:
    def now(self):
        return datetime.fromisoformat(sys.argv[3])

class CrashAfterIntent:
    def subscribe(self, alert, *, idempotency_key):
        os._exit(17)

service = WatchlistService(
    store=JsonWatchlistStore(Path(sys.argv[1])),
    clock=Clock(),
    subscription_executor=CrashAfterIntent(),
)
WatchlistTelegramHandler(service).handle_callback(
    sys.argv[2], actor="synthetic-owner", chat_id="telegram-chat-42"
)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "watchlist.json"),
            callback,
            clock.now().isoformat(),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    assert crashed.returncode == 17
    clock.advance(minutes=6)
    executor = RecordingSubscriptionExecutor()
    restarted = build(tmp_path, clock, executor)

    recovered = WatchlistTelegramHandler(restarted).handle_callback(
        callback,
        actor=ACTOR,
        chat_id=CHAT_ID,
    )

    assert recovered.status == "uncertain"
    assert executor.calls == []
    reconciled = restarted.reconcile_subscription(
        proposal.proposal_id,
        subscribed=True,
        external_reference="verified-alert",
    )
    assert reconciled.status == "subscribed"


def test_callback_is_short_lived_and_scoped_without_consuming_on_mismatch(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    telegram = WatchlistTelegramHandler(service)
    proposal = service.propose_companies([company(1)])[0]
    callback = telegram.company_callback(
        proposal,
        intended_actor=ACTOR,
        intended_chat_id=CHAT_ID,
        ttl=timedelta(minutes=30),
    )

    assert telegram.handle_callback(
        callback, actor="mallory", chat_id=CHAT_ID
    ).status == "mismatched"
    assert telegram.handle_callback(
        callback, actor=ACTOR, chat_id="wrong-chat"
    ).status == "mismatched"
    assert telegram.handle_callback(
        callback, actor=ACTOR, chat_id=CHAT_ID
    ).status == "monitoring_activated"

    expired = telegram.company_callback(
        proposal,
        intended_actor=ACTOR,
        intended_chat_id=CHAT_ID,
        ttl=timedelta(minutes=30),
    )
    clock.advance(minutes=31)

    assert telegram.handle_callback(
        expired, actor=ACTOR, chat_id=CHAT_ID
    ).status == "expired"


def test_authorizations_are_random_one_time_records(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 16, 12, tzinfo=timezone.utc))
    service = build(tmp_path, clock)
    telegram = WatchlistTelegramHandler(service)
    proposal = service.propose_companies([company(1)])[0]

    first = telegram.company_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )
    second = telegram.company_callback(
        proposal, intended_actor=ACTOR, intended_chat_id=CHAT_ID
    )

    assert first != second
    assert telegram.handle_callback(
        first, actor=ACTOR, chat_id=CHAT_ID
    ).status == "monitoring_activated"
    assert telegram.handle_callback(
        first, actor=ACTOR, chat_id=CHAT_ID
    ).status == "replayed"
    assert telegram.handle_callback(
        second, actor=ACTOR, chat_id=CHAT_ID
    ).status == "replayed"
