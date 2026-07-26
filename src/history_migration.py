"""Idempotent import of legacy discovery and application history.

The import keeps the old SQLite database read-only.  It projects seen rows into
the opportunity store and applied/known records into the local application
store.  Existing equal projections are counted as already present; a different
record with the same identity fails closed instead of overwriting history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Protocol, Sequence
from zoneinfo import ZoneInfo

from application_domain import ApplicationSnapshot, LifecycleEvent, LifecycleState
from opportunity_domain import OpportunityRecord
from opportunity_sources import OpportunityLead
from role_identity import RoleIdentity, canonical_role_identity


_STATUS_LIFECYCLES = {
    "applied": LifecycleState.SUBMITTED,
    "submitted": LifecycleState.SUBMITTED,
    "awaiting_response": LifecycleState.SUBMITTED,
    "interview": LifecycleState.INTERVIEW,
    "rejected": LifecycleState.REJECTED,
}
HISTORY_IMPORT_VERSION = "job-agent.history-import.v1"


class HistoryMigrationConflict(RuntimeError):
    """A target identity exists with data different from the imported record."""


@dataclass(frozen=True)
class LegacySeenRecord:
    legacy_key: str
    title: str
    company: str
    source: str
    first_seen: str
    score: float | None


@dataclass(frozen=True)
class HistoricalApplicationRecord:
    application_id: str
    source: str
    external_id: str
    company: str
    title: str
    status: str
    status_observed_at: str
    canonical_url: str = ""
    notes: str = ""


@dataclass(frozen=True)
class MigrationReport:
    source_seen: int
    source_applications: int
    imported_seen: int
    imported_applications: int
    already_present_seen: int
    already_present_applications: int


class OpportunityStore(Protocol):
    def save(self, record: OpportunityRecord) -> None: ...

    def load(self, stable_id: str) -> OpportunityRecord: ...

    def list(self) -> tuple[OpportunityRecord, ...]: ...


class ApplicationStore(Protocol):
    def save(self, application: ApplicationSnapshot) -> None: ...

    def load(self, application_id: str) -> ApplicationSnapshot: ...

    def list(self) -> tuple[ApplicationSnapshot, ...]: ...


class LegacySqliteHistorySource:
    """Read the original ``seen`` and ``applied`` tables without mutating them."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def seen(self) -> tuple[LegacySeenRecord, ...]:
        rows = self._rows(
            "SELECT url, title, company, source, first_seen, score "
            "FROM seen ORDER BY url"
        )
        return tuple(
            LegacySeenRecord(
                legacy_key=str(row["url"]),
                title=str(row["title"] or ""),
                company=str(row["company"] or ""),
                source=str(row["source"] or "legacy"),
                first_seen=str(row["first_seen"] or ""),
                score=None if row["score"] is None else float(row["score"]),
            )
            for row in rows
        )

    def applications(self) -> tuple[HistoricalApplicationRecord, ...]:
        rows = self._rows(
            "SELECT url, title, company, applied_at, status, notes "
            "FROM applied ORDER BY url"
        )
        return tuple(
            HistoricalApplicationRecord(
                application_id=f"legacy-application:{_digest(str(row['url']))}",
                source="legacy-agent",
                external_id=str(row["url"]),
                company=str(row["company"] or ""),
                title=str(row["title"] or ""),
                status=str(row["status"] or "applied"),
                status_observed_at=str(row["applied_at"] or ""),
                canonical_url=str(row["url"]),
                notes=str(row["notes"] or ""),
            )
            for row in rows
        )

    def _rows(self, query: str) -> tuple[sqlite3.Row, ...]:
        if not self._path.is_file():
            raise FileNotFoundError(self._path)
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return tuple(connection.execute(query))


