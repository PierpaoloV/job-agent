from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discovery_schedule import (
    DiscoverySchedule,
    DiscoveryTelegramHandler,
    FileDiscoveryScheduleStore,
    SchedulePolicy,
)


class AdjustableClock:
    def __init__(self, value: datetime) -> None:
        self.current = value

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


class RecordingNotifier:
    def __init__(self) -> None:
        self.digests: list[tuple[str, list[dict], int]] = []
        self.alerts: list[tuple[str, dict, str]] = []

    def send_digest(
        self,
        jobs,
        *,
        overflow_count: int,
        batch_id: str,
        idempotency_key: str,
        before_send=None,
    ) -> None:
        if before_send is not None:
            before_send()
        self.digests.append((idempotency_key, list(jobs), overflow_count))

    def send_alert(
        self,
        job,
        *,
        reason: str,
        idempotency_key: str,
        before_send=None,
    ) -> None:
        if before_send is not None:
            before_send()
        self.alerts.append((idempotency_key, dict(job), reason))


class FailsOnceNotifier(RecordingNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.failed_key: str | None = None

    def send_alert(
        self,
        job,
        *,
        reason: str,
        idempotency_key: str,
        before_send=None,
    ) -> None:
        if self.failed_key is None:
            self.failed_key = idempotency_key
            raise RuntimeError("temporary transport failure")
        super().send_alert(
            job,
            reason=reason,
            idempotency_key=idempotency_key,
            before_send=before_send,
        )


def role(index: int, *, score: float | None = None, **changes) -> dict:
    value = {
        "stable_id": f"role-{index}",
        "official_vacancy_version": "v1",
        "title": f"Role {index}",
        "company": "Example",
        "score": score if score is not None else (100 - index) / 100,
        "top_tier": {"value": False, "explanation": "not exceptional"},
        "compensation": {"status": "unknown"},
    }
    value.update(changes)
    return value


def build(tmp_path: Path, clock: AdjustableClock, notifier: RecordingNotifier):
    return DiscoverySchedule(
        store=FileDiscoveryScheduleStore(tmp_path / "schedule.json"),
        notifier=notifier,
        clock=clock,
        policy=SchedulePolicy(
            digest_every=timedelta(days=3),
            imminent_deadline=timedelta(hours=36),
            digest_limit=10,
            anchor=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
    )


def test_three_day_digest_is_ranked_limited_and_overflow_is_read_only(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 3, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    schedule = build(tmp_path, clock, notifier)

    first = schedule.process([role(index) for index in range(12)])

    assert first.digest_sent is False
    clock.advance(days=1)
    result = schedule.process([])
    assert result.digest_sent is True
    assert [item["stable_id"] for item in notifier.digests[0][1]] == [
        f"role-{index}" for index in range(10)
    ]
    assert notifier.digests[0][2] == 2

    before = (tmp_path / "schedule.json").read_text(encoding="utf-8")
    callback = DiscoveryTelegramHandler.callback_data(result.batch_id)
    assert [
        item["stable_id"]
        for item in DiscoveryTelegramHandler(schedule).handle_callback_data(callback)
    ] == [
        "role-10",
        "role-11",
    ]
    assert (tmp_path / "schedule.json").read_text(encoding="utf-8") == before


def test_retries_and_restart_do_not_duplicate_digest_or_urgent_alerts(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 4, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    top = role(1, top_tier={"value": True, "explanation": "exceptional"})

    first = build(tmp_path, clock, notifier).process([top])
    second = build(tmp_path, clock, notifier).process([top])

    assert first.digest_sent is True
    assert second.digest_sent is False
    assert len(notifier.digests) == 1
    assert len(notifier.alerts) == 1


def test_repeat_before_due_date_does_not_remove_role_from_pending_digest(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    schedule = build(tmp_path, clock, notifier)

    schedule.process([role(1)])
    clock.advance(days=1)
    schedule.process([role(1)])
    clock.advance(days=1)
    schedule.process([])

    assert [item["stable_id"] for item in notifier.digests[0][1]] == ["role-1"]


def test_pending_alert_retries_with_same_delivery_key_after_transport_failure(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = FailsOnceNotifier()
    top = role(1, top_tier=True)

    with pytest.raises(RuntimeError, match="temporary transport failure"):
        build(tmp_path, clock, notifier).process([top])
    build(tmp_path, clock, notifier).process([])

    assert notifier.alerts[0][0] == notifier.failed_key


def test_top_tier_alert_does_not_depend_on_compensation(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    unknown_cash = role(
        1,
        top_tier={"value": True, "explanation": "exceptional fit"},
        compensation={"status": "unknown", "base_cash": None},
    )

    build(tmp_path, clock, notifier).process([unknown_cash])

    assert [(item[1]["stable_id"], item[2]) for item in notifier.alerts] == [
        ("role-1", "top_tier")
    ]


def test_imminent_deadline_alert_uses_controllable_clock(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    deadline = role(1, application_deadline="2026-07-04T00:00:00+00:00")
    schedule = build(tmp_path, clock, notifier)

    schedule.process([deadline])
    assert notifier.alerts == []

    clock.advance(hours=4)
    schedule.process([])
    assert [(item[1]["stable_id"], item[2]) for item in notifier.alerts] == [
        ("role-1", "imminent_deadline")
    ]


def test_expired_deadline_never_generates_an_alert(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 5, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()

    build(tmp_path, clock, notifier).process(
        [role(1, application_deadline="2026-07-04T00:00:00+00:00")]
    )

    assert notifier.alerts == []


def test_new_official_version_can_alert_again_but_same_version_cannot(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    schedule = build(tmp_path, clock, notifier)

    schedule.process([role(1, top_tier=True)])
    schedule.process([role(1, top_tier=True)])
    schedule.process([role(1, top_tier=True, official_vacancy_version="v2")])

    assert len(notifier.alerts) == 2


def test_store_rejects_incompatible_state_version(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"version": "job-agent.discovery-schedule.v999"}))

    with pytest.raises(ValueError, match="Unsupported discovery schedule version"):
        FileDiscoveryScheduleStore(path).load()


def test_schedule_state_excludes_candidate_and_professional_evidence(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    sensitive = role(
        1,
        candidate_profile={"email": "private@example.test"},
        requirements_evidence_matrix={"rows": [{"evidence_ids": ["secret"]}]},
    )

    build(tmp_path, clock, notifier).process([sensitive])

    serialized = (tmp_path / "schedule.json").read_text(encoding="utf-8")
    assert "candidate_profile" not in serialized
    assert "requirements_evidence_matrix" not in serialized
    assert "private@example.test" not in serialized


def test_schedule_can_stage_for_a_local_interactive_dispatch(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 4, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    schedule = build(tmp_path, clock, notifier)

    staged = schedule.stage(
        [role(1, top_tier={"value": True, "explanation": "exceptional"})]
    )

    assert staged.digest_sent is False
    assert staged.urgent_alerts_sent == 0
    assert notifier.alerts == []
    assert notifier.digests == []
    assert {
        event["status"]
        for event in FileDiscoveryScheduleStore(
            tmp_path / "schedule.json"
        ).load()["outbox"].values()
    } == {"pending"}

    delivered = schedule.dispatch_pending()

    assert delivered.digest_sent is True
    assert delivered.urgent_alerts_sent == 1
    assert len(notifier.alerts) == 1
    assert len(notifier.digests) == 1


def test_staging_prunes_a_persisted_role_with_mismatched_ats_company(tmp_path):
    clock = AdjustableClock(datetime(2026, 7, 2, 8, tzinfo=timezone.utc))
    notifier = RecordingNotifier()
    store = FileDiscoveryScheduleStore(tmp_path / "schedule.json")
    identity = "linkedin:4399398799@bad-version"
    store.save(
        {
            "version": "job-agent.discovery-schedule.v1",
            "last_digest_slot": 0,
            "known_versions": [identity],
            "roles": {
                identity: {
                    "first_seen_at": clock.now().isoformat(),
                    "digest_pending": True,
                    "job": role(
                        1,
                        stable_id="linkedin:4399398799",
                        official_vacancy_version="bad-version",
                        company="Rivia",
                        official_url="https://jobs.lever.co/rivr/robotics-role",
                    ),
                }
            },
            "batches": {},
            "outbox": {},
        }
    )

    build(tmp_path, clock, notifier).stage([])

    state = store.load()
    assert identity not in state["roles"]
    assert identity not in state["known_versions"]
