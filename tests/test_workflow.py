from datetime import datetime, timezone
import pathlib
import pytest
import sys
import types


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))

import dedupe
import main
import rank_llm
from workflow import ShortlistArtifact, WorkflowCoordinator


class FakeDiscovery:
    def fetch(self, days_back: int):
        assert days_back == 3
        return [{"id": "email-1", "body": "alert"}]


class FakeParser:
    def parse(self, emails):
        assert emails == [{"id": "email-1", "body": "alert"}]
        return [
            {
                "title": "Research Scientist",
                "company": "Acme AI",
                "location": "Zurich",
                "url": "https://jobs.example/42",
                "dedup_key": "acme:42",
                "source": "LinkedIn",
                "email_date": "Thu, 16 Jul 2026 08:00:00 +0000",
                "raw_email_context": "Public vacancy context from the alert",
            },
            {
                "title": "Already applied",
                "company": "Old Co",
                "url": "https://jobs.example/applied",
                "dedup_key": "old:1",
            },
        ]


class FakePersistence:
    def __init__(self):
        self.seen = []
        self.shortlists = []

    def filter_new(self, jobs):
        return list(jobs)

    def is_applied(self, url: str):
        return url.endswith("/applied")

    def mark_seen(self, jobs):
        self.seen.extend(jobs)

    def save_shortlist(self, artifact):
        self.shortlists.append(artifact)


class FakeScreener:
    def __init__(self):
        self.calls = []

    def screen(self, job):
        self.calls.append(job)
        return {"score": 0.8, "reasons": ["research role"], "shortlisted": True}


class FakeGrader:
    def __init__(self):
        self.calls = []

    def rank(self, jobs, top_n: int):
        self.calls.append((jobs, top_n))
        return [{**jobs[0], "score": 0.91, "rationale": "Strong fit"}]


class FakeNotifier:
    def __init__(self):
        self.digests = []
        self.errors = []

    def send_digest(self, jobs):
        self.digests.append(jobs)

    def send_error(self, message: str):
        self.errors.append(message)


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


def test_current_digest_journey_runs_through_the_public_coordinator():
    persistence = FakePersistence()
    screener = FakeScreener()
    grader = FakeGrader()
    notifier = FakeNotifier()
    coordinator = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=FakeParser(),
        persistence=persistence,
        screener=screener,
        grader=grader,
        notifier=notifier,
        clock=FixedClock(),
    )

    result = coordinator.run(days_back=3)

    assert result.status == "completed"
    assert result.artifact.version == "job-agent.shortlist.v1"
    assert result.artifact.created_at == "2026-07-16T10:30:00+00:00"
    assert [record.stable_id for record in result.artifact.opportunities] == ["acme:42"]
    assert result.artifact.opportunities[0].screening_reasons == ("research role",)
    assert persistence.shortlists == [result.artifact]
    assert len(screener.calls) == 1
    assert grader.calls[0][1] == 10
    assert [job["dedup_key"] for job in persistence.seen] == ["acme:42"]
    assert notifier.digests == [[{
        "title": "Research Scientist",
        "company": "Acme AI",
        "location": "Zurich",
        "url": "https://jobs.example/42",
        "dedup_key": "acme:42",
        "source": "LinkedIn",
        "email_date": "Thu, 16 Jul 2026 08:00:00 +0000",
        "raw_email_context": "Public vacancy context from the alert",
        "stable_id": "acme:42",
        "local_score": 0.8,
        "screening_reasons": ["research role"],
        "screening_outcome": "unknown",
        "screening_features": {},
        "score": 0.91,
        "rationale": "Strong fit",
    }]]
    assert notifier.errors == []


def test_ingest_and_screen_needs_no_model_credential_or_grader_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    grader = FakeGrader()
    coordinator = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=FakeParser(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=grader,
        notifier=FakeNotifier(),
        clock=FixedClock(),
    )

    artifact = coordinator.ingest_and_screen(days_back=3)

    assert len(artifact.opportunities) == 1
    assert grader.calls == []


