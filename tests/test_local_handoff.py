from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from actions_state import StateBundle  # noqa: E402
from deep_grading_contract import SanitizedProfessionalProfile  # noqa: E402
from deep_grading_service import DeepGradingService  # noqa: E402
from deep_grading_store import DeepGradeStore  # noqa: E402
from discovery_pending import PendingShortlistStore  # noqa: E402
from local_handoff import HandoffIdentity, LocalHandoffService  # noqa: E402
import local_handoff  # noqa: E402
from opportunity_domain import (  # noqa: E402
    Evaluation,
    OfficialVacancyData,
    OfficialVacancySnapshot,
)
from opportunity_storage import JsonOpportunityStore  # noqa: E402
from opportunity_workflow import OpportunityWorkflow  # noqa: E402
from workflow import ShortlistArtifact  # noqa: E402


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


class AdjustableClock:
    def __init__(self):
        self.current = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def advance(self, delta):
        self.current += delta


class LocalOfficialSource:
    def __init__(self, vacancy):
        self.vacancy = vacancy
        self.calls = []

    def retrieve(self, lead, runtime):
        self.calls.append((lead.stable_id, runtime))
        return self.vacancy


class CrashOnceOfficialSource(LocalOfficialSource):
    def retrieve(self, lead, runtime):
        self.calls.append((lead.stable_id, runtime))
        if len(self.calls) == 1:
            raise RuntimeError("simulated retrieval crash")
        return self.vacancy


class NoModelOpportunityEvaluator:
    def __init__(self):
        self.calls = []

    def evaluate(self, snapshot):
        self.calls.append(snapshot)
        return Evaluation(
            fit_summary="Locally verified",
            gaps=(),
            compensation_status="unknown",
            wealth_potential_confidence="low",
            immigration="not stated",
            ownership="unknown",
            risks=(),
            rank_explanation="Official vacancy recovered locally",
            requirement_analysis=("Python",),
            sources=(snapshot.vacancy.canonical_url,),
        )


class GradingProvider:
    def __init__(self):
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        components = {
            name: {"score": 80, "explanation": f"Evidence-based {name}"}
            for name in (
                "fit",
                "research_preference",
                "geography",
                "compensation_confidence",
                "wealth_potential",
                "language",
                "immigration",
                "ownership",
                "freshness",
                "deadline",
                "risk",
            )
        }
        return {
            "schema_version": "job-agent.deep-grade.v1",
            "overall_score": 84,
            "top_tier": {"value": True, "explanation": "Strong fit"},
            "rank_explanation": "Strong official-vacancy fit",
            "components": components,
            "compensation": {
                "base_cash": {"status": "unknown", "facts": []},
                "bonus": {"status": "unknown", "facts": []},
                "equity": {"status": "unknown", "facts": []},
                "benchmarks": [],
                "wealth_potential": {
                    "confidence": "low",
                    "explanation": "Compensation is not published",
                    "assumptions": [],
                },
            },
            "sponsorship": {
                "status": "not_stated",
                "source": "https://careers.acme.example/jobs/42",
                "verified_at": "2026-07-16",
                "visa_obstacle": False,
            },
            "ownership": {
                "classification": "unknown",
                "source": "https://careers.acme.example/jobs/42",
                "verified_at": "2026-07-16",
            },
            "risks": [],
            "gaps": [],
            "requirements_evidence_matrix": {
                "version": "job-agent.requirements-evidence.v1",
                "rows": [
                    {
                        "id": "req.python",
                        "requirement": "Python",
                        "importance": "required",
                        "status": "matched",
                        "evidence_ids": ["exp.python"],
                        "explanation": "Python delivery evidence",
                    }
                ],
            },
            "sources": ["https://careers.acme.example/jobs/42"],
        }


class FailBeforeGradeCacheStore(DeepGradeStore):
    def __init__(self, root):
        super().__init__(root)
        self.fail_once = True

    def save(self, result):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated crash before grade cache")
        super().save(result)


