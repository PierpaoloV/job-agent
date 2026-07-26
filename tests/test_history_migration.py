from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import LifecycleState  # noqa: E402
from application_policies import ReopenedRolePolicy  # noqa: E402
from application_storage import JsonApplicationStore  # noqa: E402
from history_migration import (  # noqa: E402
    HistoricalApplicationRecord,
    HistoryMigrationConflict,
    HistoryMigrationService,
    LegacySqliteHistorySource,
    load_known_applications,
)
from opportunity_storage import JsonOpportunityStore  # noqa: E402
from role_identity import (  # noqa: E402
    RoleIdentity,
    RoleIdentityKind,
    canonical_role_identity,
    role_identity_aliases,
)


MIGRATED_AT = datetime(2026, 7, 22, 9, 30, tzinfo=timezone.utc)


def test_typed_role_identities_normalize_urls_and_scope_employer_job_ids():
    canonical = canonical_role_identity(
        {
            "stable_id": "linkedin:42",
            "canonical_url": "HTTPS://JOBS.EXAMPLE/42/",
            "company": "Example AI",
            "official_job_id": "REQ-42",
        }
    )
    same_url = role_identity_aliases({"url": "https://jobs.example/42"})
    different_employer = role_identity_aliases(
        {"company": "Other AI", "official_job_id": "REQ-42"}
    )

    assert canonical == RoleIdentity(RoleIdentityKind.URL, "https://jobs.example/42")
    assert canonical in same_url
    assert (
        RoleIdentity(RoleIdentityKind.EMPLOYER_JOB_ID, "example ai:req-42")
        not in different_employer
    )


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE seen (
                url TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                source TEXT,
                first_seen TEXT,
                score REAL
            );
            CREATE TABLE applied (
                url TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                applied_at TEXT,
                status TEXT,
                notes TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO seen VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    "https://jobs.example/seen-1",
                    "Research Scientist",
                    "Example One",
                    "LinkedIn",
                    "2026-04-18T16:06:49.972104",
                    0.82,
                ),
                (
                    "linkedin:seen-2",
                    "ML Engineer",
                    "Example Two",
                    "LinkedIn",
                    "2026-04-19T10:00:00+00:00",
                    None,
                ),
            ),
        )
        connection.execute(
            "INSERT INTO applied VALUES (?, ?, ?, ?, ?, ?)",
            (
                "https://jobs.example/applied-1",
                "Applied Scientist",
                "Example Applied",
                "2026-05-02T12:00:00",
                "applied",
                "Imported from the original agent",
            ),
        )


def _known_application(
    *,
    application_id: str = "known:example:REQ-42",
    status: str = "rejected",
) -> HistoricalApplicationRecord:
    return HistoricalApplicationRecord(
        application_id=application_id,
        source="conversation",
        external_id="REQ-42",
        company="Example Health",
        title="AI Scientist",
        status=status,
        status_observed_at="2026-07-22T09:00:00+00:00",
        canonical_url="https://careers.example/REQ-42",
        notes="Status supplied by the candidate",
    )


def _service(tmp_path: Path) -> HistoryMigrationService:
    return HistoryMigrationService(
        legacy_source=LegacySqliteHistorySource(tmp_path / "legacy.sqlite"),
        opportunity_store=JsonOpportunityStore(tmp_path / "opportunities"),
        application_store=JsonApplicationStore(tmp_path / "applications"),
        migrated_at=lambda: MIGRATED_AT,
    )


def test_migration_preserves_legacy_history_and_reruns_without_duplicates(tmp_path):
    _legacy_database(tmp_path / "legacy.sqlite")
    service = _service(tmp_path)

    first = service.migrate(known_applications=(_known_application(),))
    second = service.migrate(known_applications=(_known_application(),))

    opportunities = service.opportunity_store.list()
    applications = service.application_store.list()
    assert first.source_seen == 2
    assert first.source_applications == 2
    assert first.imported_seen == 2
    assert first.imported_applications == 2
    assert second.imported_seen == 0
    assert second.imported_applications == 0
    assert second.already_present_seen == 2
    assert second.already_present_applications == 2
    assert len(opportunities) == 2
    assert {record.lead.title for record in opportunities} == {
        "Research Scientist",
        "ML Engineer",
    }
    assert {record.lead.discovered_at for record in opportunities} == {
        "2026-04-18T16:06:49.972104+02:00",
        "2026-04-19T10:00:00+00:00",
    }
    assert {record.lead.snippet for record in opportunities} == {
        "legacy_local_score=0.82",
        "legacy_local_score=unknown",
    }
    assert len(applications) == 2
    assert {application.lifecycle_state for application in applications} == {
        LifecycleState.SUBMITTED,
        LifecycleState.REJECTED,
    }
    rejected = next(
        application
        for application in applications
        if application.lifecycle_state == LifecycleState.REJECTED
    )
    assert rejected.opportunity["historical_status"] == "rejected"
    assert (
        rejected.opportunity["historical_notes"] == "Status supplied by the candidate"
    )


def test_conflicting_reimport_fails_closed_instead_of_overwriting_history(tmp_path):
    _legacy_database(tmp_path / "legacy.sqlite")
    service = _service(tmp_path)
    service.migrate(known_applications=(_known_application(),))

    changed = _known_application(status="interview")
    with pytest.raises(HistoryMigrationConflict, match="known:example:REQ-42"):
        service.migrate(known_applications=(changed,))

    stored = service.application_store.load("known:example:REQ-42")
    assert stored.lifecycle_state == LifecycleState.REJECTED


