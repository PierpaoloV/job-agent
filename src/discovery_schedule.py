"""Durable cadence and notification policy for graded opportunities.

The scheduler is transport-neutral.  It persists deterministic delivery keys
and passes them to the notification adapter so both process restarts and a
transport retry can converge on one visible Telegram message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from search_official_source import is_official_company_url


SCHEDULE_STATE_VERSION = "job-agent.discovery-schedule.v1"
INTERACTIVE_DELIVERY_FORMAT = "interactive-v1"


class BatchNotYetPublished(LookupError):
    """The callback batch is not present in the synchronized state yet."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class DiscoveryNotifier(Protocol):
    def send_digest(
        self,
        jobs: Sequence[dict[str, Any]],
        *,
        overflow_count: int,
        batch_id: str,
        idempotency_key: str,
        before_send=None,
    ) -> None: ...

    def send_alert(
        self,
        job: Mapping[str, Any],
        *,
        reason: str,
        idempotency_key: str,
        before_send=None,
    ) -> None: ...


class OverflowTransport(Protocol):
    def send_overflow(
        self, jobs: Sequence[dict[str, Any]], *, batch_id: str
    ) -> None: ...


@dataclass(frozen=True)
class SchedulePolicy:
    digest_every: timedelta = timedelta(days=3)
    imminent_deadline: timedelta = timedelta(hours=36)
    digest_limit: int = 10
    anchor: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __post_init__(self) -> None:
        if self.digest_every <= timedelta(0):
            raise ValueError("digest cadence must be positive")
        if self.imminent_deadline < timedelta(0):
            raise ValueError("deadline window cannot be negative")
        if self.digest_limit < 1:
            raise ValueError("digest limit must be positive")
        if self.anchor.tzinfo is None:
            raise ValueError("digest anchor must be timezone-aware")


@dataclass(frozen=True)
class ScheduleResult:
    digest_sent: bool
    batch_id: str | None = None
    urgent_alerts_sent: int = 0