class FailAfterGradeCacheStore(DeepGradeStore):
    def __init__(self, root):
        super().__init__(root)
        self.fail_once = True

    def save(self, result):
        super().save(result)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated crash after grade cache")


def _profile():
    return SanitizedProfessionalProfile.from_mapping(
        {
            "provenance": "canonical_cv_evidence_bank",
            "professional_summary": "Applied AI researcher",
            "skills": ["Python"],
            "professional_evidence": [
                {
                    "id": "exp.python",
                    "claim": "Built production Python research systems",
                    "source_id": "canonical-cv:research",
                }
            ],
            "target_preferences": {"geography": ["Zurich"]},
        }
    )


def _official_vacancy():
    return OfficialVacancyData(
        official_job_id="acme-42",
        canonical_url="https://careers.acme.example/jobs/42",
        company="Acme AI",
        role="Research Scientist",
        team="Vision",
        location="Zurich",
        modality="hybrid",
        seniority="senior",
        compensation="not published",
        requirements=("Python",),
        ownership="unknown",
        sponsorship="not stated",
        description="Full official vacancy: build Python vision systems.",
        published_at="2026-07-14",
    )


def _authority():
    return {
        "repository": "example-org/job-agent",
        "workflow": "run.yml",
        "branch": "main",
    }


def _remote_bundle(root, *, official_version, authority=None):
    snippet = "EMAIL SNIPPET MUST NEVER BE GRADED"
    artifact = ShortlistArtifact.from_dict(
        {
            "version": "job-agent.shortlist.v1",
            "created_at": "2026-07-16T10:00:00+00:00",
            "opportunities": [
                {
                    "stable_id": "linkedin:42",
                    "discovered_at": "2026-07-16T10:00:00+00:00",
                    "source_confidence": "supported",
                    "local_score": 0.9,
                    "screening_reasons": ["strong local fit"],
                    "shortlisted": True,
                    "screening_outcome": "needs_local_fetch",
                    "job": {
                        "title": "Research Scientist",
                        "company": "Acme AI",
                        "location": "Zurich",
                        "source": "LinkedIn",
                        "url": "https://linkedin.example/jobs/42",
                        "dedup_key": "linkedin:42",
                        "snippet": snippet,
                        "verification_status": "needs_local_fetch",
                        "official_vacancy_version": official_version,
                    },
                }
            ],
        }
    )
    artifact.write(root / "data" / "pending-shortlist.json")
    bundle = StateBundle(root)
    bundle.write_manifest(
        {
            **(authority or _authority()),
            "run_id": 42,
            "run_attempt": 1,
            "stage": "deep",
        }
    )
    return bundle, snippet


def test_remote_needs_local_fetch_imports_idempotently_and_grades_official_page_once(
    tmp_path, monkeypatch
):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    bundle, snippet = _remote_bundle(
        tmp_path / "remote", official_version=expected_version
    )
    packaged_pending = (
        bundle.package_dir / "files" / "data" / "pending-shortlist.json"
    ).read_bytes()
    local_root = tmp_path / "local"
    source = LocalOfficialSource(vacancy)
    evaluator = NoModelOpportunityEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(local_root / "data" / "private" / "opportunities"),
        official_source=source,
        evaluator=evaluator,
        clock=FixedClock(),
    )
    provider = GradingProvider()
    grading = DeepGradingService(
        provider=provider,
        store=DeepGradeStore(local_root / "data" / "deep-grades"),
    )
    handoff = LocalHandoffService(
        root=local_root,
        workflow=workflow,
        grading=grading,
        profile=_profile(),
        expected_authority=_authority(),
    )

    first_import = handoff.import_bundle(bundle.package_dir)
    repeated_import = handoff.import_bundle(bundle.package_dir)
    def reject_remote_state_mutation(store, graded_jobs):
        raise AssertionError("local handoff must not mutate remote-owned state")

    monkeypatch.setattr(
        PendingShortlistStore,
        "clear_graded",
        reject_remote_state_mutation,
    )
    first = handoff.resume("linkedin:42", expected_version)
    completed_import = handoff.import_bundle(bundle.package_dir)
    repeated = handoff.resume("linkedin:42", expected_version)

    assert first_import.imported == 1
    assert repeated_import.imported == 0
    assert repeated_import.existing == 1
    assert completed_import.imported == 0
    assert completed_import.existing == 1
    assert first.stable_id == "linkedin:42"
    assert first.official_vacancy_version == expected_version
    assert repeated.grade == first.grade
    assert len(source.calls) == 1
    assert len(provider.calls) == 1
    assert evaluator.calls == []
    assert snippet not in json.dumps(provider.calls[0], sort_keys=True)
    assert "Full official vacancy" in json.dumps(provider.calls[0], sort_keys=True)
    local_pending = local_root / "data" / "pending-shortlist.json"
    assert local_pending.read_bytes() == packaged_pending
    assert handoff.remaining() == ()