def test_ingest_deduplicates_same_role_within_one_daily_batch():
    class DuplicateParser:
        def parse(self, emails):
            job = {
                "title": "Research Scientist",
                "company": "Example",
                "url": "https://example.test/jobs/42",
                "dedup_key": "example:42",
            }
            return [job, dict(job)]

    artifact = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=DuplicateParser(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=FakeGrader(),
        notifier=FakeNotifier(),
        clock=FixedClock(),
    ).ingest_and_screen(days_back=3)

    assert [item.stable_id for item in artifact.opportunities] == ["example:42"]


def test_shortlist_artifact_redacts_email_addresses_from_alert_context():
    class ParserWithRecipientData:
        def parse(self, emails):
            return [{
                "title": "ML Engineer",
                "company": "Example",
                "url": "https://jobs.example/recipient-data",
                "dedup_key": "example:recipient-data",
                "raw_email_context": (
                    "ML Engineer vacancy sent to candidate.private@example.com"
                ),
            }]

    coordinator = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=ParserWithRecipientData(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=FakeGrader(),
        notifier=FakeNotifier(),
        clock=FixedClock(),
    )

    artifact = coordinator.ingest_and_screen(days_back=3)

    serialized = artifact.to_json()
    assert "ML Engineer vacancy" in serialized
    assert "candidate.private@example.com" not in serialized


def test_shortlist_artifact_is_durable_and_rejects_unknown_versions(tmp_path):
    coordinator = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=FakeParser(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=FakeGrader(),
        notifier=FakeNotifier(),
        clock=FixedClock(),
    )
    path = tmp_path / "shortlist.json"
    coordinator.ingest_and_screen(days_back=3).write(path)

    restored = ShortlistArtifact.read(path)

    assert restored.opportunities[0].stable_id == "acme:42"
    incompatible = restored.to_json().replace(
        "job-agent.shortlist.v1", "job-agent.shortlist.v999"
    )
    try:
        ShortlistArtifact.from_json(incompatible)
    except ValueError as error:
        assert str(error) == (
            "Unsupported shortlist artifact version: job-agent.shortlist.v999"
        )
    else:
        raise AssertionError("incompatible artifact version was accepted")


def test_shortlist_artifact_rejects_nested_candidate_sensitive_fields():
    value = {
        "version": "job-agent.shortlist.v1",
        "created_at": "2026-07-16T10:30:00+00:00",
        "opportunities": [{
            "stable_id": "example:1",
            "discovered_at": "2026-07-16T10:30:00+00:00",
            "source_confidence": "supported",
            "local_score": 0.8,
            "screening_reasons": [],
            "shortlisted": True,
            "job": {
                "dedup_key": "example:1",
                "compensation": {"candidate_profile": {"current_salary": 1}},
            },
        }],
    }

    try:
        ShortlistArtifact.from_dict(value)
    except ValueError as error:
        assert str(error) == "Shortlist artifact contains candidate-sensitive data"
    else:
        raise AssertionError("candidate-sensitive artifact was accepted")


def test_shortlist_artifact_rejects_camel_case_sensitive_screening_keys():
    for sensitive_key in ("accessToken", "apiKey", "healthData"):
        value = {
            "version": "job-agent.shortlist.v1",
            "created_at": "2026-07-16T10:30:00+00:00",
            "opportunities": [
                {
                    "stable_id": "example:1",
                    "discovered_at": "2026-07-16T10:30:00+00:00",
                    "source_confidence": "supported",
                    "local_score": 0.8,
                    "screening_reasons": [],
                    "shortlisted": True,
                    "screening_features": {sensitive_key: "private-value"},
                    "job": {"dedup_key": "example:1"},
                }
            ],
        }

        try:
            ShortlistArtifact.from_dict(value)
        except ValueError as error:
            assert str(error) == (
                "Shortlist artifact contains candidate-sensitive data"
            )
        else:
            raise AssertionError(f"sensitive key {sensitive_key} was accepted")