def test_two_source_records_for_one_vacancy_fail_before_creating_duplicates(tmp_path):
    _legacy_database(tmp_path / "legacy.sqlite")
    service = _service(tmp_path)
    duplicate = HistoricalApplicationRecord(
        application_id="known:duplicate:applied-1",
        source="candidate_history",
        external_id="applied-1",
        company="Example Applied",
        title="Applied Scientist",
        status="awaiting_response",
        status_observed_at="2026-07-22T09:00:00+00:00",
        canonical_url="https://jobs.example/applied-1/",
    )

    with pytest.raises(HistoryMigrationConflict, match="duplicate vacancy identity"):
        service.migrate(known_applications=(duplicate,))

    assert service.opportunity_store.list() == ()
    assert service.application_store.list() == ()


def test_imported_active_application_is_visible_to_duplicate_policy_by_canonical_url(
    tmp_path,
):
    _legacy_database(tmp_path / "legacy.sqlite")
    service = _service(tmp_path)
    service.migrate(known_applications=())
    imported = service.application_store.list()[0]

    evidence = ReopenedRolePolicy().prior_evidence(
        {
            "stable_id": "linkedin:new-identifier",
            "canonical_url": "https://jobs.example/applied-1",
            "company": "Example Applied",
            "title": "Applied Scientist",
        },
        (imported,),
    )

    assert len(evidence) == 1
    assert evidence[0].application_id == imported.application_id
    assert evidence[0].is_active is True


def test_all_supported_known_application_statuses_have_explicit_lifecycle_mapping(
    tmp_path,
):
    _legacy_database(tmp_path / "legacy.sqlite")
    service = _service(tmp_path)
    records = tuple(
        HistoricalApplicationRecord(
            **{
                **_known_application(
                    application_id=f"known:example:{status}",
                    status=status,
                ).__dict__,
                "external_id": status,
                "canonical_url": f"https://careers.example/{status}",
            }
        )
        for status in ("submitted", "awaiting_response", "interview", "rejected")
    )

    service.migrate(known_applications=records)

    by_status = {
        item.opportunity["historical_status"]: item.lifecycle_state
        for item in service.application_store.list()
        if item.application_id.startswith("known:")
    }
    assert by_status == {
        "submitted": LifecycleState.SUBMITTED,
        "awaiting_response": LifecycleState.SUBMITTED,
        "interview": LifecycleState.INTERVIEW,
        "rejected": LifecycleState.REJECTED,
    }


def test_unknown_historical_status_is_rejected_before_any_target_write(tmp_path):
    _legacy_database(tmp_path / "legacy.sqlite")
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="Unsupported historical application status"):
        service.migrate(known_applications=(_known_application(status="maybe"),))

    assert service.opportunity_store.list() == ()
    assert service.application_store.list() == ()


def test_versioned_local_seed_loader_preserves_available_application_fields(tmp_path):
    seed = tmp_path / "known-applications.json"
    seed.write_text(
        """{
          "version": "job-agent.history-import.v1",
          "applications": [{
            "application_id": "known:employer:42",
            "source": "candidate_history",
            "external_id": "42",
            "company": "Employer",
            "title": "Research Engineer",
            "status": "awaiting_response",
            "status_observed_at": "",
            "canonical_url": "",
            "notes": "Exact date and URL unavailable"
          }]
        }""",
        encoding="utf-8",
    )

    records = load_known_applications(seed)

    assert records == (
        HistoricalApplicationRecord(
            application_id="known:employer:42",
            source="candidate_history",
            external_id="42",
            company="Employer",
            title="Research Engineer",
            status="awaiting_response",
            status_observed_at="",
            canonical_url="",
            notes="Exact date and URL unavailable",
        ),
    )


def test_seed_loader_rejects_unknown_versions(tmp_path):
    seed = tmp_path / "known-applications.json"
    seed.write_text('{"version":"v999","applications":[]}', encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported history import version"):
        load_known_applications(seed)


def test_missing_historical_date_uses_first_migration_time_on_every_rerun(tmp_path):
    _legacy_database(tmp_path / "legacy.sqlite")
    later = datetime(2026, 7, 23, 9, 30, tzinfo=timezone.utc)
    migration_times = iter((MIGRATED_AT, later))
    service = HistoryMigrationService(
        legacy_source=LegacySqliteHistorySource(tmp_path / "legacy.sqlite"),
        opportunity_store=JsonOpportunityStore(tmp_path / "opportunities"),
        application_store=JsonApplicationStore(tmp_path / "applications"),
        migrated_at=lambda: next(migration_times),
    )
    undated = HistoricalApplicationRecord(
        **{
            **_known_application().__dict__,
            "status_observed_at": "",
        }
    )

    first = service.migrate(known_applications=(undated,))
    second = service.migrate(known_applications=(undated,))

    assert first.imported_applications == 2
    assert second.imported_applications == 0
    assert second.already_present_applications == 2
    stored = service.application_store.load(undated.application_id)
    assert stored.opportunity["historical_timestamp_basis"] == "migration_time_only"
    assert stored.history[-1].occurred_at == MIGRATED_AT.isoformat()
