"""Owner-local tailoring adapter backed by encrypted GitHub Actions artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Mapping, Protocol

from application_domain import OfficialVacancy, PreparedArtifacts
from hosted_artifact_handoff import (
    ArtifactHandoffIdentity,
    LocalArtifactHandoff,
)
from hosted_artifact_github import HostedDispatchRejected, HostedWorkflowRun


HOSTED_TAILORING_STATE_VERSION = "job-agent.hosted-tailoring-intent.v2"
_PHASES = frozenset(
    {
        "prepared",
        "dispatching",
        "ambiguous",
        "dispatched",
        "run_bound",
        "completed",
        "failed",
        "resolution_required",
    }
)
_STATE_FIELDS = frozenset(
    {
        "version",
        "intent_id",
        "identity",
        "phase",
        "transport_scope",
        "prepared_at",
        "dispatch_started_at",
        "dispatch_accepted_at",
        "run_discovery_deadline",
        "baseline_captured",
        "prior_workflow_run_ids",
        "workflow_run_id",
        "failure_reason",
        "artifacts",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "version",
        "cv_path",
        "cover_letter_path",
        "cv_hash",
        "cover_letter_hash",
        "evidence_source_version",
        "matrix_version",
        "family",
        "claims",
        "stretch_decision",
    }
)


class HostedArtifactClient(Protocol):
    @property
    def transport_scope(self) -> Mapping[str, str]: ...

    def workflow_run_ids(
        self, identity: ArtifactHandoffIdentity
    ) -> frozenset[int]: ...

    def dispatch(self, identity: ArtifactHandoffIdentity) -> None: ...

    def workflow_runs(
        self,
        identity: ArtifactHandoffIdentity,
        *,
        exclude_run_ids: frozenset[int],
    ) -> tuple[HostedWorkflowRun, ...]: ...

    def package_for_run(
        self,
        identity: ArtifactHandoffIdentity,
        *,
        workflow_run_id: int,
    ) -> bytes | None: ...


class HostedPreparationPending(RuntimeError):
    """The external request is durable and later worker cycles must reconcile."""


class HostedPreparationResolutionRequired(RuntimeError):
    """Automatic recovery stopped because dispatch/run identity is not unique."""


class HostedPreparationFailed(RuntimeError):
    """A definitive hosted run failed without enabling an automatic retry."""


@dataclass(frozen=True)
class HostedPreparationResolution:
    """Fresh evidence for one terminal hosted preparation intent."""

    intent_id: str
    phase: str
    reason: str
    retry_safe: bool


class HostedTailoringStateStore:
    """Private durable guard around one possibly ambiguous hosted dispatch."""

    def __init__(self, root: Path) -> None:
        configured_root = Path(root).expanduser()
        self._root = (
            configured_root
            if configured_root.is_absolute()
            else Path.cwd() / configured_root
        )

    def load(
        self,
        intent_id: str,
        identity: ArtifactHandoffIdentity,
    ) -> dict[str, object] | None:
        intent_id = _canonical_intent_id(intent_id)
        path = self._path(intent_id)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        _require_private_directory(self._root)
        _require_private_regular_file(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Hosted tailoring intent state is invalid") from None
        if (
            not isinstance(value, dict)
            or set(value) != _STATE_FIELDS
            or value.get("version") != HOSTED_TAILORING_STATE_VERSION
            or value.get("intent_id") != intent_id
            or value.get("identity") != identity.to_dict()
            or value.get("phase") not in _PHASES
            or not _valid_transport_scope(value.get("transport_scope"))
            or not _valid_timestamp(value.get("prepared_at"))
            or not _valid_optional_timestamp(value.get("dispatch_started_at"))
            or not _valid_optional_timestamp(value.get("dispatch_accepted_at"))
            or not _valid_optional_timestamp(value.get("run_discovery_deadline"))
            or not isinstance(value.get("baseline_captured"), bool)
            or not _valid_ids(value.get("prior_workflow_run_ids"))
            or not _valid_optional_id(value.get("workflow_run_id"))
            or not _valid_optional_reason(value.get("failure_reason"))
        ):
            raise ValueError("Hosted tailoring intent state is invalid")
        _validate_phase_shape(value)
        if value["phase"] == "completed" and not isinstance(
            value.get("artifacts"), dict
        ):
            raise ValueError("Completed hosted tailoring state lacks artifacts")
        if value["phase"] == "completed":
            _validated_artifacts(value["artifacts"])
        if value["phase"] != "completed" and value.get("artifacts") is not None:
            raise ValueError("Incomplete hosted tailoring state contains artifacts")
        return value

    def save(
        self,
        value: Mapping[str, object],
        identity: ArtifactHandoffIdentity,
    ) -> None:
        candidate = dict(value)
        intent_id = _canonical_intent_id(candidate.get("intent_id", ""))
        if candidate.get("intent_id") != intent_id:
            raise ValueError("Hosted tailoring intent id must be canonical")
        if candidate.get("identity") != identity.to_dict():
            raise ValueError("Hosted tailoring intent identity mismatch")
        if candidate.get("version") != HOSTED_TAILORING_STATE_VERSION:
            raise ValueError("Hosted tailoring intent version is unsupported")
        if candidate.get("phase") not in _PHASES:
            raise ValueError("Hosted tailoring intent phase is invalid")
        if set(candidate) != _STATE_FIELDS:
            raise ValueError("Hosted tailoring intent has unknown fields")
        if (
            not _valid_transport_scope(candidate.get("transport_scope"))
            or not _valid_timestamp(candidate.get("prepared_at"))
            or not _valid_optional_timestamp(candidate.get("dispatch_started_at"))
            or not _valid_optional_timestamp(candidate.get("dispatch_accepted_at"))
            or not _valid_optional_timestamp(
                candidate.get("run_discovery_deadline")
            )
            or not isinstance(candidate.get("baseline_captured"), bool)
            or not _valid_ids(candidate.get("prior_workflow_run_ids"))
            or not _valid_optional_id(candidate.get("workflow_run_id"))
            or not _valid_optional_reason(candidate.get("failure_reason"))
        ):
            raise ValueError("Hosted tailoring intent state is invalid")
        _validate_phase_shape(candidate)
        if candidate["phase"] == "completed" and not isinstance(
            candidate["artifacts"], dict
        ):
            raise ValueError("Completed hosted tailoring state lacks artifacts")
        if candidate["phase"] == "completed":
            _validated_artifacts(candidate["artifacts"])
        if candidate["phase"] != "completed" and candidate["artifacts"] is not None:
            raise ValueError("Incomplete hosted tailoring state contains artifacts")
        existing = self.load(intent_id, identity)
        if existing is not None:
            allowed = {
                ("prepared", "prepared"),
                ("prepared", "dispatching"),
                ("dispatching", "ambiguous"),
                ("dispatching", "dispatched"),
                ("dispatching", "failed"),
                ("dispatching", "run_bound"),
                ("dispatching", "resolution_required"),
                ("ambiguous", "run_bound"),
                ("ambiguous", "resolution_required"),
                ("dispatched", "run_bound"),
                ("dispatched", "resolution_required"),
                ("run_bound", "completed"),
                ("run_bound", "failed"),
                ("run_bound", "resolution_required"),
                ("completed", "completed"),
            }
            if (str(existing["phase"]), str(candidate["phase"])) not in allowed:
                raise ValueError("Hosted tailoring intent phase cannot regress")
            if (
                existing["baseline_captured"]
                and existing["prior_workflow_run_ids"]
                != candidate["prior_workflow_run_ids"]
            ):
                raise ValueError("Hosted tailoring workflow baseline cannot change")
            if existing["transport_scope"] != candidate["transport_scope"]:
                raise ValueError("Hosted tailoring transport scope cannot change")
            if existing["prepared_at"] != candidate["prepared_at"]:
                raise ValueError("Hosted tailoring preparation time cannot change")
            if existing["workflow_run_id"] is not None and (
                existing["workflow_run_id"] != candidate["workflow_run_id"]
            ):
                raise ValueError("Hosted tailoring workflow run cannot change")
            if existing["phase"] == "completed" and existing != candidate:
                raise ValueError("Completed hosted tailoring intent is immutable")
        _ensure_private_directory(self._root)
        path = self._path(intent_id)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self._root,
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(candidate, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            _fsync_directory(self._root)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def completed_identity_for(
        self,
        artifacts: PreparedArtifacts,
    ) -> ArtifactHandoffIdentity | None:
        """Resolve exactly one completed, owner-local identity for a bundle."""

        try:
            _require_private_directory(self._root)
            expected_artifacts = json.loads(json.dumps(asdict(artifacts)))
            matches = []
            for path in sorted(self._root.iterdir()):
                if path.suffix != ".json":
                    continue
                _require_private_regular_file(path)
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError(
                        "Hosted tailoring intent state is invalid"
                    ) from None
                if not isinstance(value, dict):
                    raise ValueError("Hosted tailoring intent state is invalid")
                identity_value = value.get("identity")
                if not isinstance(identity_value, Mapping) or set(identity_value) != {
                    "application_id",
                    "official_vacancy_version",
                }:
                    raise ValueError("Hosted tailoring intent state is invalid")
                identity = ArtifactHandoffIdentity(
                    application_id=str(identity_value["application_id"]),
                    official_vacancy_version=str(
                        identity_value["official_vacancy_version"]
                    ),
                )
                intent_id = _canonical_intent_id(value.get("intent_id", ""))
                if path != self._path(intent_id):
                    raise ValueError("Hosted tailoring intent state is misplaced")
                record = self.load(intent_id, identity)
                assert record is not None
                if (
                    record["phase"] == "completed"
                    and record["artifacts"] == expected_artifacts
                ):
                    matches.append(identity)
            return matches[0] if len(matches) == 1 else None
        except (OSError, ValueError):
            return None

    def _path(self, intent_id: str) -> Path:
        canonical = _canonical_intent_id(intent_id)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"


class HostedTailoringAdapter:
    """Keep approval and verified files local while outsourcing generation."""

    def __init__(
        self,
        *,
        client: HostedArtifactClient,
        handoff: LocalArtifactHandoff,
        transfer_root: Path,
        state_store: HostedTailoringStateStore,
        source_version_loader: Callable[[], str],
        now: Callable[[], datetime] | None = None,
        run_discovery_timeout: timedelta = timedelta(minutes=10),
    ) -> None:
        if run_discovery_timeout <= timedelta(0):
            raise ValueError("Hosted run discovery timeout must be positive")
        self._client = client
        self._handoff = handoff
        self._transfer_root = Path(transfer_root)
        self._state_store = state_store
        self._source_version_loader = source_version_loader
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._run_discovery_timeout = run_discovery_timeout

    def prepare(
        self,
        application_id: str,
        intent_id: str,
        opportunity: Mapping[str, object],
        official_vacancy: OfficialVacancy,
    ) -> PreparedArtifacts:
        del opportunity
        if (
            not official_vacancy.available
            or not official_vacancy.verified
            or not official_vacancy.description.strip()
        ):
            raise ValueError("Hosted tailoring requires a verified vacancy")
        identity = ArtifactHandoffIdentity(
            application_id=application_id,
            official_vacancy_version=official_vacancy.version,
        )
        record = self._state_store.load(intent_id, identity)
        if record is not None and record["phase"] == "completed":
            artifacts = PreparedArtifacts.from_dict(record["artifacts"])
            if not self._handoff.verify_installed(identity, artifacts):
                raise RuntimeError("Completed hosted artifacts are no longer intact")
            return artifacts
        if record is None:
            now = self._now_iso()
            record = {
                "version": HOSTED_TAILORING_STATE_VERSION,
                "intent_id": intent_id,
                "identity": identity.to_dict(),
                "phase": "prepared",
                "transport_scope": dict(self._client.transport_scope),
                "prepared_at": now,
                "dispatch_started_at": None,
                "dispatch_accepted_at": None,
                "run_discovery_deadline": None,
                "baseline_captured": False,
                "prior_workflow_run_ids": [],
                "workflow_run_id": None,
                "failure_reason": None,
                "artifacts": None,
            }
            self._state_store.save(record, identity)
        if record["transport_scope"] != dict(self._client.transport_scope):
            raise HostedPreparationResolutionRequired(
                "Hosted transport scope changed after preparation"
            )
        if record["phase"] == "prepared":
            record = self._dispatch_prepared(record, identity)
            raise HostedPreparationPending(
                "Hosted preparation was dispatched for later reconciliation"
            )
        if record["phase"] in {"dispatching", "ambiguous", "dispatched"}:
            record = self._bind_workflow_run(record, identity)
        if record["phase"] == "run_bound":
            return self._reconcile_bound_run(record, identity)
        if record["phase"] == "failed":
            raise HostedPreparationFailed(str(record["failure_reason"]))
        if record["phase"] == "resolution_required":
            raise HostedPreparationResolutionRequired(
                str(record["failure_reason"])
            )
        raise HostedPreparationPending(
            "Hosted preparation remains pending reconciliation"
        )

    def _dispatch_prepared(
        self,
        record: Mapping[str, object],
        identity: ArtifactHandoffIdentity,
    ) -> dict[str, object]:
        candidate = dict(record)
        if not candidate["baseline_captured"]:
            try:
                baseline = sorted(self._client.workflow_run_ids(identity))
            except Exception as error:
                raise HostedPreparationPending(
                    "Hosted workflow baseline is temporarily unavailable"
                ) from error
            candidate = {
                **candidate,
                "baseline_captured": True,
                "prior_workflow_run_ids": baseline,
            }
            self._state_store.save(candidate, identity)
        started = self._now()
        candidate = {
            **candidate,
            "phase": "dispatching",
            "dispatch_started_at": started.isoformat(),
            "run_discovery_deadline": (
                started + self._run_discovery_timeout
            ).isoformat(),
        }
        self._state_store.save(candidate, identity)
        try:
            self._client.dispatch(identity)
        except HostedDispatchRejected as error:
            failed = {
                **candidate,
                "phase": "failed",
                "failure_reason": "hosted dispatch was definitively rejected",
            }
            self._state_store.save(failed, identity)
            raise HostedPreparationFailed(str(failed["failure_reason"])) from error
        except Exception as error:
            ambiguous = {
                **candidate,
                "phase": "ambiguous",
                "failure_reason": "dispatch outcome is ambiguous",
            }
            self._state_store.save(ambiguous, identity)
            raise HostedPreparationPending(
                "Hosted dispatch outcome is ambiguous; reconciliation required"
            ) from error
        dispatched = {
            **candidate,
            "phase": "dispatched",
            "dispatch_accepted_at": self._now_iso(),
        }
        self._state_store.save(dispatched, identity)
        return dispatched

    def _bind_workflow_run(
        self,
        record: Mapping[str, object],
        identity: ArtifactHandoffIdentity,
    ) -> dict[str, object]:
        try:
            candidates = self._client.workflow_runs(
                identity,
                exclude_run_ids=frozenset(record["prior_workflow_run_ids"]),
            )
        except Exception as error:
            raise HostedPreparationPending(
                "Hosted workflow run discovery is temporarily unavailable"
            ) from error
        if not candidates:
            deadline = datetime.fromisoformat(str(record["run_discovery_deadline"]))
            if self._now() < deadline:
                raise HostedPreparationPending(
                    "Hosted workflow run has not appeared yet"
                )
            resolved = {
                **record,
                "phase": "resolution_required",
                "failure_reason": "no workflow run appeared before the deadline",
            }
            self._state_store.save(resolved, identity)
            raise HostedPreparationResolutionRequired(
                str(resolved["failure_reason"])
            )
        if len(candidates) != 1:
            resolved = {
                **record,
                "phase": "resolution_required",
                "failure_reason": "multiple workflow runs match the dispatch window",
            }
            self._state_store.save(resolved, identity)
            raise HostedPreparationResolutionRequired(
                str(resolved["failure_reason"])
            )
        bound = {
            **record,
            "phase": "run_bound",
            "workflow_run_id": candidates[0].run_id,
            "failure_reason": None,
        }
        self._state_store.save(bound, identity)
        return bound

    def _reconcile_bound_run(
        self,
        record: Mapping[str, object],
        identity: ArtifactHandoffIdentity,
    ) -> PreparedArtifacts:
        run_id = int(record["workflow_run_id"])
        try:
            runs = self._client.workflow_runs(
                identity,
                exclude_run_ids=frozenset(record["prior_workflow_run_ids"]),
            )
        except Exception as error:
            raise HostedPreparationPending(
                "Bound workflow run is temporarily unavailable"
            ) from error
        bound = next((run for run in runs if run.run_id == run_id), None)
        if bound is None:
            raise HostedPreparationPending("Bound workflow run is not visible yet")
        if bound.status != "completed":
            raise HostedPreparationPending("Bound workflow run is still active")
        if bound.conclusion != "success":
            failed = {
                **record,
                "phase": "failed",
                "failure_reason": (
                    f"bound workflow run concluded as {bound.conclusion or 'unknown'}"
                ),
            }
            self._state_store.save(failed, identity)
            raise HostedPreparationFailed(str(failed["failure_reason"]))
        try:
            encrypted = self._client.package_for_run(
                identity,
                workflow_run_id=run_id,
            )
        except Exception as error:
            raise HostedPreparationPending(
                "Bound workflow artifact is temporarily unavailable"
            ) from error
        if encrypted is None:
            resolved = {
                **record,
                "phase": "resolution_required",
                "failure_reason": "successful workflow run produced no bound artifact",
            }
            self._state_store.save(resolved, identity)
            raise HostedPreparationResolutionRequired(
                str(resolved["failure_reason"])
            )
        artifacts = self._install(encrypted, identity)
        if not self._handoff.verify_installed(identity, artifacts):
            raise RuntimeError("Hosted artifacts failed local verification")
        self._state_store.save(
            {
                **record,
                "phase": "completed",
                "artifacts": asdict(artifacts),
            },
            identity,
        )
        return artifacts

    def _now_iso(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Hosted tailoring clock must be timezone-aware")
        return value.isoformat()

    def _install(
        self,
        encrypted: bytes,
        identity: ArtifactHandoffIdentity,
    ) -> PreparedArtifacts:
        self._transfer_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._transfer_root, 0o700)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".hosted-artifacts-",
            suffix=".enc",
            dir=self._transfer_root,
        )
        package = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(package, 0o600)
            return self._handoff.install(
                package,
                expected_identity=identity,
            )
        finally:
            package.unlink(missing_ok=True)

    def verify_artifacts(self, artifacts: PreparedArtifacts) -> bool:
        identity = self._state_store.completed_identity_for(artifacts)
        return identity is not None and self._handoff.verify_installed(
            identity,
            artifacts,
        )

    def preparation_resolution(
        self,
        application_id: str,
        intent_id: str,
        official_vacancy: OfficialVacancy,
    ) -> HostedPreparationResolution | None:
        """Recheck GitHub before a human is offered or allowed a fresh intent."""

        identity = ArtifactHandoffIdentity(
            application_id=application_id,
            official_vacancy_version=official_vacancy.version,
        )
        record = self._state_store.load(intent_id, identity)
        if record is None or record["phase"] not in {
            "failed",
            "resolution_required",
        }:
            return None
        reason = str(record["failure_reason"])
        retry_safe = False
        try:
            candidates = self._client.workflow_runs(
                identity,
                exclude_run_ids=frozenset(record["prior_workflow_run_ids"]),
            )
            bound_id = record["workflow_run_id"]
            if bound_id is None:
                deadline = datetime.fromisoformat(
                    str(record["run_discovery_deadline"])
                )
                retry_safe = (
                    not candidates
                    and (
                        record["phase"] == "failed"
                        or self._now() >= deadline
                    )
                )
            elif (
                len(candidates) == 1
                and candidates[0].run_id == bound_id
                and candidates[0].status == "completed"
                and candidates[0].conclusion not in {None, "success"}
            ):
                retry_safe = (
                    self._client.package_for_run(
                        identity,
                        workflow_run_id=int(bound_id),
                    )
                    is None
                )
        except Exception:
            retry_safe = False
        return HostedPreparationResolution(
            intent_id=intent_id,
            phase=str(record["phase"]),
            reason=reason,
            retry_safe=retry_safe,
        )

    def reload_master_cv(self) -> str:
        version = str(self._source_version_loader()).strip()
        if not version:
            raise RuntimeError("Hosted source version is unavailable")
        return version


def _valid_ids(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    ) and value == sorted(set(value))


def _valid_optional_id(value: object) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
    )


def _valid_transport_scope(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"workflow", "branch", "event"}
        and all(
            isinstance(item, str) and item and item == item.strip()
            for item in value.values()
        )
        and value["event"] == "repository_dispatch"
    )


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None


def _valid_optional_timestamp(value: object) -> bool:
    return value is None or _valid_timestamp(value)


def _valid_optional_reason(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and bool(value) and value == value.strip()
    )


def _validate_phase_shape(value: Mapping[str, object]) -> None:
    phase = value["phase"]
    if phase == "prepared":
        if (
            value["dispatch_started_at"] is not None
            or value["dispatch_accepted_at"] is not None
            or value["run_discovery_deadline"] is not None
            or value["workflow_run_id"] is not None
            or value["failure_reason"] is not None
        ):
            raise ValueError("Prepared hosted tailoring state is invalid")
        return
    if (
        value["dispatch_started_at"] is None
        or value["run_discovery_deadline"] is None
        or not value["baseline_captured"]
    ):
        raise ValueError("Dispatched hosted tailoring state lacks its baseline")
    if phase == "dispatched" and value["dispatch_accepted_at"] is None:
        raise ValueError("Accepted hosted dispatch lacks its timestamp")
    if phase in {"run_bound", "completed"} and (
        value["workflow_run_id"] is None
    ):
        raise ValueError("Hosted tailoring state lacks its workflow run")
    if phase in {"failed", "resolution_required"} and (
        value["failure_reason"] is None
    ):
        raise ValueError("Terminal hosted tailoring state lacks its reason")


def _canonical_intent_id(value: object) -> str:
    candidate = str(value)
    if not candidate or candidate != candidate.strip():
        raise ValueError("Hosted tailoring intent id must be canonical")
    return candidate


def _validated_artifacts(value: object) -> PreparedArtifacts:
    if not isinstance(value, Mapping) or set(value) != _ARTIFACT_FIELDS:
        raise ValueError("Hosted tailoring artifact state is invalid")
    try:
        return PreparedArtifacts.from_dict(value)
    except (KeyError, TypeError, ValueError):
        raise ValueError("Hosted tailoring artifact state is invalid") from None


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
        metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Hosted tailoring state directory is invalid")
    os.chmod(path, 0o700)
    _require_private_directory(path)
    _fsync_directory(path)
    if path.parent != path:
        _fsync_directory(path.parent)


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ValueError("Hosted tailoring state directory is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("Hosted tailoring state directory is not owner-only")


def _require_private_regular_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("Hosted tailoring intent state is not owner-only")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "HOSTED_TAILORING_STATE_VERSION",
    "HostedArtifactClient",
    "HostedPreparationFailed",
    "HostedPreparationPending",
    "HostedPreparationResolution",
    "HostedPreparationResolutionRequired",
    "HostedTailoringAdapter",
    "HostedTailoringStateStore",
]