class FileDiscoveryScheduleStore:
    """Atomic JSON persistence containing only public graded-role data."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_state()
        value = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Discovery schedule state must be an object")
        if value.get("version") != SCHEDULE_STATE_VERSION:
            raise ValueError(
                f"Unsupported discovery schedule version: {value.get('version')}"
            )
        return value

    def save(self, state: Mapping[str, Any]) -> None:
        if state.get("version") != SCHEDULE_STATE_VERSION:
            raise ValueError("Cannot save an incompatible discovery schedule state")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self._path)


class DiscoverySchedule:
    def __init__(
        self,
        *,
        store: FileDiscoveryScheduleStore,
        notifier: DiscoveryNotifier,
        clock: Clock,
        policy: SchedulePolicy = SchedulePolicy(),
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._policy = policy

    def process(self, graded_jobs: Sequence[Mapping[str, Any]]) -> ScheduleResult:
        self.stage(graded_jobs)
        return self.dispatch_pending()

    def stage(
        self, graded_jobs: Sequence[Mapping[str, Any]]
    ) -> ScheduleResult:
        """Persist due delivery intents without crossing the transport boundary."""

        now = _aware(self._clock.now(), "current time")
        state = self._store.load()
        _discard_unsafe_pending(state)
        self._record_jobs(
            state,
            [job for job in graded_jobs if _safe_for_delivery(job)],
            now,
        )

        self._queue_urgent_alerts(state, now)
        digest = self._queue_digest(state, now)
        self._store.save(state)
        return ScheduleResult(
            digest_sent=False,
            batch_id=(
                None if digest is None else str(digest["batch_id"])
            ),
            urgent_alerts_sent=0,
        )

    def dispatch_pending(self, *, before_send=None) -> ScheduleResult:
        """Deliver previously staged intents through the configured notifier."""

        state = self._store.load()
        _discard_unsafe_pending(state)
        pending_alerts = _pending_events(state, "alert")
        pending_digests = _pending_events(state, "digest")
        sent_urgent = self._dispatch_alerts(
            state, pending_alerts, before_send=before_send
        )
        digest_sent = False
        for pending_digest in pending_digests:
            digest_sent = (
                self._dispatch_digest(
                    state,
                    pending_digest,
                    before_send=before_send,
                )
                or digest_sent
            )
        self._store.save(state)
        batch_id = (
            str(pending_digests[-1]["batch_id"])
            if pending_digests
            else None
        )
        return ScheduleResult(
            digest_sent=digest_sent,
            batch_id=batch_id,
            urgent_alerts_sent=sent_urgent,
        )

    def dispatch_legacy_interactive(self, *, before_send=None) -> int:
        """Reissue legacy delivered events once with role-level controls.

        The local Telegram delivery ledger deduplicates the migration key even
        when a hosted-state refresh later restores the legacy event metadata.
        """

        state = self._store.load()
        migrated = 0
        for event in state.get("outbox", {}).values():
            if (
                event.get("status") != "delivered"
                or event.get("delivery_format") == INTERACTIVE_DELIVERY_FORMAT
            ):
                continue
            key = (
                f"{INTERACTIVE_DELIVERY_FORMAT}:"
                f"{event['idempotency_key']}"
            )
            if event.get("kind") == "alert":
                self._notifier.send_alert(
                    event["job"],
                    reason=str(event["reason"]),
                    idempotency_key=key,
                    before_send=before_send,
                )
            elif event.get("kind") == "digest":
                jobs = event["jobs"]
                self._notifier.send_digest(
                    jobs[: self._policy.digest_limit],
                    overflow_count=max(
                        0, len(jobs) - self._policy.digest_limit
                    ),
                    batch_id=str(event["batch_id"]),
                    idempotency_key=key,
                    before_send=before_send,
                )
            else:
                continue
            event["delivery_format"] = INTERACTIVE_DELIVERY_FORMAT
            migrated += 1
        self._store.save(state)
        return migrated

    def remaining(self, batch_id: str | None) -> tuple[dict[str, Any], ...]:
        """Return the overflow snapshot without mutating delivery or role state."""
        if not batch_id:
            return ()
        state = self._store.load()
        batch = state.get("batches", {}).get(batch_id)
        if not isinstance(batch, Mapping):
            raise BatchNotYetPublished(batch_id)
        jobs = batch.get("jobs", [])
        if not isinstance(jobs, list):
            return ()
        return tuple(dict(item) for item in jobs[self._policy.digest_limit :])

    def _record_jobs(
        self,
        state: dict[str, Any],
        jobs: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> None:
        roles = state.setdefault("roles", {})
        identities = state.setdefault("known_versions", [])
        for value in jobs:
            job = _public_json(dict(value))
            stable_id = str(job.get("stable_id") or job.get("dedup_key") or "").strip()
            if not stable_id:
                raise ValueError("A scheduled role requires a stable id")
            official_version = str(job.get("official_vacancy_version") or "unversioned")
            identity = _identity(stable_id, official_version)
            job["stable_id"] = stable_id
            previous = roles.get(identity, {})
            roles[identity] = {
                "job": job,
                "first_seen_at": previous.get("first_seen_at", now.isoformat()),
                "digest_pending": bool(
                    previous.get("digest_pending", identity not in identities)
                ),
            }
            if identity not in identities:
                identities.append(identity)

    def _queue_urgent_alerts(
        self, state: dict[str, Any], now: datetime
    ) -> list[dict[str, Any]]:
        outbox = state.setdefault("outbox", {})
        queued: list[dict[str, Any]] = []
        for identity, record in state.get("roles", {}).items():
            job = record["job"]
            reasons: list[str] = []
            if _is_top_tier(job):
                reasons.append("top_tier")
            if _is_imminent(job.get("application_deadline"), now, self._policy):
                reasons.append("imminent_deadline")
            for reason in reasons:
                event_id = f"alert:{identity}:{reason}"
                if event_id in outbox:
                    continue
                event = {
                    "kind": "alert",
                    "idempotency_key": event_id,
                    "job": job,
                    "reason": reason,
                    "status": "pending",
                }
                outbox[event_id] = event
                queued.append(event)
        return queued

    def _queue_digest(
        self, state: dict[str, Any], now: datetime
    ) -> dict[str, Any] | None:
        current_slot = int((now - self._policy.anchor) // self._policy.digest_every)
        last_slot = int(state.get("last_digest_slot", 0))
        if current_slot <= last_slot:
            return None
        state["last_digest_slot"] = current_slot
        pending = [
            record["job"]
            for record in state.get("roles", {}).values()
            if record.get("digest_pending")
        ]
        if not pending:
            return None
        ranked = sorted(
            pending,
            key=lambda job: (-_score(job), str(job.get("stable_id", ""))),
        )
        batch_id = f"digest-{current_slot}-{_jobs_digest(ranked)}"
        event_id = f"digest:{batch_id}"
        event = {
            "kind": "digest",
            "idempotency_key": event_id,
            "batch_id": batch_id,
            "jobs": ranked,
            "status": "pending",
        }
        state.setdefault("outbox", {}).setdefault(event_id, event)
        state.setdefault("batches", {})[batch_id] = {
            "created_at": now.isoformat(),
            "jobs": ranked,
        }
        pending_ids = {
            _identity(
                str(job["stable_id"]),
                str(job.get("official_vacancy_version") or "unversioned"),
            )
            for job in ranked
        }
        for identity, record in state.get("roles", {}).items():
            if identity in pending_ids:
                record["digest_pending"] = False
        return state["outbox"][event_id]

    def _dispatch_alerts(
        self,
        state: dict[str, Any],
        events: Sequence[dict[str, Any]],
        *,
        before_send=None,
    ) -> int:
        sent = 0
        for event in events:
            if event.get("status") == "delivered":
                continue
            self._notifier.send_alert(
                event["job"],
                reason=str(event["reason"]),
                idempotency_key=str(event["idempotency_key"]),
                before_send=before_send,
            )
            event["status"] = "delivered"
            event["delivery_format"] = INTERACTIVE_DELIVERY_FORMAT
            sent += 1
        return sent

    def _dispatch_digest(
        self,
        state: dict[str, Any],
        event: dict[str, Any] | None,
        *,
        before_send=None,
    ) -> bool:
        if event is None or event.get("status") == "delivered":
            return False
        jobs = event["jobs"]
        self._notifier.send_digest(
            jobs[: self._policy.digest_limit],
            overflow_count=max(0, len(jobs) - self._policy.digest_limit),
            batch_id=str(event["batch_id"]),
            idempotency_key=str(event["idempotency_key"]),
            before_send=before_send,
        )
        event["status"] = "delivered"
        event["delivery_format"] = INTERACTIVE_DELIVERY_FORMAT
        return True


class DiscoveryTelegramHandler:
    """Map the Telegram overflow action to a read-only schedule query."""

    ACTION = "Mostra altre"

    def __init__(
        self, schedule: DiscoverySchedule, transport: OverflowTransport | None = None
    ) -> None:
        self._schedule = schedule
        self._transport = transport

    @staticmethod
    def callback_data(batch_id: str) -> str:
        return f"discovery-overflow:{batch_id}"

    def handle_callback_data(self, callback_data: str) -> tuple[dict[str, Any], ...]:
        prefix = "discovery-overflow:"
        if not callback_data.startswith(prefix):
            raise ValueError("Unsupported discovery callback")
        batch_id = callback_data.removeprefix(prefix)
        jobs = self._schedule.remaining(batch_id)
        if self._transport is not None:
            self._transport.send_overflow(jobs, batch_id=batch_id)
        return jobs


def _empty_state() -> dict[str, Any]:
    return {
        "version": SCHEDULE_STATE_VERSION,
        "last_digest_slot": 0,
        "known_versions": [],
        "roles": {},
        "batches": {},
        "outbox": {},
    }


def _pending_events(state: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    outbox = state.get("outbox", {})
    if not isinstance(outbox, Mapping):
        return []
    return [
        event
        for _, event in sorted(outbox.items())
        if isinstance(event, dict)
        and event.get("kind") == kind
        and event.get("status") == "pending"
    ]


def _safe_for_delivery(job: Mapping[str, Any]) -> bool:
    official_url = str(
        job.get("official_url") or job.get("canonical_url") or ""
    ).strip()
    company = str(job.get("company") or "").strip()
    if not official_url:
        return True
    return bool(company) and is_official_company_url(official_url, company)


def _discard_unsafe_pending(state: dict[str, Any]) -> None:
    roles = state.setdefault("roles", {})
    unsafe_identities = {
        identity
        for identity, record in roles.items()
        if isinstance(record, Mapping)
        and isinstance(record.get("job"), Mapping)
        and not _safe_for_delivery(record["job"])
    }
    for identity in unsafe_identities:
        roles.pop(identity, None)
    known_versions = state.setdefault("known_versions", [])
    state["known_versions"] = [
        identity
        for identity in known_versions
        if identity not in unsafe_identities
    ]

    outbox = state.setdefault("outbox", {})
    for event_id, event in tuple(outbox.items()):
        if (
            not isinstance(event, Mapping)
            or event.get("status") != "pending"
        ):
            continue
        if event.get("kind") == "alert":
            job = event.get("job")
            if isinstance(job, Mapping) and not _safe_for_delivery(job):
                outbox.pop(event_id, None)
        elif event.get("kind") == "digest":
            jobs = event.get("jobs", [])
            if not isinstance(jobs, list):
                continue
            safe_jobs = [
                job
                for job in jobs
                if isinstance(job, Mapping) and _safe_for_delivery(job)
            ]
            if not safe_jobs:
                outbox.pop(event_id, None)
                state.setdefault("batches", {}).pop(
                    str(event.get("batch_id", "")),
                    None,
                )
                continue
            event["jobs"] = safe_jobs
            batch = state.setdefault("batches", {}).get(
                str(event.get("batch_id", ""))
            )
            if isinstance(batch, dict):
                batch["jobs"] = safe_jobs


def _identity(stable_id: str, official_version: str) -> str:
    return f"{stable_id}@{official_version}"


def _score(job: Mapping[str, Any]) -> float:
    try:
        return float(job.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _is_top_tier(job: Mapping[str, Any]) -> bool:
    value = job.get("top_tier", False)
    if isinstance(value, Mapping):
        return value.get("value") is True
    return value is True


def _is_imminent(value: Any, now: datetime, policy: SchedulePolicy) -> bool:
    if value is None or not str(value).strip():
        return False
    raw = str(value).strip()
    try:
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    remaining = deadline.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return timedelta(0) <= remaining <= policy.imminent_deadline


def _jobs_digest(jobs: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(jobs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _public_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        forbidden = {
            "candidate",
            "candidate_profile",
            "ats_answers",
            "professional_evidence",
            "requirements_evidence_matrix",
            "health",
            "demographic",
            "credential",
            "password",
            "token",
        }
        return {
            str(key): _public_json(item)
            for key, item in value.items()
            if str(key).casefold().replace("-", "_") not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [_public_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


__all__ = [
    "DiscoverySchedule",
    "DiscoveryTelegramHandler",
    "BatchNotYetPublished",
    "DiscoveryNotifier",
    "FileDiscoveryScheduleStore",
    "SCHEDULE_STATE_VERSION",
    "SchedulePolicy",
    "ScheduleResult",
]
