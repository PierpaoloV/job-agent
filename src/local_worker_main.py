"""Production entrypoint and explicit composition seam for the local worker."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any, Callable, Protocol

from application_composition import (
    HostedPreparationVacancyAdapter,
    HostedApplicationConfig,
    UnsupportedAtsAdapter,
    build_application_workflow_coordinator,
    build_hosted_tailoring_adapter,
)
from actions_state import restore_latest
from application_domain import ActionCommand, CommandStatus, WorkflowAction
from application_telegram import TelegramCommandHandler
from telegram_applications import (
    TelegramApplicationApi,
    TelegramPreparationCompletionNotifier,
    TelegramPreparationResolutionNotifier,
)
from telegram_delivery import TelegramDeliveryLedger, TelegramScheduledNotifier
from local_worker import (
    CapabilityExecution,
    CapabilityReconciliationVerifier,
    LocalWorker,
    WorkerCapability,
    WorkerLogger,
)
from local_discovery_delivery import DiscoveryNotificationCapability
from local_worker_store import (
    LocalWorkerStore,
    ReconciliationDecision,
    WorkerCommand,
)
from local_worker_telegram import (
    CallbackContext,
    CallbackRoute,
    LocalWorkerTelegramRouter,
    TelegramUpdateStore,
    TelegramWorkerApi,
    TelegramWorkerHttpApi,
)
from macos_keychain import MacOSKeychainCredentialStore
from discovery_schedule import DiscoverySchedule, FileDiscoveryScheduleStore
from hosted_artifact_preparation import HostedPreparationInputStore
from opportunity_decisions import (
    FileOpportunityDecisionStore,
    OpportunityButtonFactory,
    OpportunityDecisionService,
    ScheduleJobLookup,
    SuppressingDiscoveryNotifier,
    build_opportunity_callback_route,
)
from redacted_logging import RedactedStructuredLogger
from workflow import SystemClock


PRODUCTION_CONFIG_VERSION = "job-agent.local-worker-config.v1"
PREPARATION_CURSOR_STATE_VERSION = (
    "job-agent.application-preparation-notification-cursors.v1"
)
_IDEMPOTENT_BACKGROUND_RETRY_EVIDENCE = {
    "application_preparations": (
        "Hosted preparation reconciliation uses exact durable intent state, "
        "read-only GitHub inspection, and an idempotent Telegram delivery ledger"
    ),
    "discovery_notifications": (
        "Discovery synchronization is read-only and Telegram delivery is guarded "
        "by durable idempotency claims"
    ),
}


class TelegramRouter(Protocol):
    def consume_once(self, *, timeout: int = 25) -> int: ...


class WorkerRuntime(Protocol):
    def status(self) -> Mapping[str, Any]: ...

    def run_once(self) -> Mapping[str, Any]: ...

    def serve(self) -> Mapping[str, Any]: ...


class RuntimeFactory(Protocol):
    def __call__(self, state_path: Path) -> WorkerRuntime: ...


class SecretStore(Protocol):
    def get(self, service: str, account: str) -> str | None: ...


class TelegramApiFactory(Protocol):
    def __call__(self, *, token: str, chat_id: str) -> TelegramWorkerApi: ...


class ApplicationTelegramApiFactory(Protocol):
    def __call__(
        self,
        *,
        token: str,
        chat_id: str,
        user_id: str,
        callback_encoder: Callable[[ActionCommand], str],
    ): ...


@dataclass(frozen=True)
class PreparationNotificationCursors:
    completion: int = 0
    resolution: int = 0

    def __post_init__(self) -> None:
        for field in ("completion", "resolution"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValueError("Preparation notification cursor is invalid")


class PreparationNotificationCursorStore(Protocol):
    def load(self) -> PreparationNotificationCursors: ...

    def save(self, cursors: PreparationNotificationCursors) -> None: ...


class InMemoryPreparationNotificationCursorStore:
    """Process-local cursor state for injected and unit-test reconcilers."""

    def __init__(
        self, cursors: PreparationNotificationCursors | None = None
    ) -> None:
        self._cursors = cursors or PreparationNotificationCursors()

    def load(self) -> PreparationNotificationCursors:
        return self._cursors

    def save(self, cursors: PreparationNotificationCursors) -> None:
        self._cursors = PreparationNotificationCursors(
            completion=cursors.completion,
            resolution=cursors.resolution,
        )


class OwnerOnlyPreparationNotificationCursorStore:
    """Atomic, location-bound cursor state for the owner-local worker."""

    _FIELDS = {
        "version",
        "location",
        "completion_cursor",
        "resolution_cursor",
    }

    def __init__(self, path: Path) -> None:
        candidate = Path(path).expanduser()
        self._path = Path(os.path.abspath(os.fspath(candidate)))

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> PreparationNotificationCursors:
        self._ensure_private_directory()
        try:
            self._path.lstat()
        except FileNotFoundError:
            initial = PreparationNotificationCursors()
            self._write(initial, allow_missing=True)
            return initial
        return self._read()

    def save(self, cursors: PreparationNotificationCursors) -> None:
        validated = PreparationNotificationCursors(
            completion=cursors.completion,
            resolution=cursors.resolution,
        )
        self._ensure_private_directory()
        self._write(validated, allow_missing=False)

    def _read(self) -> PreparationNotificationCursors:
        before = self._require_private_regular_file()
        descriptor = os.open(
            self._path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_dev != before.st_dev
                or metadata.st_ino != before.st_ino
            ):
                raise ValueError(
                    "Preparation notification cursor state is not owner-only"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                descriptor = -1
                payload = json.load(source)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(
                "Preparation notification cursor state is unavailable"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(payload, Mapping) or set(payload) != self._FIELDS:
            raise ValueError("Preparation notification cursor schema is invalid")
        if payload["version"] != PREPARATION_CURSOR_STATE_VERSION:
            raise ValueError("Preparation notification cursor version is invalid")
        if payload["location"] != str(self._path):
            raise ValueError("Preparation notification cursor state was relocated")
        return PreparationNotificationCursors(
            completion=payload["completion_cursor"],
            resolution=payload["resolution_cursor"],
        )

    def _write(
        self,
        cursors: PreparationNotificationCursors,
        *,
        allow_missing: bool,
    ) -> None:
        if not allow_missing:
            self._require_private_regular_file()
        payload = {
            "version": PREPARATION_CURSOR_STATE_VERSION,
            "location": str(self._path),
            "completion_cursor": cursors.completion,
            "resolution_cursor": cursors.resolution,
        }
        temporary = self._path.with_name(
            f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                output.write(json.dumps(payload, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            self._require_private_regular_file()
            self._fsync_directory(self._path.parent)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _ensure_private_directory(self) -> None:
        directory = self._path.parent
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(parents=True, mode=0o700)
            metadata = directory.lstat()
            if directory.parent != directory:
                self._fsync_directory(directory.parent)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError(
                "Preparation notification cursor directory is not owner-only"
            )

    def _require_private_regular_file(self):
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            raise ValueError(
                "Preparation notification cursor state is unavailable"
            ) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError(
                "Preparation notification cursor state is not owner-only"
            )
        return metadata

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class ProductionWorkerConfig:
    actor_id: str
    chat_id: str
    token_keychain_service: str
    token_keychain_account: str
    hosted_artifacts: HostedApplicationConfig | None = None

    @classmethod
    def load(cls, path: Path) -> ProductionWorkerConfig:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Local worker configuration must be an object")
        if set(payload) - {"version", "telegram", "hosted_artifacts"}:
            raise ValueError("Local worker configuration schema is invalid")
        if payload.get("version") != PRODUCTION_CONFIG_VERSION:
            raise ValueError("Unsupported local worker configuration version")
        telegram = payload.get("telegram")
        if not isinstance(telegram, Mapping):
            raise ValueError("Telegram worker configuration is required")
        if set(telegram) != {
            "actor_id",
            "chat_id",
            "token_keychain_service",
            "token_keychain_account",
        }:
            raise ValueError("Telegram worker configuration schema is invalid")
        fields = {
            name: str(telegram.get(name, "")).strip()
            for name in (
                "actor_id",
                "chat_id",
                "token_keychain_service",
                "token_keychain_account",
            )
        }
        if any(not value for value in fields.values()):
            raise ValueError("Telegram worker configuration is incomplete")
        hosted = payload.get("hosted_artifacts")
        if hosted is not None and not isinstance(hosted, Mapping):
            raise ValueError("Hosted application configuration must be an object")
        return cls(
            **fields,
            hosted_artifacts=(
                None
                if hosted is None
                else HostedApplicationConfig.from_mapping(hosted)
            ),
        )


class TelegramRouterCapability:
    """Adapt the durable Telegram router to the worker execution contract."""

    def __init__(self, *, router: TelegramRouter, poll_timeout: int = 25) -> None:
        if poll_timeout < 0:
            raise ValueError("Telegram poll timeout cannot be negative")
        self._router = router
        self._poll_timeout = poll_timeout

    def recompute(self, resume_generation: int) -> None:
        return None

    def run_once(self, execution: CapabilityExecution) -> None:
        execution.checkpoint(external_action=False)
        self._router.consume_once(timeout=self._poll_timeout)

    def status(self) -> Mapping[str, Any]:
        return {"state": "ready", "healthy": True}


class ApplicationPreparationReconciler:
    """Advance hosted preparations once per worker cycle without long polling."""

    def __init__(
        self,
        coordinator,
        *,
        notifier=None,
        resolution_notifier=None,
        limit: int = 5,
        cursor_store: PreparationNotificationCursorStore | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("Preparation reconciliation limit must be positive")
        self._coordinator = coordinator
        self._notifier = notifier
        self._resolution_notifier = resolution_notifier
        self._limit = limit
        self._cursor_store = (
            cursor_store or InMemoryPreparationNotificationCursorStore()
        )
        self._cursor_state_unavailable = False
        try:
            cursors = self._cursor_store.load()
        except Exception:
            self._cursor_state_unavailable = True
            self._completion_cursor = None
            self._resolution_cursor = None
        else:
            self._completion_cursor = cursors.completion
            self._resolution_cursor = cursors.resolution

    def recompute(self, resume_generation: int) -> None:
        return None

    def run_once(self, execution: CapabilityExecution) -> None:
        if self._cursor_state_unavailable:
            raise RuntimeError(
                "Preparation notification cursor state is unavailable"
            )
        assert self._completion_cursor is not None
        assert self._resolution_cursor is not None
        processed = []
        pending = self._coordinator.pending_preparation_ids()[: self._limit]
        for application_id in pending:
            execution.checkpoint()
            if application_id in self._coordinator.pending_preparation_ids():
                result = self._coordinator.resume_pending(application_id)
                processed.append(application_id)
                if (
                    self._notifier is not None
                    and result.status == CommandStatus.COMPLETED
                    and result.next_action == WorkflowAction.FILL
                ):
                    self._notifier.notify(
                        application_id,
                        before_send=execution.checkpoint,
                    )
                elif (
                    self._resolution_notifier is not None
                    and result.status
                    in {
                        CommandStatus.FAILED,
                        CommandStatus.RECONCILIATION_REQUIRED,
                    }
                ):
                    self._resolution_notifier.notify(
                        application_id,
                        before_send=execution.checkpoint,
                    )
        completion_ids = getattr(
            self._coordinator, "preparation_completion_ids", None
        )
        processed_set = set(processed)
        if self._notifier is not None and callable(completion_ids):
            completion_cursor = self._deliver_terminal_notices(
                completion_ids(),
                notifier=self._notifier,
                processed=processed_set,
                cursor=self._completion_cursor,
                execution=execution,
            )
            self._persist_cursors(completion=completion_cursor)
        resolution_ids = getattr(
            self._coordinator, "preparation_resolution_ids", None
        )
        if self._resolution_notifier is None or not callable(resolution_ids):
            return
        resolution_cursor = self._deliver_terminal_notices(
            resolution_ids(),
            notifier=self._resolution_notifier,
            processed=processed_set,
            cursor=self._resolution_cursor,
            execution=execution,
        )
        self._persist_cursors(resolution=resolution_cursor)

    def _persist_cursors(
        self,
        *,
        completion: int | None = None,
        resolution: int | None = None,
    ) -> None:
        assert self._completion_cursor is not None
        assert self._resolution_cursor is not None
        cursors = PreparationNotificationCursors(
            completion=(
                self._completion_cursor if completion is None else completion
            ),
            resolution=(
                self._resolution_cursor if resolution is None else resolution
            ),
        )
        try:
            self._cursor_store.save(cursors)
        except Exception as error:
            self._cursor_state_unavailable = True
            raise RuntimeError(
                "Preparation notification cursor state is unavailable"
            ) from error
        self._completion_cursor = cursors.completion
        self._resolution_cursor = cursors.resolution

    def _deliver_terminal_notices(
        self,
        application_ids: Sequence[str],
        *,
        notifier,
        processed: set[str],
        cursor: int,
        execution: CapabilityExecution,
    ) -> int:
        candidates = tuple(
            application_id
            for application_id in application_ids
            if application_id not in processed
        )
        if not candidates:
            return cursor
        start = cursor % len(candidates)
        scan_limit = min(len(candidates), self._limit * 2)
        attempts = 0
        examined = 0
        needs_delivery = getattr(notifier, "needs_delivery", None)
        while examined < scan_limit and attempts < self._limit:
            application_id = candidates[(start + examined) % len(candidates)]
            examined += 1
            if callable(needs_delivery) and not needs_delivery(application_id):
                continue
            attempts += 1
            notifier.notify(
                application_id,
                before_send=execution.checkpoint,
            )
        return (start + examined) % len(candidates)

    def status(self) -> Mapping[str, Any]:
        if self._cursor_state_unavailable:
            return {"state": "unavailable", "healthy": False}
        try:
            pending = len(self._coordinator.pending_preparation_ids())
        except Exception:
            return {"state": "unavailable", "healthy": False}
        return {
            "state": "idle" if pending == 0 else "ready",
            "healthy": True,
            "pending": pending,
        }


def build_local_worker(
    *,
    state_path: Path,
    capabilities: Mapping[str, WorkerCapability] | None = None,
    telegram_router: TelegramRouter | None = None,
    telegram_poll_timeout: int = 25,
    logger: WorkerLogger | None = None,
    reconciliation_verifiers: Mapping[str, CapabilityReconciliationVerifier]
    | None = None,
    safe_retry_capabilities: Mapping[str, str] | None = None,
) -> LocalWorker:
    """Compose injected production adapters without creating external clients."""

    store = LocalWorkerStore(Path(state_path))
    wired = {}
    if telegram_router is not None:
        if capabilities is not None and "telegram" in capabilities:
            raise ValueError("The telegram capability is already configured")
        wired["telegram"] = TelegramRouterCapability(
            router=telegram_router,
            poll_timeout=telegram_poll_timeout,
        )
    wired.update(capabilities or {})
    authorized_retries = {
        name: evidence
        for name, evidence in (safe_retry_capabilities or {}).items()
        if name in wired
    }
    if telegram_router is not None:
        authorized_retries["telegram"] = (
            "The outer Telegram capability only polls updates; "
            "callback effects use separate durable capability claims"
        )
    return LocalWorker(
        store=store,
        capabilities=wired,
        logger=logger,
        reconciliation_verifiers=reconciliation_verifiers,
        safe_retry_capabilities=authorized_retries,
    )


class _DisabledRuntime:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def status(self) -> Mapping[str, Any]:
        return {
            "state": "stop",
            "health": "disabled",
            "reason": self._reason,
            "capabilities": {},
        }

    def run_once(self) -> Mapping[str, Any]:
        return self.status()

    def serve(self) -> Mapping[str, Any]:
        return self.status()


class _DeferredWorkerControl:
    """Break the production composition cycle before the worker is exposed."""

    def __init__(self) -> None:
        self._worker: LocalWorker | None = None

    def bind(self, worker: LocalWorker) -> None:
        if self._worker is not None:
            raise RuntimeError("Worker control is already bound")
        self._worker = worker

    def _bound(self) -> LocalWorker:
        if self._worker is None:
            raise RuntimeError("Worker control is not bound")
        return self._worker

    def control(self, command: WorkerCommand) -> Mapping[str, object]:
        return self._bound().control(command)

    def status(self) -> Mapping[str, object]:
        return self._bound().status()

    def execute_gated_action(
        self,
        capability: str,
        action: Callable[[CapabilityExecution], Any],
    ) -> Any:
        return self._bound().execute_gated_action(capability, action)

    def reconcile_capability(
        self, capability: str, *, actor: str, provenance: str
    ) -> ReconciliationDecision:
        return self._bound().reconcile_capability(
            capability, actor=actor, provenance=provenance
        )


def build_application_callback_route(
    coordinator, *, resolution_notifier=None
) -> CallbackRoute:
    """Wire application callbacks through the worker's scoped effect gate."""

    handler = TelegramCommandHandler(coordinator)

    def persisted_command(payload: str) -> ActionCommand | None:
        if not payload.startswith("app:"):
            return None
        return coordinator.command_for_token(payload.removeprefix("app:"))

    def handle(execution: CapabilityExecution, context: CallbackContext) -> str:
        execution.checkpoint()
        command = persisted_command(context.payload)
        if command is None:
            return CommandStatus.MISMATCHED.value
        result = handler.handle_callback(command)
        if (
            result.status == CommandStatus.EXPIRED
            and command.scope.action == WorkflowAction.RETRY_PREPARATION
            and resolution_notifier is not None
        ):
            resolution_notifier.reissue_expired_retry(
                command,
                before_send=execution.checkpoint,
            )
        return result.status.value

    def handle_stale(
        execution: CapabilityExecution, context: CallbackContext
    ) -> str:
        command = persisted_command(context.payload)
        if (
            command is None
            or command.scope.action != WorkflowAction.RETRY_PREPARATION
            or resolution_notifier is None
        ):
            return CommandStatus.MISMATCHED.value
        resolution_notifier.reissue_expired_retry(
            command,
            before_send=execution.checkpoint,
        )
        return CommandStatus.EXPIRED.value

    return CallbackRoute(
        route="applications",
        prefixes=("app:",),
        capability="applications",
        handler=handle,
        stale_handler=(
            handle_stale if resolution_notifier is not None else None
        ),
        recover_stale_replay=resolution_notifier is not None,
    )


