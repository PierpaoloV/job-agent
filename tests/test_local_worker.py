from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import threading

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import local_worker_store
from local_worker import CapabilityExecution, LocalWorker, WorkerCommand
from local_worker_store import (
    CapabilityClaimStatus,
    LocalWorkerStore,
    ReconciliationDecision,
    ReconciliationOutcome,
    StaleResumeGeneration,
)
from redacted_logging import RedactedStructuredLogger


class RecordingCapability:
    def __init__(self) -> None:
        self.recomputed: list[int] = []
        self.runs = 0

    def recompute(self, resume_generation: int) -> None:
        self.recomputed.append(resume_generation)

    def run_once(self, execution: CapabilityExecution) -> None:
        execution.checkpoint()
        self.runs += 1

    def status(self) -> dict:
        return {
            "state": "ready",
            "healthy": True,
            "pending": 1,
            "active_applications": 1,
            "token": "telegram-secret-must-not-leak",
            "diagnosis": "sensitive-health-value",
        }


def _hold_capability_claim(state_path, acquired, release, result) -> None:
    store = LocalWorkerStore(Path(state_path))
    attempt = store.claim_capability(
        "applications",
        0,
        owner="child-process",
        lease_duration=timedelta(seconds=30),
    )
    result.put(attempt.status.value)
    if attempt.claim is not None:
        acquired.set()
        release.wait(timeout=5)
        store.complete_capability(attempt.claim)


