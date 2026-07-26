"""Durably controlled execution boundary for owner-local capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import re
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from local_worker_store import (
    CapabilityClaimState,
    CapabilityClaimStatus,
    CapabilityExecutionClaim,
    LocalWorkerStore,
    ReconciliationDecision,
    ReconciliationOutcome,
    StaleResumeGeneration,
    WorkerCommand,
    WorkerControlState,
)


class WorkerCapability(Protocol):
    def recompute(self, resume_generation: int) -> None: ...

    def run_once(self, execution: CapabilityExecution) -> None: ...

    def status(self) -> Mapping[str, Any]: ...


class WorkerLogger(Protocol):
    def info(self, event: str, **fields: Any) -> None: ...

    def error(self, event: str, error: BaseException, **fields: Any) -> None: ...


class CapabilityReconciliationVerifier(Protocol):
    def verify(
        self, *, capability: str, actor: str, provenance: str
    ) -> ReconciliationDecision: ...


class WorkerPaused(RuntimeError):
    pass


class WorkerStopped(RuntimeError):
    pass


class CapabilityExecutionUnavailable(RuntimeError):
    def __init__(self, capability: str, status: CapabilityClaimStatus) -> None:
        super().__init__(f"Capability {capability} is {status.value}")
        self.capability = capability
        self.status = status


class ExecutionGate:
    """Fail closed at the boundary of every local capability call."""

    def __init__(self, store: LocalWorkerStore) -> None:
        self._store = store

    def checkpoint(self, *, resume_generation: int | None = None) -> WorkerControlState:
        state = self._store.read()
        if state.command == WorkerCommand.PAUSE:
            raise WorkerPaused("Local worker is paused")
        if state.command == WorkerCommand.STOP:
            raise WorkerStopped("Local worker is stopped")
        if (
            resume_generation is not None
            and state.resume_generation != resume_generation
        ):
            raise StaleResumeGeneration("Local worker resume generation changed")
        return state


class CapabilityExecution:
    """Generation-bound gate that capabilities call before external actions."""

    def __init__(
        self,
        *,
        gate: ExecutionGate,
        store: LocalWorkerStore,
        claim: CapabilityExecutionClaim,
        lease_duration: timedelta,
    ) -> None:
        self._gate = gate
        self._store = store
        self._claim = claim
        self._lease_duration = lease_duration
        self._action_boundary_crossed = False

    @property
    def action_boundary_crossed(self) -> bool:
        return self._action_boundary_crossed

    def checkpoint(self, *, external_action: bool = True) -> WorkerControlState:
        state = self._store.checkpoint_capability(
            self._claim, lease_duration=self._lease_duration
        )
        if state is None:
            raise StaleResumeGeneration("Capability execution lease expired")
        if state.command == WorkerCommand.PAUSE:
            raise WorkerPaused("Local worker is paused")
        if state.command == WorkerCommand.STOP:
            raise WorkerStopped("Local worker is stopped")
        if state.resume_generation != self._claim.resume_generation:
            raise StaleResumeGeneration("Local worker resume generation changed")
        if external_action:
            self._action_boundary_crossed = True
        return state


class LocalWorker:
    def __init__(
        self,
        *,
        store: LocalWorkerStore,
        capabilities: Mapping[str, WorkerCapability],
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval: float = 1.0,
        logger: WorkerLogger | None = None,
        execution_lease: timedelta = timedelta(minutes=5),
        worker_id: str | None = None,
        heartbeat_ttl: timedelta = timedelta(seconds=30),
        reconciliation_verifiers: Mapping[str, CapabilityReconciliationVerifier]
        | None = None,
        safe_retry_capabilities: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._capabilities = dict(capabilities)
        if any(not _CAPABILITY_NAME.fullmatch(name) for name in self._capabilities):
            raise ValueError("Capability names must be safe identifiers")
        if poll_interval <= 0:
            raise ValueError("Poll interval must be positive")
        if execution_lease <= timedelta(0):
            raise ValueError("Execution lease must be positive")
        if heartbeat_ttl <= timedelta(0):
            raise ValueError("Heartbeat TTL must be positive")
        self._gate = ExecutionGate(store)
        self._sleep = sleeper
        self._poll_interval = poll_interval
        self._logger = logger or _NullLogger()
        self._execution_lease = execution_lease
        self._worker_id = worker_id or uuid4().hex
        self._heartbeat_ttl = heartbeat_ttl
        self._reconciliation_verifiers = dict(reconciliation_verifiers or {})
        if any(
            not _CAPABILITY_NAME.fullmatch(name)
            for name in self._reconciliation_verifiers
        ):
            raise ValueError("Reconciliation capability names must be safe identifiers")
        self._safe_retry_capabilities = dict(safe_retry_capabilities or {})
        if any(
            not _CAPABILITY_NAME.fullmatch(name)
            or name not in self._capabilities
            or not evidence.strip()
            for name, evidence in self._safe_retry_capabilities.items()
        ):
            raise ValueError(
                "Safe-retry capabilities require configured names and evidence"
            )

    def control(self, command: WorkerCommand) -> dict[str, Any]:
        state = self._store.set_command(command)
        self._logger.info(
            "worker.control_changed",
            state=state.command.value,
            resume_generation=state.resume_generation,
        )
        return self.status()

    def reconcile_capability(
        self, capability: str, *, actor: str, provenance: str
    ) -> ReconciliationDecision:
        if not _CAPABILITY_NAME.fullmatch(capability):
            raise ValueError("Reconciliation capability name is invalid")
        if not actor.strip() or not provenance.strip():
            raise ValueError("Reconciliation actor and provenance are required")
        verifier = self._reconciliation_verifiers.get(capability)
        if verifier is None:
            raise ValueError("No reconciliation verifier is configured")
        decision = verifier.verify(
            capability=capability,
            actor=actor,
            provenance=provenance,
        )
        if (
            decision.capability != capability
            or decision.actor != actor
            or decision.provenance != provenance
        ):
            raise ValueError("Reconciliation decision scope does not match request")
        self._store.reconcile_capability_claim(decision)
        self._logger.info(
            "worker.capability_reconciled",
            capability=capability,
            outcome=decision.outcome.value,
            actor=actor,
            provenance=provenance,
        )
        return decision

    def execute_gated_action(
        self,
        capability: str,
        action: Callable[[CapabilityExecution], Any],
    ) -> Any:
        """Run a callback under a claim; the callback gates each external effect."""

        if not _CAPABILITY_NAME.fullmatch(capability):
            raise ValueError("Gated action capability name is invalid")
        state = self._gate.checkpoint()
        attempt = self._store.claim_capability(
            capability,
            state.resume_generation,
            owner=self._worker_id,
            lease_duration=self._execution_lease,
        )
        if attempt.status != CapabilityClaimStatus.ACQUIRED:
            raise CapabilityExecutionUnavailable(capability, attempt.status)
        claim = attempt.claim
        assert claim is not None
        execution = CapabilityExecution(
            gate=self._gate,
            store=self._store,
            claim=claim,
            lease_duration=self._execution_lease,
        )
        try:
            execution.checkpoint(external_action=False)
            result = action(execution)
            self._store.complete_capability(claim)
            return result
        except Exception:
            self._resolve_failed_claim(claim, execution)
            raise

    def run_once(self) -> dict[str, Any]:
        self._store.record_heartbeat(self._worker_id)
        try:
            self._gate.checkpoint()
        except (WorkerPaused, WorkerStopped):
            return self.status()
        for name, capability in self._capabilities.items():
            try:
                state = self._gate.checkpoint()
            except (WorkerPaused, WorkerStopped):
                break
            if (
                state.capability_claims.get(name)
                == CapabilityClaimState.UNCERTAIN
                and name in self._safe_retry_capabilities
            ):
                self._store.reconcile_capability_claim(
                    ReconciliationDecision(
                        capability=name,
                        outcome=ReconciliationOutcome.RETRY_VERIFIED,
                        evidence=self._safe_retry_capabilities[name],
                        provenance="local-worker:safe-retry-envelope",
                        decided_at=datetime.now(timezone.utc).isoformat(),
                        actor="system:worker",
                    )
                )
                state = self._gate.checkpoint()
            try:
                claim_attempt = self._store.claim_capability(
                    name,
                    state.resume_generation,
                    owner=self._worker_id,
                    lease_duration=self._execution_lease,
                )
            except StaleResumeGeneration:
                break
            if claim_attempt.status != CapabilityClaimStatus.ACQUIRED:
                continue
            claim = claim_attempt.claim
            assert claim is not None
            execution: CapabilityExecution | None = None
            try:
                if state.capability_generations.get(name, -1) < state.resume_generation:
                    self._gate.checkpoint()
                    capability.recompute(state.resume_generation)
                    self._store.mark_recomputed(
                        name, state.resume_generation, claim=claim
                    )
                self._gate.checkpoint(resume_generation=state.resume_generation)
                execution = CapabilityExecution(
                    gate=self._gate,
                    store=self._store,
                    claim=claim,
                    lease_duration=self._execution_lease,
                )
                capability.run_once(execution)
                self._store.complete_capability(claim)
                self._logger.info("worker.capability_completed", capability=name)
            except (StaleResumeGeneration, WorkerPaused, WorkerStopped):
                self._resolve_failed_claim(claim, execution)
                break
            except Exception as error:
                self._resolve_failed_claim(claim, execution)
                self._logger.error("worker.capability_failed", error, capability=name)
        return self.status()

    def _resolve_failed_claim(
        self,
        claim: CapabilityExecutionClaim,
        execution: CapabilityExecution | None,
    ) -> None:
        try:
            if execution is not None and execution.action_boundary_crossed:
                self._store.mark_capability_uncertain(claim)
            else:
                self._store.abandon_capability(claim)
        except StaleResumeGeneration:
            pass

    def serve(self) -> dict[str, Any]:
        self._logger.info("worker.started")
        while self._store.read().command != WorkerCommand.STOP:
            self.run_once()
            if self._store.read().command != WorkerCommand.STOP:
                self._sleep(self._poll_interval)
        self._logger.info("worker.stopped")
        return self.status()

    def status(self) -> dict[str, Any]:
        state = self._store.read()
        capabilities = {
            name: _safe_capability_status(capability)
            for name, capability in self._capabilities.items()
        }
        for name, claim_state in state.capability_claims.items():
            if claim_state != CapabilityClaimState.UNCERTAIN:
                continue
            reported = dict(capabilities.get(name, {}))
            reported["state"] = "uncertain"
            reported["healthy"] = False
            capabilities[name] = reported
        if state.command == WorkerCommand.PAUSE:
            health = "paused"
        elif state.command == WorkerCommand.STOP:
            health = "stopped"
        elif not capabilities:
            health = "unwired"
        elif not self._store.heartbeat_is_live(self._heartbeat_ttl):
            health = "degraded"
        elif all(item.get("healthy") is True for item in capabilities.values()):
            health = "healthy"
        else:
            health = "degraded"
        return {
            "state": state.command.value,
            "health": health,
            "resume_generation": state.resume_generation,
            "heartbeat_at": state.heartbeat_at,
            "capabilities": capabilities,
        }


_CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_CAPABILITY_STATES = {
    "blocked",
    "degraded",
    "idle",
    "paused",
    "ready",
    "running",
    "stopped",
    "uncertain",
    "unavailable",
}


def _safe_capability_status(capability: WorkerCapability) -> dict[str, Any]:
    try:
        reported = capability.status()
    except Exception:
        return {"state": "unavailable", "healthy": False}
    safe: dict[str, Any] = {}
    state = reported.get("state")
    if isinstance(state, str) and state in _CAPABILITY_STATES:
        safe["state"] = state
    healthy = reported.get("healthy")
    if isinstance(healthy, bool):
        safe["healthy"] = healthy
    for field in ("pending", "active_applications"):
        value = reported.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            safe[field] = value
    return safe


class _NullLogger:
    def info(self, event: str, **fields: Any) -> None:
        return None

    def error(self, event: str, error: BaseException, **fields: Any) -> None:
        return None


__all__ = [
    "CapabilityExecutionUnavailable",
    "CapabilityReconciliationVerifier",
    "CapabilityExecution",
    "ExecutionGate",
    "LocalWorker",
    "WorkerCapability",
    "WorkerCommand",
    "WorkerLogger",
    "WorkerPaused",
    "WorkerStopped",
]