class WorkerApplicationCallbackEncoder:
    """Issue compact application buttons scoped to the current worker resume."""

    def __init__(
        self,
        *,
        store: TelegramUpdateStore,
        worker,
        actor_id: str,
        chat_id: str,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Application callback TTL must be positive")
        self._store = store
        self._worker = worker
        self._actor_id = str(actor_id)
        self._chat_id = str(chat_id)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl

    def __call__(self, command: ActionCommand) -> str:
        status = self._worker.status()
        if str(status.get("state")) != WorkerCommand.RESUME.value:
            raise RuntimeError("Worker must be resumed before issuing a callback")
        try:
            resume_generation = int(status["resume_generation"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Worker resume generation is unavailable") from None
        authorization = self._store.issue_callback_authorization(
            actor_id=self._actor_id,
            chat_id=self._chat_id,
            route="applications",
            capability="applications",
            payload=TelegramCommandHandler.encode_callback(command),
            resume_generation=resume_generation,
            expires_at=self._now() + self._ttl,
        )
        return authorization.callback_data


@dataclass(frozen=True)
class ProductionApplicationWorkerRuntime:
    """One composition owns both button issuance and callback consumption."""

    worker: LocalWorker
    applications: TelegramCommandHandler
    callback_encoder: WorkerApplicationCallbackEncoder

    def status(self) -> Mapping[str, Any]:
        return self.worker.status()

    def run_once(self) -> Mapping[str, Any]:
        return self.worker.run_once()

    def serve(self) -> Mapping[str, Any]:
        return self.worker.serve()


def build_production_runtime(
    *,
    state_path: Path,
    config_path: Path,
    secret_store: SecretStore | None = None,
    api_factory: TelegramApiFactory | None = None,
    application_coordinator=None,
    repository_root: Path | None = None,
    application_ats=None,
    official_vacancies=None,
    application_clock=None,
    hosted_source_version_loader: Callable[[], str] | None = None,
    application_token_factory=None,
    application_api_factory: ApplicationTelegramApiFactory | None = None,
    telegram_poll_timeout: int = 25,
    logger: WorkerLogger | None = None,
    callback_routes: Sequence[CallbackRoute] = (),
    reconciliation_verifiers: Mapping[str, CapabilityReconciliationVerifier]
    | None = None,
) -> WorkerRuntime:
    """Compose the launchd runtime from non-secret config and Keychain secrets."""

    config_path = Path(config_path)
    if not config_path.is_file():
        return _DisabledRuntime("configuration_missing")
    try:
        config = ProductionWorkerConfig.load(config_path)
        secrets = secret_store or MacOSKeychainCredentialStore()
        token = secrets.get(
            config.token_keychain_service, config.token_keychain_account
        )
    except (OSError, RuntimeError, ValueError):
        return _DisabledRuntime("configuration_unavailable")
    if not token:
        return _DisabledRuntime("telegram_secret_missing")
    root = repository_root or Path(__file__).resolve().parents[1]
    github_token = None
    encoded_handoff_key = None
    if application_coordinator is None:
        if config.hosted_artifacts is None:
            return _DisabledRuntime("hosted_artifact_configuration_missing")
        try:
            github_token = secrets.get(
                config.hosted_artifacts.github_token_keychain_service,
                config.hosted_artifacts.github_token_keychain_account,
            )
            encoded_handoff_key = secrets.get(
                config.hosted_artifacts.handoff_key_keychain_service,
                config.hosted_artifacts.handoff_key_keychain_account,
            )
        except (OSError, RuntimeError, ValueError):
            return _DisabledRuntime("hosted_artifact_secrets_unavailable")
        if not github_token:
            return _DisabledRuntime("github_secret_missing")
        if not encoded_handoff_key:
            return _DisabledRuntime("artifact_handoff_secret_missing")
        inputs = HostedPreparationInputStore(
            root / "data" / "hosted-preparation-inputs"
        )
        application_ats = application_ats or UnsupportedAtsAdapter()
        official_vacancies = (
            official_vacancies
            or HostedPreparationVacancyAdapter(inputs)
        )
        application_clock = application_clock or SystemClock()
        hosted_source_version_loader = (
            hosted_source_version_loader
            or (lambda: "hosted-authoritative-evidence")
        )
        try:
            tailoring = build_hosted_tailoring_adapter(
                repository_root=root,
                config=config.hosted_artifacts,
                github_token=github_token,
                handoff_key=encoded_handoff_key,
                source_version_loader=hosted_source_version_loader,
            )
            application_coordinator = build_application_workflow_coordinator(
                repository_root=root,
                tailoring=tailoring,
                ats=application_ats,
                official_vacancies=official_vacancies,
                clock=application_clock,
                token_factory=application_token_factory,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _DisabledRuntime("hosted_artifact_composition_unavailable")

    factory = api_factory or TelegramWorkerHttpApi
    api = factory(token=token, chat_id=config.chat_id)
    control = _DeferredWorkerControl()
    updates = TelegramUpdateStore(config_path.parent / "telegram-updates.sqlite")
    encoder = WorkerApplicationCallbackEncoder(
        store=updates,
        worker=control,
        actor_id=config.actor_id,
        chat_id=config.chat_id,
    )
    application_factory = application_api_factory or TelegramApplicationApi
    application_api = application_factory(
        token=token,
        chat_id=config.chat_id,
        user_id=config.actor_id,
        callback_encoder=encoder,
    )
    background_capabilities = {}
    resolution_notifier = None
    opportunity_route = None
    if (
        github_token is not None
        and config.hosted_artifacts is not None
        and callable(getattr(api, "send_message", None))
    ):
        schedule_store = FileDiscoveryScheduleStore(
            root / "data" / "discovery-schedule.json"
        )
        opportunity_buttons = OpportunityButtonFactory(
            store=updates,
            worker=control,
            actor_id=config.actor_id,
            chat_id=config.chat_id,
        )
        decisions = FileOpportunityDecisionStore(
            config_path.parent / "opportunity-decisions.json"
        )
        discovery_notifier = SuppressingDiscoveryNotifier(
            TelegramScheduledNotifier(
                ledger=TelegramDeliveryLedger(
                    config_path.parent
                    / "discovery-telegram-deliveries.sqlite"
                ),
                message_sender=api.send_message,
                role_button_factory=opportunity_buttons,
            ),
            decisions,
        )
        discovery_schedule = DiscoverySchedule(
            store=schedule_store,
            notifier=discovery_notifier,
            clock=SystemClock(),
        )
        inputs = HostedPreparationInputStore(
            root / "data" / "hosted-preparation-inputs"
        )
        job_lookup = ScheduleJobLookup(schedule_store)
        decision_service = OpportunityDecisionService(
            inputs=inputs,
            coordinator=application_coordinator,
            job_lookup=job_lookup,
            decisions=decisions,
            actor=config.actor_id,
            send_status=api.send_status,
        )

        def sync_discovery_state() -> bool:
            return restore_latest(
                root=root,
                repository=config.hosted_artifacts.repository,
                token=github_token,
                workflow=config.hosted_artifacts.workflow,
                branch=config.hosted_artifacts.branch,
            )

        def refresh_opportunity_card(
            application_id: str,
            vacancy_version: str,
            authorization_token: str,
            before_send,
        ) -> str:
            discovery_notifier.send_alert(
                job_lookup(application_id, vacancy_version),
                reason="refreshed",
                idempotency_key=(
                    f"opportunity-refresh:{authorization_token}"
                ),
                before_send=before_send,
            )
            return "Scheda aggiornata"

        opportunity_route = build_opportunity_callback_route(
            decision_service,
            state_sync=sync_discovery_state,
            refresh_handler=refresh_opportunity_card,
        )
        background_capabilities["discovery_notifications"] = (
            DiscoveryNotificationCapability(
                state_sync=sync_discovery_state,
                schedule=discovery_schedule,
            )
        )
    if all(
        callable(getattr(application_coordinator, name, None))
        for name in (
            "pending_preparation_ids",
            "resume_pending",
            "preparation_completion_ids",
        )
    ):
        delivery_ledger = TelegramDeliveryLedger(
            config_path.parent / "application-telegram-deliveries.sqlite"
        )
        completion_notifier = TelegramPreparationCompletionNotifier(
            coordinator=application_coordinator,
            api=application_api,
            ledger=delivery_ledger,
            actor=config.actor_id,
        )
        resolution_notifier = TelegramPreparationResolutionNotifier(
            coordinator=application_coordinator,
            api=application_api,
            ledger=delivery_ledger,
            actor=config.actor_id,
        )
        background_capabilities["application_preparations"] = (
            ApplicationPreparationReconciler(
                application_coordinator,
                notifier=completion_notifier,
                resolution_notifier=resolution_notifier,
                cursor_store=OwnerOnlyPreparationNotificationCursorStore(
                    Path(state_path).parent
                    / "application-preparation-notification-cursors.json"
                ),
            )
        )
    routes = (
        *tuple(callback_routes),
        *((opportunity_route,) if opportunity_route is not None else ()),
        build_application_callback_route(
            application_coordinator,
            resolution_notifier=resolution_notifier,
        ),
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=updates,
        worker=control,
        actor_id=config.actor_id,
        chat_id=config.chat_id,
        routes=routes,
    )
    worker = build_local_worker(
        state_path=state_path,
        capabilities=background_capabilities,
        telegram_router=(
            None if config.hosted_artifacts is not None else router
        ),
        telegram_poll_timeout=telegram_poll_timeout,
        logger=(
            logger
            or RedactedStructuredLogger(
                sys.stderr,
                secrets=tuple(
                    value
                    for value in (token, github_token, encoded_handoff_key)
                    if value
                ),
            )
        ),
        reconciliation_verifiers=reconciliation_verifiers,
        safe_retry_capabilities={
            name: evidence
            for name, evidence in _IDEMPOTENT_BACKGROUND_RETRY_EVIDENCE.items()
            if name in background_capabilities
        },
    )
    control.bind(worker)
    return ProductionApplicationWorkerRuntime(
        worker=worker,
        applications=TelegramCommandHandler(
            application_coordinator, transport=application_api
        ),
        callback_encoder=encoder,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run the owner-local job worker")
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path.home()
        / "Library"
        / "Application Support"
        / "job-agent"
        / "worker-state.json",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Non-secret worker configuration (defaults beside state)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (used for health checks and tests)",
    )
    arguments = parser.parse_args(argv)
    config_path = arguments.config_path or (
        arguments.state_path.parent / "worker-config.json"
    )
    runtime = (
        runtime_factory(arguments.state_path)
        if runtime_factory is not None
        else build_production_runtime(
            state_path=arguments.state_path,
            config_path=config_path,
        )
    )
    status = runtime.status()
    if status.get("health") in {"unwired", "disabled"}:
        print(json.dumps(status, sort_keys=True))
        return 1
    if arguments.once:
        status = runtime.run_once()
        print(json.dumps(status, sort_keys=True))
        return 0 if status.get("health") in {"healthy", "paused", "stopped"} else 1
    else:
        runtime.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRODUCTION_CONFIG_VERSION",
    "PREPARATION_CURSOR_STATE_VERSION",
    "PreparationNotificationCursors",
    "PreparationNotificationCursorStore",
    "InMemoryPreparationNotificationCursorStore",
    "OwnerOnlyPreparationNotificationCursorStore",
    "ProductionWorkerConfig",
    "ProductionApplicationWorkerRuntime",
    "RuntimeFactory",
    "TelegramRouterCapability",
    "WorkerApplicationCallbackEncoder",
    "WorkerRuntime",
    "build_local_worker",
    "build_application_callback_route",
    "ApplicationPreparationReconciler",
    "build_production_runtime",
    "main",
]