def test_shortlist_artifact_preserves_auditable_screening_features_compatibly():
    class FeatureScreener:
        def screen(self, job):
            return {
                "score": 0.72,
                "reasons": ["local taxonomy match"],
                "shortlisted": True,
                "outcome": "shortlisted",
                "features": {
                    "geography": {"label": "zurich", "points": 20},
                    "method": "local_bag_of_phrases_v1",
                },
            }

    artifact = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=FakeParser(),
        persistence=FakePersistence(),
        screener=FeatureScreener(),
        grader=FakeGrader(),
        notifier=FakeNotifier(),
        clock=FixedClock(),
    ).ingest_and_screen(days_back=3)

    restored = ShortlistArtifact.from_json(artifact.to_json())
    opportunity = restored.opportunities[0]
    assert opportunity.screening_outcome == "shortlisted"
    assert opportunity.screening_features["geography"]["label"] == "zurich"

    legacy = artifact.to_dict()
    legacy_record = legacy["opportunities"][0]
    legacy_record.pop("screening_outcome")
    legacy_record.pop("screening_features")
    restored_legacy = ShortlistArtifact.from_dict(legacy)
    assert restored_legacy.opportunities[0].screening_outcome == "unknown"
    assert restored_legacy.opportunities[0].screening_features == {}


def test_low_score_records_remain_available_for_false_negative_audit():
    artifact = ShortlistArtifact.from_dict(
        {
            "version": "job-agent.shortlist.v1",
            "created_at": "2026-07-16T10:30:00+00:00",
            "opportunities": [
                {
                    "stable_id": f"example:{score}",
                    "discovered_at": "2026-07-16T10:30:00+00:00",
                    "source_confidence": "supported",
                    "local_score": score,
                    "screening_reasons": ["auditable local decision"],
                    "shortlisted": score > 0.5,
                    "screening_outcome": "shortlisted" if score > 0.5 else "overflow",
                    "screening_features": {"score_source": "local"},
                    "job": {"dedup_key": f"example:{score}"},
                }
                for score in (0.8, 0.4, 0.1)
            ],
        }
    )

    assert [item.local_score for item in artifact.screening_audit_sample(limit=1)] == [
        0.4
    ]


def test_verified_vacancy_contract_survives_screening_boundary():
    class VerifiedParser:
        def parse(self, emails):
            return [{
                "title": "Research Scientist",
                "company": "Example",
                "url": "https://example.test/jobs/42",
                "dedup_key": "example:42",
                "verification_status": "verified",
                "official_vacancy_version": "vacancy-v1",
                "team": "Vision Research",
                "requirements": ["Python"],
                "application_deadline": "2026-08-01",
                "official_description": "Official role description.",
            }]

    grader = FakeGrader()
    WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=VerifiedParser(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=grader,
        notifier=FakeNotifier(),
        clock=FixedClock(),
    ).run(days_back=3)

    grading_job = grader.calls[0][0][0]
    assert grading_job["stable_id"] == "example:42"
    assert grading_job["official_vacancy_version"] == "vacancy-v1"
    assert grading_job["team"] == "Vision Research"
    assert grading_job["requirements"] == ["Python"]
    assert grading_job["application_deadline"] == "2026-08-01"


class FailingDiscovery:
    def fetch(self, days_back: int):
        raise RuntimeError("token=super-secret-value")


def test_failure_emits_one_sanitized_operator_error():
    notifier = FakeNotifier()
    coordinator = WorkflowCoordinator(
        discovery=FailingDiscovery(),
        parser=FakeParser(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=FakeGrader(),
        notifier=notifier,
        clock=FixedClock(),
    )

    result = coordinator.run()

    assert result.status == "failed"
    assert notifier.digests == []
    assert notifier.errors == [
        "Job workflow failed safely. No further action was taken; "
        "check the local or GitHub Actions logs."
    ]
    assert "super-secret-value" not in notifier.errors[0]


def test_production_pending_verification_path_makes_no_legacy_model_call(monkeypatch):
    monkeypatch.setattr(
        rank_llm,
        "score_job",
        lambda job: (_ for _ in ()).throw(AssertionError("legacy model called")),
    )
    notifier = FakeNotifier()
    coordinator = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=FakeParser(),
        persistence=FakePersistence(),
        screener=FakeScreener(),
        grader=main.ProductionPortfolioGrader(),
        notifier=notifier,
        clock=FixedClock(),
    )

    result = coordinator.run(days_back=3)

    assert result.status == "completed"
    assert notifier.digests == [[]]
    assert notifier.errors == []


