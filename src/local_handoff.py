"""Fail-closed remote discovery handoff into local vacancy grading."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Mapping

from actions_state import StateBundle
from deep_grading_contract import DeepGradeResult, SanitizedProfessionalProfile
from deep_grading_service import DeepGradingService
from opportunity_domain import OfficialVacancySnapshot, Runtime, VerificationStatus
from opportunity_sources import OpportunityLead
from opportunity_workflow import OpportunityWorkflow
from vacancy_policy import VerificationState, verification_state
from workflow import NormalizedOpportunity, ShortlistArtifact


LOCAL_HANDOFF_STATE_VERSION = "job-agent.local-handoff-state.v1"
_CANONICAL_OFFICIAL_VERSION = re.compile(r"sha256:[0-9a-f]{64}")
_REQUIRED_AUTHORITY = ("repository", "workflow", "branch")


@dataclass(frozen=True)
class HandoffIdentity:
    stable_id: str
    official_vacancy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ValueError("Local handoff requires a stable id")
        if self.stable_id != self.stable_id.strip():
            raise ValueError("Local handoff stable id must be canonical")
        if not _CANONICAL_OFFICIAL_VERSION.fullmatch(
            self.official_vacancy_version
        ):
            raise ValueError(
                "Local handoff version must be canonical sha256:<64 hex>"
            )

    @property
    def canonical(self) -> str:
        return json.dumps(
            [self.stable_id, self.official_vacancy_version],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @property
    def storage_key(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "stable_id": self.stable_id,
            "official_vacancy_version": self.official_vacancy_version,
        }


class HandoffGradingPhase(str, Enum):
    RETRIEVING = "retrieving"
    CALLING = "provider_call_possible"
    UNCERTAIN = "uncertain"
    COMPLETED = "completed"


@dataclass(frozen=True)
class HandoffGradingIntent:
    identity: HandoffIdentity
    owner: str
    token: str
    phase: HandoffGradingPhase
    claimed_at: str
    lease_expires_at: str
    grading_input_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.owner.strip() or not self.token.strip():
            raise ValueError("Grading intent requires owner and token")
        _aware_datetime(self.claimed_at, "claimed_at")
        _aware_datetime(self.lease_expires_at, "lease_expires_at")
        if self.grading_input_fingerprint is not None and not (
            _CANONICAL_OFFICIAL_VERSION.fullmatch(
                self.grading_input_fingerprint
            )
        ):
            raise ValueError("Grading intent fingerprint must be canonical sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "owner": self.owner,
            "token": self.token,
            "phase": self.phase.value,
            "claimed_at": self.claimed_at,
            "lease_expires_at": self.lease_expires_at,
            "grading_input_fingerprint": self.grading_input_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoffGradingIntent":
        identity = value.get("identity")
        if not isinstance(identity, Mapping):
            raise ValueError("Grading intent identity must be an object")
        return cls(
            identity=HandoffIdentity(
                str(identity.get("stable_id", "")),
                str(identity.get("official_vacancy_version", "")),
            ),
            owner=str(value.get("owner", "")),
            token=str(value.get("token", "")),
            phase=HandoffGradingPhase(str(value.get("phase", ""))),
            claimed_at=str(value.get("claimed_at", "")),
            lease_expires_at=str(value.get("lease_expires_at", "")),
            grading_input_fingerprint=(
                None
                if value.get("grading_input_fingerprint") is None
                else str(value["grading_input_fingerprint"])
            ),
        )


class HandoffGradingResolution(str, Enum):
    CONFIRMED_NO_RESULT = "confirmed_no_result"


@dataclass(frozen=True)
class HandoffGradingResolutionCommand:
    identity: HandoffIdentity
    intent_token: str
    actor: str
    resolution: HandoffGradingResolution

    def __post_init__(self) -> None:
        if not self.intent_token.strip() or not self.actor.strip():
            raise ValueError("Grading resolution requires token and actor")
        if not isinstance(self.resolution, HandoffGradingResolution):
            raise TypeError("Grading resolution must be typed")


class HandoffGradingBusy(RuntimeError):
    def __init__(self, intent: HandoffGradingIntent) -> None:
        super().__init__("Local handoff grading claim is still active")
        self.intent = intent


class HandoffGradingOutcomeUncertain(RuntimeError):
    def __init__(self, intent: HandoffGradingIntent) -> None:
        super().__init__(
            "Local handoff grading outcome is uncertain; typed resolution required"
        )
        self.intent = intent


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalHandoffImportResult:
    imported: int = 0
    existing: int = 0


@dataclass(frozen=True)
class LocalHandoffResumeResult:
    stable_id: str
    official_vacancy_version: str
    grade: DeepGradeResult


class LocalHandoffService:
    """Install an Actions bundle, then resume only its local-fetch records."""

    def __init__(
        self,
        *,
        root: Path,
        workflow: OpportunityWorkflow,
        grading: DeepGradingService,
        profile: SanitizedProfessionalProfile,
        expected_authority: Mapping[str, str],
        owner: str = "local-worker",
        token_factory=None,
        clock=None,
        claim_lease: timedelta = timedelta(minutes=5),
    ) -> None:
        self._root = Path(root)
        self._workflow = workflow
        self._grading = grading
        self._profile = profile
        self._expected_authority = _validated_authority(expected_authority)
        self._owner = str(owner).strip()
        if not self._owner:
            raise ValueError("Local handoff grading owner is required")
        if claim_lease <= timedelta(0):
            raise ValueError("Local handoff grading lease must be positive")
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._clock = clock or _SystemClock()
        self._claim_lease = claim_lease
        self._state_path = (
            self._root / "data" / "private" / "local-handoff" / "state.json"
        )
        self._lock_path = self._state_path.with_name("state.lock")
        self._grade_cache = self._state_path.parent / "deep-grades"

    def import_bundle(self, package_dir: Path) -> LocalHandoffImportResult:
        bundle = StateBundle(
            self._root, expected_authority=self._expected_authority
        )
        manifest = bundle.validate_manifest(package_dir)
        packaged_pending = (
            Path(package_dir) / "files" / "data" / "pending-shortlist.json"
        )
        artifact = (
            ShortlistArtifact.read(packaged_pending)
            if packaged_pending.is_file()
            else None
        )
        candidates: list[
            tuple[
                NormalizedOpportunity,
                HandoffIdentity,
                str,
                OpportunityLead,
            ]
        ] = []
        for opportunity in () if artifact is None else artifact.opportunities:
            if not _needs_local_fetch(opportunity):
                continue
            identity = _handoff_identity(opportunity)
            fingerprint = _fingerprint(opportunity.to_dict())
            candidates.append(
                (opportunity, identity, fingerprint, _lead(opportunity))
            )

        with self._state_lock():
            state = self._read_state()
            imported = 0
            existing = 0
            active: list[str] = []
            planned: list[
                tuple[
                    NormalizedOpportunity,
                    HandoffIdentity,
                    str,
                    Mapping[str, Any] | None,
                    OpportunityLead,
                ]
            ] = []
            for opportunity, identity, fingerprint, lead in candidates:
                current = state["records"].get(identity.storage_key)
                active.append(identity.storage_key)
                if current is not None:
                    if current.get("identity") != identity.to_dict():
                        raise ValueError("Local handoff identity hash collision")
                    if str(current.get("fingerprint", "")) != fingerprint:
                        raise ValueError("Local handoff identity content mismatch")
                    existing += 1
                else:
                    imported += 1
                planned.append(
                    (opportunity, identity, fingerprint, current, lead)
                )

            bundle.install_package(package_dir)
            for opportunity, identity, fingerprint, current, lead in planned:
                if current is None:
                    state["records"][identity.storage_key] = {
                        "identity": identity.to_dict(),
                        "stable_id": opportunity.stable_id,
                        "official_vacancy_version": (
                            identity.official_vacancy_version
                        ),
                        "fingerprint": fingerprint,
                        "opportunity": opportunity.to_dict(),
                        "status": "pending",
                    }
                self._workflow.record_lead(lead)
            state["active"] = active
            state["active_manifest_digest"] = str(manifest["manifest_digest"])
            self._write_state(state)
            return LocalHandoffImportResult(
                imported=imported,
                existing=existing,
            )

    def remaining(self) -> tuple[HandoffIdentity, ...]:
        """Project unconsumed local work without mutating imported Actions state."""

        with self._state_lock():
            state = self._read_state()
            remaining = []
            for storage_key in state["active"]:
                record = state["records"].get(storage_key)
                if record is None or record.get("status") == "completed":
                    continue
                remaining.append(HandoffIdentity(**record["identity"]))
        return tuple(sorted(remaining, key=lambda item: item.canonical))

    def grading_intent(
        self, stable_id: str, official_vacancy_version: str
    ) -> HandoffGradingIntent | None:
        identity = HandoffIdentity(stable_id, official_vacancy_version)
        with self._state_lock():
            state = self._read_state()
            record = state["records"].get(identity.storage_key)
            if record is None:
                raise KeyError(identity.storage_key)
            return _record_intent(record)

    def resolve_uncertain(
        self, command: HandoffGradingResolutionCommand
    ) -> None:
        if not isinstance(command, HandoffGradingResolutionCommand):
            raise TypeError("A typed grading resolution command is required")
        with self._state_lock():
            state = self._read_state()
            record = state["records"].get(command.identity.storage_key)
            if record is None:
                raise KeyError(command.identity.storage_key)
            intent = _record_intent(record)
            if intent is None or intent.phase != HandoffGradingPhase.UNCERTAIN:
                raise ValueError("Grading intent is not uncertain")
            if intent.token != command.intent_token:
                raise ValueError("Grading resolution token does not match intent")
            if command.resolution != HandoffGradingResolution.CONFIRMED_NO_RESULT:
                raise ValueError("Unsupported grading resolution")
            if intent.grading_input_fingerprint is None:
                raise ValueError("Uncertain grading intent has no input fingerprint")
            if self._grading.cached_grade(
                command.identity.stable_id,
                intent.grading_input_fingerprint,
            ) is not None:
                raise ValueError("Exact cached grade exists and must be reconciled")
            record.setdefault("grading_resolutions", []).append(
                {
                    "identity": command.identity.to_dict(),
                    "intent_token": command.intent_token,
                    "actor": command.actor,
                    "resolution": command.resolution.value,
                    "resolved_at": self._clock.now().isoformat(),
                }
            )
            record.pop("grading_intent", None)
            self._write_state(state)

    def resume(
        self, stable_id: str, official_vacancy_version: str
    ) -> LocalHandoffResumeResult:
        identity = HandoffIdentity(stable_id, official_vacancy_version)
        with self._state_lock():
            state = self._read_state()
            record = state["records"].get(identity.storage_key)
            if record is None:
                raise KeyError(identity.storage_key)
            if record.get("identity") != identity.to_dict():
                raise ValueError("Local handoff identity hash collision")
            if record.get("status") == "completed":
                grade = self._read_cached_grade(identity, record)
                return LocalHandoffResumeResult(
                    stable_id=stable_id,
                    official_vacancy_version=official_vacancy_version,
                    grade=grade,
                )
            intent = _record_intent(record)
            if intent is not None:
                if intent.identity != identity:
                    raise ValueError("Grading intent identity mismatch")
                if intent.phase in {
                    HandoffGradingPhase.CALLING,
                    HandoffGradingPhase.UNCERTAIN,
                }:
                    return self._reconcile_or_raise(state, record, intent)
                if intent.phase == HandoffGradingPhase.RETRIEVING and (
                    self._clock.now()
                    < _aware_datetime(intent.lease_expires_at, "lease_expires_at")
                ):
                    raise HandoffGradingBusy(intent)
            now = self._clock.now()
            intent = HandoffGradingIntent(
                identity=identity,
                owner=self._owner,
                token=str(self._token_factory()),
                phase=HandoffGradingPhase.RETRIEVING,
                claimed_at=now.isoformat(),
                lease_expires_at=(now + self._claim_lease).isoformat(),
            )
            record["grading_intent"] = intent.to_dict()
            self._write_state(state)
        verification = self._workflow.verify_official(
            stable_id, runtime=Runtime.LOCAL
        )
        if (
            verification.status != VerificationStatus.VERIFIED
            or verification.snapshot is None
        ):
            raise RuntimeError("Local official vacancy retrieval did not verify")
        snapshot = verification.snapshot
        if snapshot.version != official_vacancy_version:
            raise ValueError("Local official vacancy version does not match handoff")
        grading_vacancy = _grading_vacancy(stable_id, snapshot)
        fingerprint = self._grading.grading_input_fingerprint(
            grading_vacancy,
            self._profile,
        )
        intent = replace(
            intent,
            phase=HandoffGradingPhase.CALLING,
            lease_expires_at=(self._clock.now() + self._claim_lease).isoformat(),
            grading_input_fingerprint=fingerprint,
        )
        with self._state_lock():
            state = self._read_state()
            record = self._claimed_record(state, identity, intent.token)
            record["grading_intent"] = intent.to_dict()
            self._write_state(state)
        grade = self._grading.grade(grading_vacancy, self._profile)
        self._assert_grade_matches(identity, fingerprint, grade)
        self._write_cached_grade(identity, grade)
        with self._state_lock():
            state = self._read_state()
            record = self._claimed_record(state, identity, intent.token)
            record["status"] = "completed"
            record["grading_input_fingerprint"] = grade.grading_input_fingerprint
            record["grading_intent"] = replace(
                intent,
                phase=HandoffGradingPhase.COMPLETED,
            ).to_dict()
            self._write_state(state)
        return LocalHandoffResumeResult(
            stable_id=stable_id,
            official_vacancy_version=official_vacancy_version,
            grade=grade,
        )

    @staticmethod
    def _claimed_record(
        state: dict[str, Any], identity: HandoffIdentity, token: str
    ) -> dict[str, Any]:
        record = state["records"].get(identity.storage_key)
        if record is None:
            raise KeyError(identity.storage_key)
        current = _record_intent(record)
        if current is None or current.token != token:
            raise RuntimeError("Local handoff grading claim was replaced")
        return record

    def _reconcile_or_raise(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
        intent: HandoffGradingIntent,
    ) -> LocalHandoffResumeResult:
        fingerprint = intent.grading_input_fingerprint
        cached = (
            None
            if fingerprint is None
            else self._grading.cached_grade(intent.identity.stable_id, fingerprint)
        )
        if cached is not None and fingerprint is not None:
            self._assert_grade_matches(intent.identity, fingerprint, cached)
            self._write_cached_grade(intent.identity, cached)
            record["status"] = "completed"
            record["grading_input_fingerprint"] = fingerprint
            record["grading_intent"] = replace(
                intent,
                phase=HandoffGradingPhase.COMPLETED,
            ).to_dict()
            self._write_state(state)
            return LocalHandoffResumeResult(
                stable_id=intent.identity.stable_id,
                official_vacancy_version=(
                    intent.identity.official_vacancy_version
                ),
                grade=cached,
            )
        if intent.phase == HandoffGradingPhase.CALLING and (
            self._clock.now()
            < _aware_datetime(intent.lease_expires_at, "lease_expires_at")
        ):
            raise HandoffGradingBusy(intent)
        uncertain = replace(intent, phase=HandoffGradingPhase.UNCERTAIN)
        record["grading_intent"] = uncertain.to_dict()
        self._write_state(state)
        raise HandoffGradingOutcomeUncertain(uncertain)

    @staticmethod
    def _assert_grade_matches(
        identity: HandoffIdentity,
        fingerprint: str,
        grade: DeepGradeResult,
    ) -> None:
        if (
            grade.opportunity_id != identity.stable_id
            or grade.grading_input_fingerprint != fingerprint
            or grade.requirements_evidence_matrix.official_vacancy_version
            != identity.official_vacancy_version
        ):
            raise ValueError("Local handoff grade identity mismatch")

    def _read_cached_grade(
        self, identity: HandoffIdentity, record: Mapping[str, Any]
    ) -> DeepGradeResult:
        path = self._cached_grade_path(identity)
        if not path.is_file():
            raise RuntimeError("Completed local handoff grade cache is missing")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Cached local handoff grade must be an object")
        grade = DeepGradeResult.from_dict(value)
        if value != grade.to_dict():
            raise ValueError("Cached local handoff grade is not canonical")
        if (
            grade.opportunity_id != identity.stable_id
            or grade.grading_input_fingerprint
            != record.get("grading_input_fingerprint")
            or grade.requirements_evidence_matrix.official_vacancy_version
            != identity.official_vacancy_version
        ):
            raise ValueError("Cached local handoff grade identity mismatch")
        return grade

    def _write_cached_grade(
        self, identity: HandoffIdentity, grade: DeepGradeResult
    ) -> None:
        self._grade_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._grade_cache, 0o700)
        path = self._cached_grade_path(identity)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(grade.to_dict(), indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(self._grade_cache)

    def _cached_grade_path(self, identity: HandoffIdentity) -> Path:
        digest = identity.storage_key.removeprefix("sha256:")
        return self._grade_cache / f"{digest}.json"

    @contextmanager
    def _state_lock(self):
        directory = self._state_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {
                "version": LOCAL_HANDOFF_STATE_VERSION,
                "records": {},
                "active": [],
            }
        value = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Local handoff state must be an object")
        if value.get("version") != LOCAL_HANDOFF_STATE_VERSION:
            raise ValueError("Unsupported local handoff state version")
        records = value.get("records")
        if not isinstance(records, Mapping):
            raise ValueError("Local handoff records must be an object")
        active = value.get("active", list(records))
        if not isinstance(active, list) or any(
            not isinstance(item, str) for item in active
        ):
            raise ValueError("Local handoff active projection must be an array")
        state = {
            "version": LOCAL_HANDOFF_STATE_VERSION,
            "records": {str(key): dict(item) for key, item in records.items()},
            "active": list(active),
        }
        if "active_manifest_digest" in value:
            state["active_manifest_digest"] = str(value["active_manifest_digest"])
        return state

    def _write_state(self, state: Mapping[str, Any]) -> None:
        directory = self._state_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        temporary = self._state_path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._state_path)
        os.chmod(self._state_path, 0o600)
        _fsync_directory(directory)


def _needs_local_fetch(opportunity: NormalizedOpportunity) -> bool:
    return (
        opportunity.shortlisted
        and verification_state(opportunity.job.get("verification_status"))
        == VerificationState.NEEDS_LOCAL_FETCH
    )


def _handoff_identity(opportunity: NormalizedOpportunity) -> HandoffIdentity:
    version = str(opportunity.job.get("official_vacancy_version", "")).strip()
    return HandoffIdentity(opportunity.stable_id, version)


def _lead(opportunity: NormalizedOpportunity) -> OpportunityLead:
    job = opportunity.job
    canonical_url = str(
        job.get("canonical_url")
        or job.get("official_url")
        or job.get("url")
        or ""
    ).strip()
    if not canonical_url:
        raise ValueError("Local handoff requires a vacancy URL")
    return OpportunityLead(
        stable_id=opportunity.stable_id,
        source=str(job.get("source", "remote discovery")),
        source_confidence=opportunity.source_confidence,
        canonical_url=canonical_url,
        title=str(job.get("title") or job.get("role") or ""),
        company=str(job.get("company", "")),
        location=str(job.get("location", "")),
        modality=str(job.get("modality") or job.get("remote_policy") or ""),
        snippet=str(job.get("snippet", "")),
        email_received_at=_optional_string(job.get("email_date")),
        discovered_at=opportunity.discovered_at,
        published_at=_optional_string(
            job.get("published_at") or job.get("publication_date")
        ),
    )


def _grading_vacancy(
    stable_id: str, snapshot: OfficialVacancySnapshot
) -> dict[str, Any]:
    vacancy = snapshot.vacancy
    evidence_date = snapshot.retrieved_at[:10]
    return {
        "stable_id": stable_id,
        "verification_status": VerificationState.VERIFIED.value,
        "official_url": vacancy.canonical_url,
        "canonical_url": vacancy.canonical_url,
        "official_vacancy_version": snapshot.version,
        "retrieved_at": snapshot.retrieved_at,
        "published_at": vacancy.published_at,
        "title": vacancy.role,
        "role": vacancy.role,
        "company": vacancy.company,
        "team": vacancy.team,
        "location": vacancy.location,
        "modality": vacancy.modality,
        "seniority": vacancy.seniority,
        "official_description": vacancy.description,
        "requirements": list(vacancy.requirements),
        "compensation": {"status": "unknown"},
        "sponsorship": {
            "status": "not_stated",
            "source": vacancy.canonical_url,
            "verified_at": evidence_date,
        },
        "ownership": {
            "classification": "unknown",
            "source": vacancy.canonical_url,
            "verified_at": evidence_date,
        },
    }


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _record_intent(
    record: Mapping[str, Any],
) -> HandoffGradingIntent | None:
    value = record.get("grading_intent")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Local handoff grading intent must be an object")
    return HandoffGradingIntent.from_dict(value)


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Grading intent {label} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Grading intent {label} must be timezone-aware")
    return parsed


def _validated_authority(value: Mapping[str, str]) -> dict[str, str]:
    if any(
        not isinstance(value.get(key), str) or not str(value[key]).strip()
        for key in _REQUIRED_AUTHORITY
    ):
        raise ValueError(
            "Local handoff requires exact repository, workflow, and branch authority"
        )
    return {str(key): str(item).strip() for key, item in value.items()}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "LOCAL_HANDOFF_STATE_VERSION",
    "HandoffIdentity",
    "HandoffGradingBusy",
    "HandoffGradingIntent",
    "HandoffGradingOutcomeUncertain",
    "HandoffGradingPhase",
    "HandoffGradingResolution",
    "HandoffGradingResolutionCommand",
    "LocalHandoffImportResult",
    "LocalHandoffResumeResult",
    "LocalHandoffService",
]
