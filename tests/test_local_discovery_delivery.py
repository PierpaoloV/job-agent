from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_schedule import (
    DiscoverySchedule,
    FileDiscoveryScheduleStore,
    SchedulePolicy,
)
from local_discovery_delivery import DiscoveryNotificationCapability


class Clock:
    def __init__(self):
        self.current = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)

    def now(self):
        return self.current


class Notifier:
    def __init__(self):
        self.alerts = []

    def send_alert(self, job, *, reason, idempotency_key, before_send=None):
        if before_send is not None:
            before_send()
        self.alerts.append((job["stable_id"], reason, idempotency_key))

    def send_digest(self, jobs, *, before_send=None, **kwargs):
        if before_send is not None:
            before_send()
        return None


class Execution:
    def __init__(self):
        self.checkpoints = []

    def checkpoint(self, *, external_action=True):
        self.checkpoints.append(external_action)


def test_local_capability_syncs_then_delivers_staged_interactive_alert(tmp_path):
    clock = Clock()
    notifier = Notifier()
    state_path = tmp_path / "schedule.json"
    schedule = DiscoverySchedule(
        store=FileDiscoveryScheduleStore(state_path),
        notifier=notifier,
        clock=clock,
        policy=SchedulePolicy(
            anchor=datetime(2026, 7, 26, 8, tzinfo=timezone.utc),
            digest_every=timedelta(days=3),
        ),
    )
    synced = []

    def sync():
        synced.append("state")
        schedule.stage(
            [
                {
                    "stable_id": "example:42",
                    "official_vacancy_version": "sha256:" + "a" * 64,
                    "top_tier": {"value": True},
                }
            ]
        )

    capability = DiscoveryNotificationCapability(
        state_sync=sync,
        schedule=schedule,
    )
    execution = Execution()

    capability.run_once(execution)

    assert synced == ["state"]
    assert notifier.alerts[0][:2] == ("example:42", "top_tier")
    assert execution.checkpoints == [True, True]
    assert capability.status() == {"state": "ready", "healthy": True}

    capability.run_once(execution)

    assert synced == ["state"]
    assert len(notifier.alerts) == 1
    assert execution.checkpoints == [True, True]


def test_local_capability_migrates_legacy_delivered_alert_once(tmp_path):
    clock = Clock()
    notifier = Notifier()
    state_path = tmp_path / "schedule.json"
    state_path.write_text(
        json.dumps(
            {
                "version": "job-agent.discovery-schedule.v1",
                "known_versions": ["example:42@v1"],
                "roles": {},
                "batches": {},
                "last_digest_slot": 0,
                "outbox": {
                    "alert:example:42@v1:top_tier": {
                        "kind": "alert",
                        "idempotency_key": "alert:example:42@v1:top_tier",
                        "job": {
                            "stable_id": "example:42",
                            "official_vacancy_version": "v1",
                        },
                        "reason": "top_tier",
                        "status": "delivered",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    schedule = DiscoverySchedule(
        store=FileDiscoveryScheduleStore(state_path),
        notifier=notifier,
        clock=clock,
    )
    capability = DiscoveryNotificationCapability(
        state_sync=lambda: True,
        schedule=schedule,
    )
    execution = Execution()

    capability.run_once(execution)
    capability.run_once(execution)

    assert notifier.alerts == [
        (
            "example:42",
            "top_tier",
            "interactive-v1:alert:example:42@v1:top_tier",
        )
    ]
    saved_event = next(
        iter(
            json.loads(state_path.read_text(encoding="utf-8"))["outbox"].values()
        )
    )
    assert saved_event["delivery_format"] == "interactive-v1"