def test_legacy_persistence_keeps_existing_seen_and_applied_history(monkeypatch, tmp_path):
    monkeypatch.setattr(dedupe, "DB_PATH", tmp_path / "seen.sqlite")
    seen_job = {
        "title": "Seen role",
        "company": "Seen Co",
        "url": "https://jobs.example/seen",
        "dedup_key": "seen:1",
    }
    applied_job = {
        "title": "Applied role",
        "company": "Applied Co",
        "url": "https://jobs.example/applied-before-refactor",
        "dedup_key": "applied:1",
    }
    new_job = {
        "title": "New role",
        "company": "New Co",
        "url": "https://jobs.example/new",
        "dedup_key": "new:1",
    }
    dedupe.mark_seen([seen_job])
    dedupe.mark_applied(
        applied_job["url"],
        title=applied_job["title"],
        company=applied_job["company"],
    )

    shortlist_dir = tmp_path / "shortlists"
    persistence = main.LegacyPersistence(shortlist_dir=shortlist_dir)

    assert persistence.filter_new([seen_job, new_job]) == [new_job]
    assert persistence.is_applied(applied_job["url"]) is True
    persistence.mark_seen([new_job])

    restarted_persistence = main.LegacyPersistence()
    assert restarted_persistence.filter_new([seen_job, new_job]) == []
    assert [record["url"] for record in dedupe.get_applied()] == [applied_job["url"]]

    artifact = WorkflowCoordinator(
        discovery=FakeDiscovery(),
        parser=FakeParser(),
        persistence=persistence,
        screener=FakeScreener(),
        grader=FakeGrader(),
        notifier=FakeNotifier(),
        clock=FixedClock(),
    ).ingest_and_screen(days_back=3)
    persisted_paths = list(shortlist_dir.glob("*.json"))
    assert len(persisted_paths) == 1
    assert ShortlistArtifact.read(persisted_paths[0]) == artifact


def test_main_routes_the_cli_through_the_coordinator(monkeypatch):
    calls = []

    class FakeCoordinator:
        def run(self, *, days_back: int):
            calls.append(days_back)
            return "workflow result"

    monkeypatch.setattr(main, "build_coordinator", lambda: FakeCoordinator())

    assert main.main(days_back=7) == "workflow result"
    assert calls == [7]


def test_main_build_uses_local_screening_and_stops_before_unverified_model_grading(
    monkeypatch, tmp_path
):
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(
        "portfolio:\n  shortlist_threshold: 0.45\n"
        "target_roles:\n  - Research Engineer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOB_AGENT_PREFERENCES_PATH", str(preferences))

    coordinator = main.build_coordinator()

    assert coordinator._screener.__class__.__name__ == "LocalPortfolioScreener"
    assert coordinator._grader.__class__.__name__ == "ProductionPortfolioGrader"


def test_main_fails_closed_without_explicit_preferences(monkeypatch):
    monkeypatch.delenv("JOB_AGENT_PREFERENCES_PATH", raising=False)

    with pytest.raises(RuntimeError, match="JOB_AGENT_PREFERENCES_PATH"):
        main.build_coordinator()


def test_main_loads_portfolio_policy_from_preferences_file(tmp_path):
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(
        "portfolio:\n  shortlist_threshold: 0.91\n"
        "target_roles:\n  - AI Safety Engineer\n",
        encoding="utf-8",
    )

    policy = main._load_portfolio_policy(preferences)

    assert policy.shortlist_threshold == 0.91
    assert "ai safety engineer" in policy.role_taxonomy["applied"]