def test_possible_grading_call_requires_typed_resolution_before_retry(tmp_path):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    bundle, _ = _remote_bundle(
        tmp_path / "remote", official_version=expected_version
    )
    local_root = tmp_path / "local"
    source = LocalOfficialSource(vacancy)
    provider = GradingProvider()
    clock = AdjustableClock()
    tokens = iter(("claim-one", "claim-two"))
    handoff = LocalHandoffService(
        root=local_root,
        workflow=OpportunityWorkflow(
            store=JsonOpportunityStore(
                local_root / "data" / "private" / "opportunities"
            ),
            official_source=source,
            evaluator=NoModelOpportunityEvaluator(),
            clock=clock,
        ),
        grading=DeepGradingService(
            provider=provider,
            store=FailBeforeGradeCacheStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
        owner="worker-a",
        token_factory=lambda: next(tokens),
        clock=clock,
    )
    handoff.import_bundle(bundle.package_dir)

    with pytest.raises(RuntimeError, match="before grade cache"):
        handoff.resume("linkedin:42", expected_version)
    with pytest.raises(local_handoff.HandoffGradingBusy):
        handoff.resume("linkedin:42", expected_version)
    clock.advance(timedelta(minutes=6))
    with pytest.raises(local_handoff.HandoffGradingOutcomeUncertain):
        handoff.resume("linkedin:42", expected_version)

    intent = handoff.grading_intent("linkedin:42", expected_version)
    assert intent is not None
    assert intent.phase == local_handoff.HandoffGradingPhase.UNCERTAIN
    assert intent.owner == "worker-a"
    assert intent.token == "claim-one"
    assert intent.grading_input_fingerprint.startswith("sha256:")
    assert len(provider.calls) == 1
    assert len(source.calls) == 1

    with pytest.raises(TypeError, match="typed grading resolution"):
        handoff.resolve_uncertain({"resolution": "retry"})
    handoff.resolve_uncertain(
        local_handoff.HandoffGradingResolutionCommand(
            identity=HandoffIdentity("linkedin:42", expected_version),
            intent_token=intent.token,
            actor="Synthetic Owner",
            resolution=(
                local_handoff.HandoffGradingResolution.CONFIRMED_NO_RESULT
            ),
        )
    )

    completed = handoff.resume("linkedin:42", expected_version)

    assert completed.grade.opportunity_id == "linkedin:42"
    assert len(provider.calls) == 2
    assert len(source.calls) == 2


def test_exact_durable_grade_cache_reconciles_without_repeating_provider(tmp_path):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    bundle, _ = _remote_bundle(
        tmp_path / "remote", official_version=expected_version
    )
    local_root = tmp_path / "local"
    source = LocalOfficialSource(vacancy)
    provider = GradingProvider()
    handoff = LocalHandoffService(
        root=local_root,
        workflow=OpportunityWorkflow(
            store=JsonOpportunityStore(
                local_root / "data" / "private" / "opportunities"
            ),
            official_source=source,
            evaluator=NoModelOpportunityEvaluator(),
            clock=FixedClock(),
        ),
        grading=DeepGradingService(
            provider=provider,
            store=FailAfterGradeCacheStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
        owner="worker-a",
        token_factory=lambda: "claim-one",
        clock=FixedClock(),
    )
    handoff.import_bundle(bundle.package_dir)

    with pytest.raises(RuntimeError, match="after grade cache"):
        handoff.resume("linkedin:42", expected_version)
    completed = handoff.resume("linkedin:42", expected_version)

    assert completed.grade.opportunity_id == "linkedin:42"
    assert handoff.remaining() == ()
    assert len(provider.calls) == 1
    assert len(source.calls) == 1


def test_retrieval_claim_can_only_be_reissued_after_its_lease_expires(tmp_path):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    bundle, _ = _remote_bundle(
        tmp_path / "remote", official_version=expected_version
    )
    local_root = tmp_path / "local"
    source = CrashOnceOfficialSource(vacancy)
    provider = GradingProvider()
    clock = AdjustableClock()
    tokens = iter(("claim-one", "claim-two"))
    handoff = LocalHandoffService(
        root=local_root,
        workflow=OpportunityWorkflow(
            store=JsonOpportunityStore(
                local_root / "data" / "private" / "opportunities"
            ),
            official_source=source,
            evaluator=NoModelOpportunityEvaluator(),
            clock=clock,
        ),
        grading=DeepGradingService(
            provider=provider,
            store=DeepGradeStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
        owner="worker-a",
        token_factory=lambda: next(tokens),
        clock=clock,
        claim_lease=timedelta(minutes=5),
    )
    handoff.import_bundle(bundle.package_dir)

    with pytest.raises(RuntimeError, match="retrieval crash"):
        handoff.resume("linkedin:42", expected_version)
    with pytest.raises(local_handoff.HandoffGradingBusy):
        handoff.resume("linkedin:42", expected_version)

    clock.advance(timedelta(minutes=6))
    completed = handoff.resume("linkedin:42", expected_version)

    assert completed.grade.opportunity_id == "linkedin:42"
    assert len(source.calls) == 2
    assert len(provider.calls) == 1
    intent = handoff.grading_intent("linkedin:42", expected_version)
    assert intent is not None
    assert intent.token == "claim-two"
    assert intent.phase == local_handoff.HandoffGradingPhase.COMPLETED


def test_simultaneous_resumes_share_one_atomic_grading_claim(tmp_path):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    bundle, _ = _remote_bundle(
        tmp_path / "remote", official_version=expected_version
    )
    local_root = tmp_path / "local"
    source = LocalOfficialSource(vacancy)
    provider = GradingProvider()
    grading = DeepGradingService(
        provider=provider,
        store=DeepGradeStore(local_root / "data" / "deep-grades"),
    )
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(
            local_root / "data" / "private" / "opportunities"
        ),
        official_source=source,
        evaluator=NoModelOpportunityEvaluator(),
        clock=FixedClock(),
    )
    token_counter = 0
    token_lock = threading.Lock()

    def slow_token():
        nonlocal token_counter
        time.sleep(0.05)
        with token_lock:
            token_counter += 1
            return f"claim-{token_counter}"

    services = tuple(
        LocalHandoffService(
            root=local_root,
            workflow=workflow,
            grading=grading,
            profile=_profile(),
            expected_authority=_authority(),
            owner=f"worker-{index}",
            token_factory=slow_token,
            clock=FixedClock(),
        )
        for index in (1, 2)
    )
    services[0].import_bundle(bundle.package_dir)
    start = threading.Barrier(3)

    def resume(service):
        start.wait()
        try:
            return service.resume("linkedin:42", expected_version)
        except (
            local_handoff.HandoffGradingBusy,
            local_handoff.HandoffGradingOutcomeUncertain,
        ) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(resume, service) for service in services]
        start.wait()
        outcomes = [future.result() for future in futures]

    assert len(provider.calls) == 1
    assert len(source.calls) == 1
    assert sum(
        isinstance(outcome, local_handoff.LocalHandoffResumeResult)
        for outcome in outcomes
    ) == 1


def test_local_resume_rejects_an_official_vacancy_version_mismatch(tmp_path):
    mismatched_version = "sha256:" + ("0" * 64)
    bundle, _ = _remote_bundle(
        tmp_path / "remote", official_version=mismatched_version
    )
    local_root = tmp_path / "local"
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(local_root / "data" / "private" / "opportunities"),
        official_source=LocalOfficialSource(_official_vacancy()),
        evaluator=NoModelOpportunityEvaluator(),
        clock=FixedClock(),
    )
    provider = GradingProvider()
    handoff = LocalHandoffService(
        root=local_root,
        workflow=workflow,
        grading=DeepGradingService(
            provider=provider,
            store=DeepGradeStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
    )
    handoff.import_bundle(bundle.package_dir)

    with pytest.raises(ValueError, match="version does not match handoff"):
        handoff.resume("linkedin:42", mismatched_version)

    assert provider.calls == []
    assert (local_root / "data" / "pending-shortlist.json").is_file()


def test_local_import_rejects_an_incompatible_remote_authority(tmp_path):
    bundle, _ = _remote_bundle(
        tmp_path / "remote",
        official_version="sha256:" + ("1" * 64),
        authority={
            "repository": "attacker/fork",
            "workflow": "run.yml",
            "branch": "main",
        },
    )
    local_root = tmp_path / "local"
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(local_root / "data" / "private" / "opportunities"),
        official_source=LocalOfficialSource(_official_vacancy()),
        evaluator=NoModelOpportunityEvaluator(),
        clock=FixedClock(),
    )
    handoff = LocalHandoffService(
        root=local_root,
        workflow=workflow,
        grading=DeepGradingService(
            provider=GradingProvider(),
            store=DeepGradeStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
    )

    with pytest.raises(ValueError, match="authority mismatch: repository"):
        handoff.import_bundle(bundle.package_dir)

    assert not (local_root / "data" / "private" / "opportunities").exists()


def test_identity_mismatch_rejects_bundle_before_local_state_mutation(tmp_path):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    remote_root = tmp_path / "remote"
    bundle, _ = _remote_bundle(remote_root, official_version=expected_version)
    first_package = shutil.copytree(bundle.package_dir, tmp_path / "first-package")
    bundle.install_package(first_package)

    pending = remote_root / "data" / "pending-shortlist.json"
    changed = json.loads(pending.read_text(encoding="utf-8"))
    changed["opportunities"][0]["job"]["title"] = "Mutated title"
    pending.write_text(json.dumps(changed), encoding="utf-8")
    bundle.write_manifest(
        {
            **_authority(),
            "run_id": 43,
            "run_attempt": 1,
            "stage": "deep",
        }
    )
    changed_package = shutil.copytree(
        bundle.package_dir, tmp_path / "changed-package"
    )

    local_root = tmp_path / "local"
    handoff = LocalHandoffService(
        root=local_root,
        workflow=OpportunityWorkflow(
            store=JsonOpportunityStore(
                local_root / "data" / "private" / "opportunities"
            ),
            official_source=LocalOfficialSource(vacancy),
            evaluator=NoModelOpportunityEvaluator(),
            clock=FixedClock(),
        ),
        grading=DeepGradingService(
            provider=GradingProvider(),
            store=DeepGradeStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
    )
    handoff.import_bundle(first_package)
    local_pending = local_root / "data" / "pending-shortlist.json"
    local_head = local_root / "data" / "actions-state-head.json"
    pending_before = local_pending.read_bytes()
    head_before = local_head.read_bytes()

    with pytest.raises(ValueError, match="identity content mismatch"):
        handoff.import_bundle(changed_package)

    assert local_pending.read_bytes() == pending_before
    assert local_head.read_bytes() == head_before


@pytest.mark.parametrize(
    "authority",
    (
        {},
        {"repository": "", "workflow": "run.yml", "branch": "main"},
        {"repository": None, "workflow": "run.yml", "branch": "main"},
        {"repository": "example-org/job-agent", "branch": "main"},
    ),
)
def test_local_handoff_requires_complete_non_empty_authority(tmp_path, authority):
    with pytest.raises(ValueError, match="repository, workflow, and branch"):
        LocalHandoffService(
            root=tmp_path,
            workflow=object(),
            grading=object(),
            profile=_profile(),
            expected_authority=authority,
        )


def test_handoff_identity_is_canonical_and_rejects_non_hash_versions():
    first = HandoffIdentity("a@b", "sha256:" + ("1" * 64))
    second = HandoffIdentity("a", "sha256:" + ("2" * 64))

    assert first.canonical != second.canonical
    assert first.storage_key != second.storage_key
    assert first.storage_key.startswith("sha256:")

    with pytest.raises(ValueError, match="canonical sha256"):
        HandoffIdentity("a", "b@c")


def test_local_handoff_fsyncs_private_state_containing_directory(
    tmp_path, monkeypatch
):
    vacancy = _official_vacancy()
    expected_version = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at="2026-07-16T10:30:00+00:00",
    ).version
    bundle, _ = _remote_bundle(
        tmp_path / "remote", official_version=expected_version
    )
    synced: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(file_descriptor):
        mode = os.fstat(file_descriptor).st_mode
        synced.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(file_descriptor)

    monkeypatch.setattr(local_handoff.os, "fsync", recording_fsync)
    local_root = tmp_path / "local"
    handoff = LocalHandoffService(
        root=local_root,
        workflow=OpportunityWorkflow(
            store=JsonOpportunityStore(
                local_root / "data" / "private" / "opportunities"
            ),
            official_source=LocalOfficialSource(vacancy),
            evaluator=NoModelOpportunityEvaluator(),
            clock=FixedClock(),
        ),
        grading=DeepGradingService(
            provider=GradingProvider(),
            store=DeepGradeStore(local_root / "data" / "deep-grades"),
        ),
        profile=_profile(),
        expected_authority=_authority(),
    )

    handoff.import_bundle(bundle.package_dir)

    assert synced == ["file", "directory"]


def test_state_bundle_rejects_extra_fields_from_otherwise_valid_deep_grade(tmp_path):
    grading = DeepGradingService(
        provider=GradingProvider(),
        store=DeepGradeStore(tmp_path / "data" / "deep-grades"),
    )
    grading.grade(
        {
            "stable_id": "linkedin:42",
            "verification_status": "verified",
            "official_vacancy_version": "sha256:" + ("1" * 64),
            "retrieved_at": "2026-07-16T10:30:00+00:00",
            "official_description": "Official Python role",
            "requirements": ["Python"],
            "location": "Zurich",
        },
        _profile(),
    )
    grade_path = next((tmp_path / "data" / "deep-grades").glob("*.json"))
    wrong_name = grade_path.with_name("wrong-grade-name.json")
    grade_path.rename(wrong_name)
    with pytest.raises(ValueError, match="artifact identity"):
        StateBundle(tmp_path).write_manifest()
    wrong_name.rename(grade_path)
    StateBundle(tmp_path).write_manifest()
    payload = json.loads(grade_path.read_text(encoding="utf-8"))
    payload["candidate_profile"] = {"identity_document": "passport.pdf"}
    grade_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="non-canonical public fields"):
        StateBundle(tmp_path).write_manifest()
