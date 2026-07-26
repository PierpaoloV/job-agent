"""Owner-local, atomic control state for the macOS worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import secrets
import threading
from typing import Any, Callable


STATE_VERSION = "job-agent.local-worker.v1"
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class WorkerCommand(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"


class StaleResumeGeneration(RuntimeError):
    pass


class CapabilityClaimStatus(str, Enum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    UNCERTAIN = "uncertain"


class CapabilityClaimState(str, Enum):
    PROCESSING = "processing"
    UNCERTAIN = "uncertain"


class ReconciliationOutcome(str, Enum):
    RETRY_VERIFIED = "retry_verified"
    RETRY_BLOCKED = "retry_blocked"


@dataclass(frozen=True)
class ReconciliationDecision:
    capability: str
    outcome: ReconciliationOutcome
    evidence: str
    provenance: str
    decided_at: str
    actor: str

    def __post_init__(self) -> None:
        if not self.capability.strip():
            raise ValueError("Reconciliation capability is required")
        object.__setattr__(self, "outcome", ReconciliationOutcome(self.outcome))
        for field in ("evidence", "provenance", "actor"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"Reconciliation {field} is required")
        _parse_datetime(self.decided_at)


@dataclass(frozen=True)
class CapabilityExecutionClaim:
    capability: str
    owner: str
    token: str
    resume_generation: int
    expires_at: str


@dataclass(frozen=True)
class CapabilityClaimAttempt:
    status: CapabilityClaimStatus
    claim: CapabilityExecutionClaim | None = None


@dataclass(frozen=True)
class WorkerControlState:
    command: WorkerCommand
    resume_generation: int
    updated_at: str | None
    capability_generations: dict[str, int]
    heartbeat_owner: str | None
    heartbeat_at: str | None
    capability_claims: dict[str, CapabilityClaimState]


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "control": {
            "command": WorkerCommand.RESUME.value,
            "resume_generation": 0,
            "updated_at": None,
        },
        "capability_generations": {},
        "capability_claims": {},
        "reconciliation_audit": [],
        "heartbeat": {"owner": None, "at": None},
    }


class LocalWorkerStore:
    """Persist worker controls behind a process and thread lock."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def read(self) -> WorkerControlState:
        def inspect(state: dict[str, Any]) -> WorkerControlState:
            _age_expired_claims(state, self._now())
            return _control_state(state)

        return self._transact(inspect)

    def set_command(self, command: WorkerCommand) -> WorkerControlState:
        command = WorkerCommand(command)

        def update(state: dict[str, Any]) -> WorkerControlState:
            control = state["control"]
            previous = WorkerCommand(str(control["command"]))
            if command == WorkerCommand.RESUME and previous != WorkerCommand.RESUME:
                control["resume_generation"] = int(control["resume_generation"]) + 1
            control["command"] = command.value
            control["updated_at"] = self._now().isoformat()
            return _control_state(state)

        return self._transact(update)

    def record_heartbeat(self, owner: str) -> WorkerControlState:
        if not owner.strip():
            raise ValueError("Heartbeat owner is required")

        def update(state: dict[str, Any]) -> WorkerControlState:
            state["heartbeat"] = {
                "owner": owner,
                "at": self._now().isoformat(),
            }
            return _control_state(state)

        return self._transact(update)

    def heartbeat_is_live(self, ttl: timedelta) -> bool:
        if ttl <= timedelta(0):
            raise ValueError("Heartbeat TTL must be positive")
        state = self.read()
        if state.heartbeat_at is None:
            return False
        age = self._now() - _parse_datetime(state.heartbeat_at)
        return timedelta(0) <= age <= ttl

    def mark_recomputed(
        self,
        capability: str,
        resume_generation: int,
        *,
        claim: CapabilityExecutionClaim,
    ) -> WorkerControlState:
        if not capability.strip():
            raise ValueError("Capability name is required")

        def update(state: dict[str, Any]) -> WorkerControlState:
            current = int(state["control"]["resume_generation"])
            command = WorkerCommand(str(state["control"]["command"]))
            if current != resume_generation or command != WorkerCommand.RESUME:
                raise StaleResumeGeneration(
                    "Resume generation changed before recompute completed"
                )
            _require_claim(state, claim)
            state["capability_generations"][capability] = resume_generation
            return _control_state(state)

        return self._transact(update)

    def claim_capability(
        self,
        capability: str,
        resume_generation: int,
        *,
        owner: str,
        lease_duration: timedelta,
    ) -> CapabilityClaimAttempt:
        if not capability.strip():
            raise ValueError("Capability name is required")
        if not owner.strip():
            raise ValueError("Claim owner is required")
        if lease_duration <= timedelta(0):
            raise ValueError("Lease duration must be positive")

        def update(state: dict[str, Any]) -> CapabilityClaimAttempt:
            control = state["control"]
            if (
                WorkerCommand(str(control["command"])) != WorkerCommand.RESUME
                or int(control["resume_generation"]) != resume_generation
            ):
                raise StaleResumeGeneration(
                    "Resume generation changed before capability claim"
                )
            claims = state["capability_claims"]
            existing = claims.get(capability)
            now = self._now()
            if existing is not None:
                if str(existing.get("state")) == CapabilityClaimStatus.UNCERTAIN.value:
                    return CapabilityClaimAttempt(CapabilityClaimStatus.UNCERTAIN)
                expires_at = _parse_datetime(str(existing["expires_at"]))
                if expires_at > now:
                    return CapabilityClaimAttempt(CapabilityClaimStatus.BUSY)
                existing["state"] = CapabilityClaimStatus.UNCERTAIN.value
                existing["expired_at"] = now.isoformat()
                return CapabilityClaimAttempt(CapabilityClaimStatus.UNCERTAIN)

            claim = CapabilityExecutionClaim(
                capability=capability,
                owner=owner,
                token=secrets.token_urlsafe(24),
                resume_generation=resume_generation,
                expires_at=(now + lease_duration).isoformat(),
            )
            claims[capability] = {
                "state": "processing",
                "owner": claim.owner,
                "token": claim.token,
                "resume_generation": claim.resume_generation,
                "expires_at": claim.expires_at,
            }
            return CapabilityClaimAttempt(CapabilityClaimStatus.ACQUIRED, claim)

        return self._transact_any(update)

    def complete_capability(self, claim: CapabilityExecutionClaim) -> None:
        def update(state: dict[str, Any]) -> None:
            _require_claim(state, claim)
            del state["capability_claims"][claim.capability]

        self._transact_any(update)

    def checkpoint_capability(
        self,
        claim: CapabilityExecutionClaim,
        *,
        lease_duration: timedelta,
    ) -> WorkerControlState | None:
        """Atomically validate control/claim state and renew the execution lease."""

        def update(state: dict[str, Any]) -> WorkerControlState | None:
            control_state = _control_state(state)
            if (
                control_state.command != WorkerCommand.RESUME
                or control_state.resume_generation != claim.resume_generation
            ):
                return control_state
            stored = _require_claim(state, claim)
            now = self._now()
            if _parse_datetime(str(stored["expires_at"])) <= now:
                stored["state"] = CapabilityClaimStatus.UNCERTAIN.value
                stored["expired_at"] = now.isoformat()
                return None
            stored["expires_at"] = (now + lease_duration).isoformat()
            return control_state

        return self._transact_any(update)

    def mark_capability_uncertain(self, claim: CapabilityExecutionClaim) -> None:
        def update(state: dict[str, Any]) -> None:
            stored = _require_claim(state, claim)
            stored["state"] = CapabilityClaimStatus.UNCERTAIN.value
            stored["uncertain_at"] = self._now().isoformat()

        self._transact_any(update)

    def reconcile_capability_claim(self, decision: ReconciliationDecision) -> None:
        """Audit a typed verification and clear only verified-retry claims."""

        def update(state: dict[str, Any]) -> None:
            stored = state["capability_claims"].get(decision.capability)
            if not isinstance(stored, dict):
                raise ValueError("No uncertain capability claim to reconcile")
            if str(stored.get("state")) != CapabilityClaimStatus.UNCERTAIN.value:
                raise ValueError("Only uncertain capability claims can be reconciled")
            state["reconciliation_audit"].append(
                {
                    "capability": decision.capability,
                    "outcome": decision.outcome.value,
                    "evidence": decision.evidence,
                    "provenance": decision.provenance,
                    "decided_at": decision.decided_at,
                    "actor": decision.actor,
                }
            )
            if decision.outcome == ReconciliationOutcome.RETRY_VERIFIED:
                del state["capability_claims"][decision.capability]

        self._transact_any(update)

    def reconciliation_audit(self) -> tuple[ReconciliationDecision, ...]:
        state = self._load()
        return tuple(
            ReconciliationDecision(
                capability=str(item["capability"]),
                outcome=ReconciliationOutcome(str(item["outcome"])),
                evidence=str(item["evidence"]),
                provenance=str(item["provenance"]),
                decided_at=str(item["decided_at"]),
                actor=str(item["actor"]),
            )
            for item in state["reconciliation_audit"]
        )

    def abandon_capability(self, claim: CapabilityExecutionClaim) -> None:
        """Release a claim only when no external action may have crossed its gate."""

        self.complete_capability(claim)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_state()
        state = json.loads(self._path.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION:
            raise ValueError("Unsupported local worker state version")
        state.setdefault("capability_claims", {})
        state.setdefault("reconciliation_audit", [])
        state.setdefault("heartbeat", {"owner": None, "at": None})
        _control_state(state)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory = os.open(self._path.parent, directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _transact(
        self, operation: Callable[[dict[str, Any]], WorkerControlState]
    ) -> WorkerControlState:
        return self._transact_any(operation)

    def _transact_any(self, operation: Callable[[dict[str, Any]], Any]) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        thread_key = str(lock_path.resolve())
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(thread_key, threading.RLock())
        with thread_lock:
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    state = self._load()
                    result = operation(state)
                    self._save(state)
                    return result
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _age_expired_claims(state: dict[str, Any], now: datetime) -> None:
    claims = state.get("capability_claims", {})
    if not isinstance(claims, dict):
        raise ValueError("Invalid local worker capability claims")
    for stored in claims.values():
        if not isinstance(stored, dict):
            continue
        if str(stored.get("state")) != CapabilityClaimState.PROCESSING.value:
            continue
        if _parse_datetime(str(stored["expires_at"])) <= now:
            stored["state"] = CapabilityClaimState.UNCERTAIN.value
            stored["expired_at"] = now.isoformat()


def _control_state(state: dict[str, Any]) -> WorkerControlState:
    control = state.get("control")
    if not isinstance(control, dict):
        raise ValueError("Invalid local worker control state")
    generations = state.get("capability_generations")
    if not isinstance(generations, dict):
        raise ValueError("Invalid local worker capability generations")
    claims = state.get("capability_claims", {})
    if not isinstance(claims, dict):
        raise ValueError("Invalid local worker capability claims")
    heartbeat = state.get("heartbeat", {})
    if not isinstance(heartbeat, dict):
        raise ValueError("Invalid local worker heartbeat")
    resume_generation = int(control.get("resume_generation", -1))
    if resume_generation < 0:
        raise ValueError("Invalid local worker resume generation")
    normalized_generations = {
        str(name): int(generation) for name, generation in generations.items()
    }
    if any(generation < 0 for generation in normalized_generations.values()):
        raise ValueError("Invalid capability resume generation")
    normalized_claims = {
        str(name): CapabilityClaimState(str(claim["state"]))
        for name, claim in claims.items()
        if isinstance(claim, dict)
    }
    if len(normalized_claims) != len(claims):
        raise ValueError("Invalid local worker capability claim")
    updated_at = control.get("updated_at")
    heartbeat_owner = heartbeat.get("owner")
    heartbeat_at = heartbeat.get("at")
    if heartbeat_at is not None:
        _parse_datetime(str(heartbeat_at))
    return WorkerControlState(
        command=WorkerCommand(str(control["command"])),
        resume_generation=resume_generation,
        updated_at=None if updated_at is None else str(updated_at),
        capability_generations=normalized_generations,
        heartbeat_owner=(None if heartbeat_owner is None else str(heartbeat_owner)),
        heartbeat_at=None if heartbeat_at is None else str(heartbeat_at),
        capability_claims=normalized_claims,
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Claim timestamps must include a timezone")
    return parsed


def _require_claim(
    state: dict[str, Any], claim: CapabilityExecutionClaim
) -> dict[str, Any]:
    stored = state["capability_claims"].get(claim.capability)
    if not isinstance(stored, dict) or any(
        stored.get(field) != expected
        for field, expected in (
            ("owner", claim.owner),
            ("token", claim.token),
            ("resume_generation", claim.resume_generation),
        )
    ):
        raise StaleResumeGeneration("Capability claim is no longer owned")
    if str(stored.get("state")) != "processing":
        raise StaleResumeGeneration("Capability claim is not executable")
    return stored


__all__ = [
    "CapabilityClaimAttempt",
    "CapabilityClaimState",
    "CapabilityClaimStatus",
    "CapabilityExecutionClaim",
    "LocalWorkerStore",
    "ReconciliationDecision",
    "ReconciliationOutcome",
    "StaleResumeGeneration",
    "WorkerCommand",
    "WorkerControlState",
]
