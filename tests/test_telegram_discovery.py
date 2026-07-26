from __future__ import annotations

import json
from pathlib import Path
import pytest
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_schedule import (
    DiscoverySchedule,
    DiscoveryTelegramHandler,
    FileDiscoveryScheduleStore,
)
from telegram_delivery import TelegramDeliveryLedger
import telegram_discovery
from telegram_discovery import TelegramBotApi, TelegramUpdateConsumer


class NeverUsedNotifier:
    def send_digest(self, jobs, **kwargs):
        raise AssertionError("callback must not run scheduling")

    def send_alert(self, job, **kwargs):
        raise AssertionError("callback must not run scheduling")


class FakeTelegramApi:
    def __init__(self, update):
        self.update = update
        self.overflow = []
        self.acknowledged = []

    def poll_updates(self, *, offset, timeout):
        return [self.update]

    def send_overflow(self, jobs, *, batch_id):
        self.overflow.append((batch_id, list(jobs)))

    def acknowledge_callback(self, callback_query_id, text):
        self.acknowledged.append((callback_query_id, text))


class FailingOverflowApi(FakeTelegramApi):
    def send_overflow(self, jobs, *, batch_id):
        raise RuntimeError("ambiguous overflow delivery")


def test_callback_consumer_fails_closed_without_repository_identity(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("JOB_AGENT_GITHUB_REPOSITORY", raising=False)

    with pytest.raises(RuntimeError, match="JOB_AGENT_GITHUB_REPOSITORY"):
        telegram_discovery.build_consumer(
            state_path=tmp_path / "schedule.json",
            ledger_path=tmp_path / "callbacks.sqlite",
            github_token="test-token",
        )


def test_production_bot_api_requests_only_callback_updates(monkeypatch):
    captured = {}

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"ok": True, "result": []}

    def fake_get(url, *, params, timeout):
        captured.update({"url": url, "params": params, "timeout": timeout})
        return Response()

    monkeypatch.setattr(telegram_discovery.requests, "get", fake_get)
    api = TelegramBotApi(
        token="test-token",
        overflow_notifier=object(),
    )

    assert api.poll_updates(offset=12, timeout=25) == []
    assert captured["params"] == {
        "timeout": 25,
        "allowed_updates": '["callback_query"]',
        "offset": 12,
    }


def test_production_update_consumer_routes_overflow_without_mutating_schedule(tmp_path):
    state_path = tmp_path / "schedule.json"
    state_path.write_text(
        json.dumps({
            "version": "job-agent.discovery-schedule.v1",
            "last_digest_slot": 1,
            "known_versions": [],
            "roles": {},
            "outbox": {},
            "batches": {
                "digest-1-abc": {
                    "created_at": "2026-07-04T08:00:00+00:00",
                    "jobs": [{"stable_id": f"role-{index}"} for index in range(12)],
                }
            },
        }),
        encoding="utf-8",
    )
    schedule = DiscoverySchedule(
        store=FileDiscoveryScheduleStore(state_path),
        notifier=NeverUsedNotifier(),
        clock=object(),
    )
    update = {
        "update_id": 88,
        "callback_query": {
            "id": "callback-1",
            "data": DiscoveryTelegramHandler.callback_data("digest-1-abc"),
        },
    }
    api = FakeTelegramApi(update)
    before = state_path.read_bytes()
    consumer = TelegramUpdateConsumer(
        schedule=schedule,
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "updates.json"),
    )

    consumer.consume_once()
    consumer.consume_once()

    assert [item["stable_id"] for item in api.overflow[0][1]] == [
        "role-10",
        "role-11",
    ]
    assert len(api.overflow) == 1
    assert state_path.read_bytes() == before
    assert api.acknowledged == [
        ("callback-1", "2 opportunità mostrate"),
        ("callback-1", "Azione già completata"),
    ]