class HistoryMigrationService:
    def __init__(
        self,
        *,
        legacy_source: LegacySqliteHistorySource,
        opportunity_store: OpportunityStore,
        application_store: ApplicationStore,
        migrated_at: Callable[[], datetime],
        legacy_timezone: ZoneInfo = ZoneInfo("Europe/Rome"),
    ) -> None:
        self._legacy_source = legacy_source
        self.opportunity_store = opportunity_store
        self.application_store = application_store
        self._migrated_at = migrated_at
        self._legacy_timezone = legacy_timezone

    def migrate(
        self,
        *,
        known_applications: Sequence[HistoricalApplicationRecord],
    ) -> MigrationReport:
        seen_source = self._legacy_source.seen()
        application_source = (
            *self._legacy_source.applications(),
            *tuple(known_applications),
        )
        seen_candidates = tuple(self._seen_projection(item) for item in seen_source)
        application_candidates = tuple(
            self._application_projection(item) for item in application_source
        )
        seen_new, seen_existing = self._classify_opportunities(seen_candidates)
        applications_new, applications_existing = self._classify_applications(
            application_candidates
        )
        for record in seen_new:
            self.opportunity_store.save(record)
        for application in applications_new:
            self.application_store.save(application)
        return MigrationReport(
            source_seen=len(seen_source),
            source_applications=len(application_source),
            imported_seen=len(seen_new),
            imported_applications=len(applications_new),
            already_present_seen=seen_existing,
            already_present_applications=applications_existing,
        )

    def _seen_projection(self, item: LegacySeenRecord) -> OpportunityRecord:
        discovered_at = self._timestamp(item.first_seen)
        score = "unknown" if item.score is None else str(item.score)
        lead = OpportunityLead(
            stable_id=f"legacy-seen:{_digest(item.legacy_key)}",
            source=item.source,
            source_confidence="legacy-import",
            canonical_url=item.legacy_key,
            title=item.title,
            company=item.company,
            location="",
            modality="",
            snippet=f"legacy_local_score={score}",
            email_received_at=None,
            discovered_at=discovered_at,
            published_at=None,
        )
        return OpportunityRecord(lead=lead)

    def _application_projection(
        self, item: HistoricalApplicationRecord
    ) -> ApplicationSnapshot:
        status = item.status.strip().casefold()
        try:
            lifecycle = _STATUS_LIFECYCLES[status]
        except KeyError:
            raise ValueError(
                f"Unsupported historical application status: {item.status}"
            ) from None
        observed_at, timestamp_basis = self._application_timestamp(item)
        stable_identity = item.canonical_url or f"{item.source}:{item.external_id}"
        opportunity = {
            "stable_id": stable_identity,
            "canonical_url": item.canonical_url,
            "official_url": item.canonical_url,
            "official_job_id": item.external_id,
            "company": item.company,
            "title": item.title,
            "source": item.source,
            "historical_status": status,
            "historical_notes": item.notes,
            "status_observed_at": observed_at,
            "historical_timestamp_basis": timestamp_basis,
        }
        version = f"history:{_digest(_canonical_json(opportunity))}"
        return ApplicationSnapshot(
            application_id=item.application_id,
            opportunity=opportunity,
            opportunity_version=version,
            lifecycle_state=lifecycle,
            authorization_version=version,
            history=(LifecycleEvent(lifecycle, observed_at),),
        )

    def _application_timestamp(
        self, item: HistoricalApplicationRecord
    ) -> tuple[str, str]:
        if item.status_observed_at:
            parsed = datetime.fromisoformat(item.status_observed_at)
            basis = (
                "source_timestamp"
                if parsed.tzinfo is not None
                else f"source_local_time_assumed_{self._legacy_timezone.key}"
            )
            return self._timestamp(item.status_observed_at), basis
        try:
            stored = self.application_store.load(item.application_id)
        except KeyError:
            return self._migrated_at().isoformat(), "migration_time_only"
        return stored.history[-1].occurred_at, "migration_time_only"

    def _timestamp(self, value: str) -> str:
        if not value:
            return self._migrated_at().isoformat()
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"Historical timestamp must be ISO-8601: {value}"
            ) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._legacy_timezone)
        return parsed.isoformat()

    def _classify_opportunities(
        self, candidates: tuple[OpportunityRecord, ...]
    ) -> tuple[tuple[OpportunityRecord, ...], int]:
        unique = self._unique(
            candidates,
            key=lambda item: item.lead.stable_id,
            label="seen history",
        )
        new = []
        existing = 0
        for stable_id, candidate in unique.items():
            try:
                stored = self.opportunity_store.load(stable_id)
            except KeyError:
                new.append(candidate)
                continue
            if stored != candidate:
                raise HistoryMigrationConflict(
                    f"Conflicting seen history identity: {stable_id}"
                )
            existing += 1
        return tuple(new), existing

    def _classify_applications(
        self, candidates: tuple[ApplicationSnapshot, ...]
    ) -> tuple[tuple[ApplicationSnapshot, ...], int]:
        unique = self._unique(
            candidates,
            key=lambda item: item.application_id,
            label="application history",
        )
        identities: dict[RoleIdentity, str] = {}
        for candidate in (*self.application_store.list(), *unique.values()):
            vacancy_identity = canonical_role_identity(candidate.opportunity)
            if vacancy_identity is None:
                raise HistoryMigrationConflict(
                    "Historical application has no vacancy identity: "
                    f"{candidate.application_id}"
                )
            previous_id = identities.get(vacancy_identity)
            if previous_id is not None and previous_id != candidate.application_id:
                raise HistoryMigrationConflict(
                    "Conflicting duplicate vacancy identity: "
                    f"{previous_id} and {candidate.application_id}"
                )
            identities[vacancy_identity] = candidate.application_id
        new = []
        existing = 0
        for application_id, candidate in unique.items():
            try:
                stored = self.application_store.load(application_id)
            except KeyError:
                new.append(candidate)
                continue
            if stored != candidate:
                raise HistoryMigrationConflict(
                    f"Conflicting application history identity: {application_id}"
                )
            existing += 1
        return tuple(new), existing

    @staticmethod
    def _unique(items, *, key, label):
        unique = {}
        for item in items:
            identity = key(item)
            previous = unique.get(identity)
            if previous is not None and previous != item:
                raise HistoryMigrationConflict(
                    f"Conflicting {label} identity: {identity}"
                )
            unique[identity] = item
        return unique


def load_known_applications(path: Path) -> tuple[HistoricalApplicationRecord, ...]:
    """Load the private, versioned candidate-history seed."""

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != HISTORY_IMPORT_VERSION:
        actual = value.get("version") if isinstance(value, dict) else None
        raise ValueError(f"Unsupported history import version: {actual}")
    applications = value.get("applications")
    if not isinstance(applications, list):
        raise ValueError("History import applications must be a list")
    records = []
    for item in applications:
        if not isinstance(item, dict):
            raise ValueError("Historical application must be an object")
        try:
            records.append(HistoricalApplicationRecord(**item))
        except TypeError as exc:
            raise ValueError(f"Invalid historical application: {exc}") from None
    return tuple(records)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "HISTORY_IMPORT_VERSION",
    "HistoricalApplicationRecord",
    "HistoryMigrationConflict",
    "HistoryMigrationService",
    "LegacySqliteHistorySource",
    "MigrationReport",
    "load_known_applications",
]