def test_pause_is_durable_and_one_resume_generation_recomputes_work_once(tmp_path):
    state_path = tmp_path / "worker-state.json"
    capability = RecordingCapability()
    first = LocalWorker(
        store=LocalWorkerStore(state_path),
        capabilities={"applications": capability},
    )

    first.control(WorkerCommand.PAUSE)
    first.run_once()

    assert capability.runs == 0

    restarted = LocalWorker(
        store=LocalWorkerStore(state_path),
        capabilities={"applications": capability},
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        states = list(
            executor.map(lambda _: restarted.control(WorkerCommand.RESUME), range(16))
        )
    restarted.run_once()
    restarted.run_once()

    assert {state["resume_generation"] for state in states} == {1}
    assert capability.recomputed == [1]
    assert capability.runs == 2


def test_execution_gate_is_checked_again_before_the_next_capability(tmp_path):
    state_path = tmp_path / "worker-state.json"
    store = LocalWorkerStore(state_path)
    second = RecordingCapability()

    class PausingCapability(RecordingCapability):
        def run_once(self, execution: CapabilityExecution) -> None:
            super().run_once(execution)
            store.set_command(WorkerCommand.PAUSE)

    first = PausingCapability()
    worker = LocalWorker(
        store=store,
        capabilities={"discovery": first, "applications": second},
    )

    worker.run_once()

    assert first.runs == 1
    assert second.recomputed == []
    assert second.runs == 0
    assert worker.status()["state"] == "pause"


def test_status_exposes_only_allowlisted_operational_fields(tmp_path):
    worker = LocalWorker(
        store=LocalWorkerStore(tmp_path / "worker-state.json"),
        capabilities={"applications": RecordingCapability()},
    )

    worker.run_once()
    status = worker.status()

    assert status == {
        "state": "resume",
        "health": "healthy",
        "resume_generation": 0,
        "heartbeat_at": status["heartbeat_at"],
        "capabilities": {
            "applications": {
                "state": "ready",
                "healthy": True,
                "pending": 1,
                "active_applications": 1,
            }
        },
    }
    assert "telegram-secret-must-not-leak" not in repr(status)
    assert "sensitive-health-value" not in repr(status)


def test_empty_worker_is_fail_closed_as_unwired(tmp_path):
    worker = LocalWorker(
        store=LocalWorkerStore(tmp_path / "worker-state.json"),
        capabilities={},
    )

    assert worker.status()["health"] == "unwired"
    assert worker.run_once()["health"] == "unwired"


def test_health_requires_a_live_durable_heartbeat(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    state_path = tmp_path / "worker-state.json"
    store = LocalWorkerStore(state_path, now=lambda: current[0])
    worker = LocalWorker(
        store=store,
        capabilities={"applications": RecordingCapability()},
        heartbeat_ttl=timedelta(seconds=10),
    )

    assert worker.status()["health"] == "degraded"
    live = worker.run_once()
    assert live["health"] == "healthy"
    assert live["heartbeat_at"] == current[0].isoformat()

    current[0] += timedelta(seconds=11)
    restarted = LocalWorker(
        store=LocalWorkerStore(state_path, now=lambda: current[0]),
        capabilities={"applications": RecordingCapability()},
        heartbeat_ttl=timedelta(seconds=10),
    )
    assert restarted.status()["health"] == "degraded"


def test_serve_uses_injected_wait_and_exits_after_durable_stop(tmp_path):
    capability = RecordingCapability()
    waits: list[float] = []
    worker = None

    def wait(seconds: float) -> None:
        waits.append(seconds)
        assert worker is not None
        worker.control(WorkerCommand.STOP)

    worker = LocalWorker(
        store=LocalWorkerStore(tmp_path / "worker-state.json"),
        capabilities={"applications": capability},
        sleeper=wait,
        poll_interval=0.25,
    )

    result = worker.serve()

    assert capability.runs == 1
    assert waits == [0.25]
    assert result["state"] == "stop"


def test_capability_failure_is_redacted_and_does_not_skip_other_work(tmp_path):
    output = StringIO()

    class FailingCapability(RecordingCapability):
        def run_once(self, execution: CapabilityExecution) -> None:
            execution.checkpoint()
            raise RuntimeError("Bearer worker-token-789 failed for private@example.com")

    healthy = RecordingCapability()
    worker = LocalWorker(
        store=LocalWorkerStore(tmp_path / "worker-state.json"),
        capabilities={
            "failing": FailingCapability(),
            "applications": healthy,
        },
        logger=RedactedStructuredLogger(output, secrets=("worker-token-789",)),
    )

    worker.run_once()

    assert healthy.runs == 1
    assert "worker.capability_failed" in output.getvalue()
    assert "worker-token-789" not in output.getvalue()
    assert "private@example.com" not in output.getvalue()


def test_resume_generation_change_during_recompute_blocks_stale_work(tmp_path):
    store = LocalWorkerStore(tmp_path / "worker-state.json")

    class ResumeDuringRecompute(RecordingCapability):
        def recompute(self, resume_generation: int) -> None:
            super().recompute(resume_generation)
            if resume_generation == 0:
                store.set_command(WorkerCommand.PAUSE)
                store.set_command(WorkerCommand.RESUME)

    capability = ResumeDuringRecompute()
    worker = LocalWorker(
        store=store,
        capabilities={"applications": capability},
    )

    worker.run_once()
    assert capability.runs == 0

    worker.run_once()
    assert capability.recomputed == [0, 1]
    assert capability.runs == 1


def test_control_change_between_gate_and_claim_is_handled_fail_closed(tmp_path):
    class PausingBeforeClaimStore(LocalWorkerStore):
        def claim_capability(self, *args, **kwargs):
            self.set_command(WorkerCommand.PAUSE)
            return super().claim_capability(*args, **kwargs)

    capability = RecordingCapability()
    worker = LocalWorker(
        store=PausingBeforeClaimStore(tmp_path / "worker-state.json"),
        capabilities={"applications": capability},
    )

    status = worker.run_once()

    assert status["state"] == "pause"
    assert capability.recomputed == []
    assert capability.runs == 0


def test_capability_execution_is_exclusive_across_worker_instances(tmp_path):
    state_path = tmp_path / "worker-state.json"
    entered = threading.Event()
    release = threading.Event()

    class BlockingCapability(RecordingCapability):
        def recompute(self, resume_generation: int) -> None:
            super().recompute(resume_generation)
            entered.set()
            release.wait(timeout=2)

    first_capability = BlockingCapability()
    competing_capability = RecordingCapability()
    first = LocalWorker(
        store=LocalWorkerStore(state_path),
        capabilities={"applications": first_capability},
    )
    competing = LocalWorker(
        store=LocalWorkerStore(state_path),
        capabilities={"applications": competing_capability},
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_run = executor.submit(first.run_once)
        assert entered.wait(timeout=1)
        competing.run_once()
        release.set()
        first_run.result(timeout=2)

    assert first_capability.recomputed == [0]
    assert first_capability.runs == 1
    assert competing_capability.recomputed == []
    assert competing_capability.runs == 0


def test_pause_after_preparation_blocks_capability_external_action(tmp_path):
    store = LocalWorkerStore(tmp_path / "worker-state.json")
    prepared = threading.Event()
    continue_to_boundary = threading.Event()
    external_actions: list[str] = []

    class BoundaryCapability(RecordingCapability):
        def run_once(self, execution: CapabilityExecution) -> None:
            prepared.set()
            continue_to_boundary.wait(timeout=2)
            execution.checkpoint()
            external_actions.append("sent")

    worker = LocalWorker(
        store=store,
        capabilities={"applications": BoundaryCapability()},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(worker.run_once)
        assert prepared.wait(timeout=1)
        store.set_command(WorkerCommand.PAUSE)
        continue_to_boundary.set()
        running.result(timeout=2)

    assert external_actions == []
    assert worker.status()["state"] == "pause"


def test_capability_claim_is_exclusive_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "worker-state.json"
    acquired = context.Event()
    release = context.Event()
    result = context.Queue()
    process = context.Process(
        target=_hold_capability_claim,
        args=(str(state_path), acquired, release, result),
    )
    process.start()
    try:
        assert acquired.wait(timeout=3)
        competing = LocalWorkerStore(state_path).claim_capability(
            "applications",
            0,
            owner="parent-process",
            lease_duration=timedelta(seconds=30),
        )
        assert result.get(timeout=1) == CapabilityClaimStatus.ACQUIRED.value
        assert competing.status == CapabilityClaimStatus.BUSY
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
    assert process.exitcode == 0


def test_expired_claim_is_uncertain_until_verified_reconciliation_is_audited(
    tmp_path,
):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    state_path = tmp_path / "worker-state.json"
    store = LocalWorkerStore(state_path, now=lambda: current[0])
    acquired = store.claim_capability(
        "applications",
        0,
        owner="crashed-worker",
        lease_duration=timedelta(seconds=1),
    )
    assert acquired.claim is not None

    current[0] += timedelta(seconds=2)
    decision = ReconciliationDecision(
        capability="applications",
        outcome=ReconciliationOutcome.RETRY_VERIFIED,
        evidence="ATS and mailbox show no application side effect",
        provenance="telegram:command",
        decided_at=current[0].isoformat(),
        actor="42",
    )

    class Verifier:
        def verify(self, *, capability, actor, provenance):
            assert (capability, actor, provenance) == (
                "applications",
                "42",
                "telegram:command",
            )
            return decision

    capability = RecordingCapability()
    restarted_store = LocalWorkerStore(state_path, now=lambda: current[0])
    restarted = LocalWorker(
        store=restarted_store,
        capabilities={"applications": capability},
        reconciliation_verifiers={"applications": Verifier()},
        execution_lease=timedelta(seconds=1),
    )

    blocked = restarted.run_once()

    assert capability.runs == 0
    assert blocked["health"] == "degraded"
    assert blocked["capabilities"]["applications"] == {
        "state": "uncertain",
        "healthy": False,
        "pending": 1,
        "active_applications": 1,
    }

    verified = restarted.reconcile_capability(
        "applications", actor="42", provenance="telegram:command"
    )
    restarted.run_once()

    assert verified == decision
    assert restarted_store.reconciliation_audit() == (decision,)
    assert capability.runs == 1


def test_status_atomically_ages_an_expired_processing_claim_to_uncertain(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    state_path = tmp_path / "worker-state.json"
    store = LocalWorkerStore(state_path, now=lambda: current[0])
    store.record_heartbeat("crashed-worker")
    store.claim_capability(
        "applications",
        0,
        owner="crashed-worker",
        lease_duration=timedelta(seconds=1),
    )
    current[0] += timedelta(seconds=2)
    worker = LocalWorker(
        store=store,
        capabilities={"applications": RecordingCapability()},
        heartbeat_ttl=timedelta(seconds=30),
    )

    status = worker.status()

    assert status["health"] == "degraded"
    assert status["capabilities"]["applications"]["state"] == "uncertain"
    assert store.read().capability_claims["applications"].value == "uncertain"


def test_failed_reconciliation_verification_remains_uncertain_and_is_audited(
    tmp_path,
):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    state_path = tmp_path / "worker-state.json"
    store = LocalWorkerStore(state_path, now=lambda: current[0])
    store.claim_capability(
        "applications",
        0,
        owner="crashed-worker",
        lease_duration=timedelta(seconds=1),
    )
    current[0] += timedelta(seconds=2)
    store.claim_capability(
        "applications",
        0,
        owner="restarted-worker",
        lease_duration=timedelta(seconds=1),
    )
    decision = ReconciliationDecision(
        capability="applications",
        outcome=ReconciliationOutcome.RETRY_BLOCKED,
        evidence="ATS outcome could not be determined",
        provenance="telegram:callback",
        decided_at=current[0].isoformat(),
        actor="42",
    )

    class Verifier:
        def verify(self, **_):
            return decision

    worker = LocalWorker(
        store=store,
        capabilities={"applications": RecordingCapability()},
        reconciliation_verifiers={"applications": Verifier()},
    )

    assert (
        worker.reconcile_capability(
            "applications", actor="42", provenance="telegram:callback"
        )
        == decision
    )
    assert worker.status()["capabilities"]["applications"]["state"] == "uncertain"
    assert store.reconciliation_audit() == (decision,)


def test_capability_claim_completion_uses_owner_token_cas(tmp_path):
    store = LocalWorkerStore(tmp_path / "worker-state.json")
    attempt = store.claim_capability(
        "applications",
        0,
        owner="worker-one",
        lease_duration=timedelta(seconds=30),
    )
    assert attempt.claim is not None

    with pytest.raises(StaleResumeGeneration):
        store.complete_capability(replace(attempt.claim, token="stolen-token"))

    competing = store.claim_capability(
        "applications",
        0,
        owner="worker-two",
        lease_duration=timedelta(seconds=30),
    )
    assert competing.status == CapabilityClaimStatus.BUSY


def test_control_state_fsyncs_temporary_file_and_containing_directory(
    tmp_path, monkeypatch
):
    synced_kinds: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        synced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(file_descriptor)

    monkeypatch.setattr(local_worker_store.os, "fsync", recording_fsync)

    LocalWorkerStore(tmp_path / "state" / "worker-state.json").set_command(
        WorkerCommand.PAUSE
    )

    assert synced_kinds == ["file", "directory"]


def test_control_state_is_owner_only_before_any_bytes_are_written(
    tmp_path, monkeypatch
):
    observed_modes: list[int] = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777):
        file_descriptor = real_open(path, flags, mode)
        if str(path).endswith(".tmp"):
            observed_modes.append(stat.S_IMODE(os.fstat(file_descriptor).st_mode))
        return file_descriptor

    monkeypatch.setattr(local_worker_store.os, "open", recording_open)
    LocalWorkerStore(tmp_path / "state" / "worker-state.json").set_command(
        WorkerCommand.PAUSE
    )

    assert observed_modes
    assert set(observed_modes) == {0o600}
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