def test_consumer_syncs_remote_batch_before_callback_lookup(tmp_path):
    state_path = tmp_path / "schedule.json"
    state_path.write_text(json.dumps({
        "version": "job-agent.discovery-schedule.v1",
        "last_digest_slot": 0,
        "known_versions": [],
        "roles": {},
        "outbox": {},
        "batches": {},
    }))
    authoritative = {
        "version": "job-agent.discovery-schedule.v1",
        "last_digest_slot": 1,
        "known_versions": [],
        "roles": {},
        "outbox": {},
        "batches": {
            "digest-1-abc": {
                "created_at": "2026-07-04T08:00:00+00:00",
                "jobs": [{"stable_id": f"role-{index}"} for index in range(11)],
            }
        },
    }
    schedule = DiscoverySchedule(
        store=FileDiscoveryScheduleStore(state_path),
        notifier=NeverUsedNotifier(),
        clock=object(),
    )
    api = FakeTelegramApi({
        "update_id": 89,
        "callback_query": {
            "id": "callback-2",
            "data": DiscoveryTelegramHandler.callback_data("digest-1-abc"),
        },
    })

    TelegramUpdateConsumer(
        schedule=schedule,
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "updates.sqlite"),
        state_sync=lambda: state_path.write_text(json.dumps(authoritative)),
    ).consume_once()

    assert [item["stable_id"] for item in api.overflow[0][1]] == ["role-10"]
    assert json.loads(state_path.read_text()) == authoritative


def test_failed_callback_effect_becomes_uncertain_instead_of_replaying(tmp_path):
    state_path = tmp_path / "schedule.json"
    state_path.write_text(json.dumps({
        "version": "job-agent.discovery-schedule.v1",
        "last_digest_slot": 1,
        "known_versions": [],
        "roles": {},
        "outbox": {},
        "batches": {"digest-1-abc": {"jobs": [{"stable_id": "role-10"}]}},
    }))
    update = {
        "update_id": 90,
        "callback_query": {
            "id": "callback-3",
            "data": DiscoveryTelegramHandler.callback_data("digest-1-abc"),
        },
    }
    api = FailingOverflowApi(update)
    consumer = TelegramUpdateConsumer(
        schedule=DiscoverySchedule(
            store=FileDiscoveryScheduleStore(state_path),
            notifier=NeverUsedNotifier(),
            clock=object(),
        ),
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "updates.sqlite"),
    )

    try:
        consumer.consume_once()
    except RuntimeError:
        pass
    else:
        raise AssertionError("ambiguous callback effect was swallowed")
    consumer.consume_once()

    assert api.acknowledged == [
        ("callback-3", "Esito incerto: riprova il pulsante"),
        ("callback-3", "Esito incerto: riprova il pulsante"),
    ]


def test_missing_remote_batch_stays_retryable_until_state_is_published(tmp_path):
    state_path = tmp_path / "schedule.json"
    empty = {
        "version": "job-agent.discovery-schedule.v1",
        "last_digest_slot": 1,
        "known_versions": [],
        "roles": {},
        "outbox": {},
        "batches": {},
    }
    state_path.write_text(json.dumps(empty))
    update = {
        "update_id": 91,
        "callback_query": {
            "id": "callback-4",
            "data": DiscoveryTelegramHandler.callback_data("digest-1-later"),
        },
    }
    api = FakeTelegramApi(update)
    consumer = TelegramUpdateConsumer(
        schedule=DiscoverySchedule(
            store=FileDiscoveryScheduleStore(state_path),
            notifier=NeverUsedNotifier(),
            clock=object(),
        ),
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "updates.sqlite"),
    )

    consumer.consume_once()
    published = dict(empty)
    published["batches"] = {
        "digest-1-later": {"jobs": [{"stable_id": f"role-{i}"} for i in range(11)]}
    }
    state_path.write_text(json.dumps(published))
    # A worker restart before the next offset-confirming poll can receive the
    # same update again. The waiting state deliberately permits that retry.
    TelegramUpdateConsumer(
        schedule=DiscoverySchedule(
            store=FileDiscoveryScheduleStore(state_path),
            notifier=NeverUsedNotifier(),
            clock=object(),
        ),
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "updates.sqlite"),
    ).consume_once()

    assert [item["stable_id"] for item in api.overflow[0][1]] == ["role-10"]
    assert api.acknowledged == [
        ("callback-4", "Stato in pubblicazione: riprova il pulsante"),
        ("callback-4", "1 opportunità mostrate"),
    ]
