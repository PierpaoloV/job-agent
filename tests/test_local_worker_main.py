from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import stat
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_worker_main import (  # noqa: E402
    ApplicationPreparationReconciler,
    OwnerOnlyPreparationNotificationCursorStore,
    PreparationNotificationCursors,
    TelegramRouterCapability,
    WorkerApplicationCallbackEncoder,
    build_application_callback_route,
    build_local_worker,
    build_production_runtime,
    main,
)
from application_domain import (  # noqa: E402
    ActionCommand,
    AuthorizationScope,
    CommandResult,
    CommandStatus,
    LifecycleState,
    WorkflowAction,
)
from local_worker_store import LocalWorkerStore  # noqa: E402
from local_worker_telegram import TelegramUpdateStore  # noqa: E402


class RecordingExecution:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def checkpoint(self, *, external_action: bool = True) -> None:
        self._events.append(f"checkpoint:{external_action}")


class RecordingRouter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def consume_once(self, *, timeout: int = 25) -> int:
        self._events.append(f"poll:{timeout}")
        return 2


def test_telegram_router_capability_checks_gate_at_external_boundary():
    events: list[str] = []
    capability = TelegramRouterCapability(
        router=RecordingRouter(events), poll_timeout=0
    )

    capability.recompute(4)
    capability.run_once(RecordingExecution(events))

    assert events == ["checkpoint:False", "poll:0"]
    assert capability.status() == {
        "state": "ready",
        "healthy": True,
    }


def test_composition_wires_router_as_a_real_worker_capability(tmp_path):
    events: list[str] = []
    worker = build_local_worker(
        state_path=tmp_path / "worker-state.json",
        telegram_router=RecordingRouter(events),
        telegram_poll_timeout=0,
    )

    status = worker.run_once()

    assert events == ["poll:0"]
    assert status["health"] == "healthy"
    assert set(status["capabilities"]) == {"telegram"}


def test_composition_recovers_only_the_safe_telegram_poll_envelope(tmp_path):
    events: list[str] = []
    state_path = tmp_path / "worker-state.json"
    store = LocalWorkerStore(state_path)
    attempt = store.claim_capability(
        "telegram",
        0,
        owner="terminated-worker",
        lease_duration=timedelta(minutes=5),
    )
    assert attempt.claim is not None
    store.mark_capability_uncertain(attempt.claim)

    worker = build_local_worker(
        state_path=state_path,
        telegram_router=RecordingRouter(events),
        telegram_poll_timeout=0,
    )

    worker.run_once()

    assert events == ["poll:0"]
    assert "telegram" not in store.read().capability_claims
    audit = store.reconciliation_audit()
    assert len(audit) == 1
    assert audit[0].capability == "telegram"
    assert audit[0].actor == "system:worker"


def test_preparation_reconciler_advances_each_intent_once_without_polling_loop():
    events = []

    class Coordinator:
        pending = ["app-1", "app-2"]

        def pending_preparation_ids(self):
            return tuple(self.pending)

        def resume_pending(self, application_id):
            events.append(f"reconcile:{application_id}")
            self.pending.remove(application_id)

    capability = ApplicationPreparationReconciler(Coordinator())
    execution = RecordingExecution(events)

    capability.run_once(execution)

    assert events == [
        "checkpoint:True",
        "reconcile:app-1",
        "checkpoint:True",
        "reconcile:app-2",
    ]
    assert capability.status() == {"state": "idle", "healthy": True, "pending": 0}


def test_preparation_reconciler_notifies_only_verified_completed_transitions():
    events = []

    class Coordinator:
        pending = ["pending", "failed", "ambiguous", "completed"]

        def pending_preparation_ids(self):
            return tuple(self.pending)

        def preparation_completion_ids(self):
            return ("completed",)

        def resume_pending(self, application_id):
            self.pending.remove(application_id)
            statuses = {
                "pending": CommandStatus.ACCEPTED,
                "failed": CommandStatus.FAILED,
                "ambiguous": CommandStatus.RECONCILIATION_REQUIRED,
                "completed": CommandStatus.COMPLETED,
            }
            return CommandResult(
                statuses[application_id],
                LifecycleState.CV_READY,
                (
                    WorkflowAction.FILL
                    if application_id == "completed"
                    else None
                ),
            )

    class Notifier:
        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"notify:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(), notifier=Notifier(), limit=4
    )
    capability.run_once(RecordingExecution(events))

    assert events == [
        "checkpoint:True",
        "checkpoint:True",
        "checkpoint:True",
        "checkpoint:True",
        "checkpoint:True",
        "notify:completed",
    ]


def test_preparation_reconciler_durably_surfaces_terminal_resolution():
    events = []

    class Coordinator:
        pending = ["failed"]

        def pending_preparation_ids(self):
            return tuple(self.pending)

        def preparation_resolution_ids(self):
            return ("failed",)

        def resume_pending(self, application_id):
            self.pending.remove(application_id)
            return CommandResult(
                CommandStatus.RECONCILIATION_REQUIRED,
                LifecycleState.APPROVED,
                None,
            )

    class ResolutionNotifier:
        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"resolve:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(),
        resolution_notifier=ResolutionNotifier(),
    )
    capability.run_once(RecordingExecution(events))

    assert events == [
        "checkpoint:True",
        "checkpoint:True",
        "resolve:failed",
    ]


def test_preparation_reconciler_recovers_unnotified_completed_state_after_restart():
    events = []

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return ("completed-before-crash",)

    class Notifier:
        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"notify:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(), notifier=Notifier()
    )
    capability.run_once(RecordingExecution(events))

    assert events == [
        "checkpoint:True",
        "notify:completed-before-crash",
    ]


def test_preparation_reconciler_skips_deduplicated_completions_without_spending_budget():
    events = []
    completion_ids = tuple(f"completed-{index}" for index in range(1, 7))

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return completion_ids

    class Notifier:
        def needs_delivery(self, application_id):
            events.append(f"check:{application_id}")
            return application_id == "completed-6"

        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"notify:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(), notifier=Notifier(), limit=5
    )
    capability.run_once(RecordingExecution(events))

    assert events == [
        "check:completed-1",
        "check:completed-2",
        "check:completed-3",
        "check:completed-4",
        "check:completed-5",
        "check:completed-6",
        "checkpoint:True",
        "notify:completed-6",
    ]


def test_preparation_reconciler_skips_deduplicated_resolutions_without_spending_budget():
    events = []
    resolution_ids = tuple(f"resolution-{index}" for index in range(1, 7))

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_resolution_ids(self):
            return resolution_ids

    class Notifier:
        def needs_delivery(self, application_id):
            events.append(f"check:{application_id}")
            return application_id == "resolution-6"

        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"resolve:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(), resolution_notifier=Notifier(), limit=5
    )
    capability.run_once(RecordingExecution(events))

    assert events == [
        "check:resolution-1",
        "check:resolution-2",
        "check:resolution-3",
        "check:resolution-4",
        "check:resolution-5",
        "check:resolution-6",
        "checkpoint:True",
        "resolve:resolution-6",
    ]


def test_preparation_reconciler_bounds_terminal_scans_and_advances_cursor():
    events = []
    completion_ids = tuple(f"completed-{index}" for index in range(30))

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return completion_ids

    class Notifier:
        def needs_delivery(self, application_id):
            events.append(f"check:{application_id}")
            return False

        def notify(self, application_id, *, before_send):
            raise AssertionError(f"deduplicated notice reached send: {application_id}")

    capability = ApplicationPreparationReconciler(
        Coordinator(), notifier=Notifier(), limit=5
    )

    capability.run_once(RecordingExecution(events))
    capability.run_once(RecordingExecution(events))

    assert events == [
        *(f"check:completed-{index}" for index in range(20)),
    ]


def test_preparation_reconciler_resumes_fair_scan_after_store_and_process_restart(
    tmp_path,
):
    events = []
    completion_ids = tuple(f"completed-{index}" for index in range(30))
    state_path = tmp_path / "state" / "notification-cursors.json"

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return completion_ids

    class Notifier:
        def needs_delivery(self, application_id):
            events.append(f"check:{application_id}")
            return False

        def notify(self, application_id, *, before_send):
            raise AssertionError(f"deduplicated notice reached send: {application_id}")

    first = ApplicationPreparationReconciler(
        Coordinator(),
        notifier=Notifier(),
        limit=5,
        cursor_store=OwnerOnlyPreparationNotificationCursorStore(state_path),
    )
    first.run_once(RecordingExecution(events))
    restarted = ApplicationPreparationReconciler(
        Coordinator(),
        notifier=Notifier(),
        limit=5,
        cursor_store=OwnerOnlyPreparationNotificationCursorStore(state_path),
    )
    restarted.run_once(RecordingExecution(events))

    assert events == [
        *(f"check:completed-{index}" for index in range(20)),
    ]
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert state_path.parent.stat().st_uid == os.getuid()
    assert state_path.stat().st_uid == os.getuid()


def test_preparation_reconciler_persists_completion_and_resolution_cursors_independently(
    tmp_path,
):
    events = []
    state_path = tmp_path / "state" / "notification-cursors.json"

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return tuple(f"completed-{index}" for index in range(12))

        def preparation_resolution_ids(self):
            return tuple(f"resolution-{index}" for index in range(12))

    class Notifier:
        def __init__(self, prefix):
            self._prefix = prefix

        def needs_delivery(self, application_id):
            events.append(f"{self._prefix}:{application_id}")
            return False

        def notify(self, application_id, *, before_send):
            raise AssertionError(f"deduplicated notice reached send: {application_id}")

    def reconciler():
        return ApplicationPreparationReconciler(
            Coordinator(),
            notifier=Notifier("completion"),
            resolution_notifier=Notifier("resolution"),
            limit=2,
            cursor_store=OwnerOnlyPreparationNotificationCursorStore(state_path),
        )

    reconciler().run_once(RecordingExecution(events))
    reconciler().run_once(RecordingExecution(events))

    assert events == [
        *(f"completion:completed-{index}" for index in range(4)),
        *(f"resolution:resolution-{index}" for index in range(4)),
        *(f"completion:completed-{index}" for index in range(4, 8)),
        *(f"resolution:resolution-{index}" for index in range(4, 8)),
    ]


@pytest.mark.parametrize(
    "invalid_state",
    ["corrupt", "mode", "directory_mode", "symlink", "relocated"],
)
def test_preparation_reconciler_fails_closed_for_untrusted_cursor_state(
    tmp_path, invalid_state
):
    events = []
    original = tmp_path / "original" / "notification-cursors.json"
    store = OwnerOnlyPreparationNotificationCursorStore(original)
    store.load()

    state_path = original
    if invalid_state == "corrupt":
        original.write_text("not-json", encoding="utf-8")
    elif invalid_state == "mode":
        original.chmod(0o644)
    elif invalid_state == "directory_mode":
        original.parent.chmod(0o755)
    elif invalid_state == "symlink":
        target = tmp_path / "target.json"
        shutil.copy2(original, target)
        original.unlink()
        original.symlink_to(target)
    else:
        relocated_directory = tmp_path / "relocated"
        relocated_directory.mkdir(mode=0o700)
        state_path = relocated_directory / original.name
        shutil.copy2(original, state_path)

    class Coordinator:
        def pending_preparation_ids(self):
            events.append("pending")
            return ()

        def preparation_completion_ids(self):
            return ("completed",)

    class Notifier:
        def notify(self, application_id, *, before_send):
            events.append(f"notify:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(),
        notifier=Notifier(),
        cursor_store=OwnerOnlyPreparationNotificationCursorStore(state_path),
    )

    assert capability.status() == {"state": "unavailable", "healthy": False}
    with pytest.raises(RuntimeError, match="cursor state is unavailable"):
        capability.run_once(RecordingExecution(events))
    assert events == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {**payload, "unexpected": True},
        lambda payload: {key: value for key, value in payload.items() if key != "version"},
        lambda payload: {**payload, "version": "future-version"},
        lambda payload: {**payload, "completion_cursor": True},
        lambda payload: {**payload, "resolution_cursor": -1},
    ],
)
def test_cursor_store_rejects_noncanonical_schema_and_cursor_values(
    tmp_path, mutation
):
    state_path = tmp_path / "state" / "notification-cursors.json"
    store = OwnerOnlyPreparationNotificationCursorStore(state_path)
    store.load()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_path.write_text(
        json.dumps(mutation(payload)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        OwnerOnlyPreparationNotificationCursorStore(state_path).load()


def test_cursor_store_atomically_fsyncs_private_state_and_directory(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state" / "notification-cursors.json"
    fsync_calls = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr("local_worker_main.os.fsync", recording_fsync)
    store = OwnerOnlyPreparationNotificationCursorStore(state_path)
    store.load()
    store.save(
        PreparationNotificationCursors(
            completion=11,
            resolution=7,
        )
    )

    assert len(fsync_calls) >= 4
    assert set(state_path.parent.iterdir()) == {state_path}
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_preparation_reconciler_bounds_notify_calls_even_when_delivery_returns_false():
    events = []

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return tuple(f"completed-{index}" for index in range(20))

    class Notifier:
        def needs_delivery(self, application_id):
            events.append(f"check:{application_id}")
            return True

        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"notify:{application_id}")
            return False

    capability = ApplicationPreparationReconciler(
        Coordinator(), notifier=Notifier(), limit=5
    )
    capability.run_once(RecordingExecution(events))

    assert len([event for event in events if event.startswith("check:")]) == 5
    assert len([event for event in events if event.startswith("notify:")]) == 5


def test_preparation_reconciler_bounds_new_delivery_attempts():
    events = []
    completion_ids = tuple(f"completed-{index}" for index in range(12))

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return completion_ids

    class Notifier:
        def needs_delivery(self, application_id):
            events.append(f"check:{application_id}")
            return True

        def notify(self, application_id, *, before_send):
            before_send()
            events.append(f"notify:{application_id}")
            return True

    capability = ApplicationPreparationReconciler(
        Coordinator(), notifier=Notifier(), limit=5
    )
    capability.run_once(RecordingExecution(events))

    assert [event for event in events if event.startswith("notify:")] == [
        f"notify:completed-{index}" for index in range(5)
    ]
    assert [event for event in events if event.startswith("check:")] == [
        f"check:completed-{index}" for index in range(5)
    ]


def test_worker_services_telegram_before_one_preparation_reconciliation(tmp_path):
    events = []

    class Reconciler:
        def recompute(self, resume_generation):
            return None

        def run_once(self, execution):
            execution.checkpoint()
            events.append("reconcile")

        def status(self):
            return {"state": "ready", "healthy": True}

    worker = build_local_worker(
        state_path=tmp_path / "worker-state.json",
        telegram_router=RecordingRouter(events),
        telegram_poll_timeout=0,
        capabilities={"application_preparations": Reconciler()},
    )

    worker.run_once()

    assert events == ["poll:0", "reconcile"]


def test_application_route_rechecks_gate_before_application_handler():
    events = []

    class Coordinator:
        def command_for_token(self, token):
            events.append(f"lookup:{token}")
            return object()

        def handle(self, command):
            del command
            events.append("handle")
            return SimpleNamespace(status=SimpleNamespace(value="completed"))

    route = build_application_callback_route(Coordinator())
    execution = RecordingExecution(events)

    result = route.handler(
        execution,
        SimpleNamespace(payload="app:authorization-token"),
    )

    assert route.route == route.capability == "applications"
    assert result == "completed"
    assert events == ["checkpoint:True", "lookup:authorization-token", "handle"]


def test_application_route_reissues_expired_retry_without_dispatching_it():
    events = []
    expired = ActionCommand(
        token="expired-retry",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:failed-intent",
        ),
    )
    replacement = ActionCommand(
        token="replacement-retry",
        scope=expired.scope,
    )

    class Coordinator:
        def command_for_token(self, token):
            events.append(f"lookup:{token}")
            return {
                expired.token: expired,
                replacement.token: replacement,
            }.get(token)

        def handle(self, command):
            events.append(f"handle:{command.token}")
            if command == expired:
                return CommandResult(CommandStatus.EXPIRED, None, None)
            events.append("dispatch")
            return CommandResult(CommandStatus.COMPLETED, None, None)

    class ResolutionNotifier:
        def reissue_expired_retry(self, command, *, before_send):
            events.append(f"reissue:{command.token}")
            before_send()
            return True

    route = build_application_callback_route(
        Coordinator(), resolution_notifier=ResolutionNotifier()
    )
    execution = RecordingExecution(events)

    assert route.handler(
        execution,
        SimpleNamespace(payload=f"app:{expired.token}"),
    ) == "expired"
    assert "dispatch" not in events

    assert route.handler(
        execution,
        SimpleNamespace(payload=f"app:{replacement.token}"),
    ) == "completed"
    assert events.count("dispatch") == 1


def test_application_stale_route_only_reissues_persisted_retry_command():
    events = []
    retry = ActionCommand(
        token="persisted-retry",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:failed-intent",
        ),
    )
    prepare = ActionCommand(
        token="persisted-prepare",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.PREPARE,
            version="vacancy-v1",
        ),
    )

    class Coordinator:
        def command_for_token(self, token):
            return {
                retry.token: retry,
                prepare.token: prepare,
            }.get(token)

        def handle(self, command):
            raise AssertionError(f"stale callback dispatched: {command}")

    class ResolutionNotifier:
        def reissue_expired_retry(self, command, *, before_send):
            events.append(command.token)
            before_send()
            return True

    route = build_application_callback_route(
        Coordinator(), resolution_notifier=ResolutionNotifier()
    )
    assert route.stale_handler is not None
    assert route.recover_stale_replay is True
    execution = RecordingExecution(events)

    assert route.stale_handler(
        execution, SimpleNamespace(payload=f"app:{retry.token}")
    ) == "expired"
    assert route.stale_handler(
        execution, SimpleNamespace(payload=f"app:{prepare.token}")
    ) == "mismatched"
    assert route.stale_handler(
        execution, SimpleNamespace(payload="app:forged")
    ) == "mismatched"
    assert events == ["persisted-retry", "checkpoint:True"]


def test_application_callback_encoder_issues_compact_server_scoped_token(tmp_path):
    current = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)
    store = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current)
    worker = SimpleNamespace(status=lambda: {"state": "resume", "resume_generation": 4})
    encoder = WorkerApplicationCallbackEncoder(
        store=store,
        worker=worker,
        actor_id="42",
        chat_id="99",
        now=lambda: current,
    )

    callback_data = encoder(
        ActionCommand(
            token="application-authorization-token",
            scope=AuthorizationScope(
                application_id="application-001",
                action=WorkflowAction.SUBMIT,
                version="manifest-v1",
            ),
        )
    )

    assert len(callback_data.encode("utf-8")) <= 64
    assert "application-authorization-token" not in callback_data
    assert "applications" not in callback_data


def test_production_cli_runs_injected_runtime_without_real_external_calls(tmp_path):
    calls: list[object] = []

    class Runtime:
        def status(self):
            return {"health": "degraded"}

        def run_once(self):
            calls.append("run_once")
            return {"health": "healthy"}

        def serve(self):
            raise AssertionError("--once must not serve forever")

    def runtime_factory(state_path: Path):
        calls.append(state_path)
        return Runtime()

    state_path = tmp_path / "worker-state.json"
    exit_code = main(
        ["--once", "--state-path", str(state_path)],
        runtime_factory=runtime_factory,
    )

    assert exit_code == 0
    assert calls == [state_path, "run_once"]


def test_production_factory_loads_non_secret_config_and_keychain_token(tmp_path):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "job-agent.local-worker-config.v1",
                "telegram": {
                    "actor_id": "42",
                    "chat_id": "99",
                    "token_keychain_service": "job-agent.telegram",
                    "token_keychain_account": "worker-bot",
                },
            }
        ),
        encoding="utf-8",
    )
    secret_reads = []
    api_calls = []

    class SecretStore:
        def get(self, service, account):
            secret_reads.append((service, account))
            return "telegram-secret"

    class Api:
        def poll_updates(self, *, offset, timeout):
            return []

        def send_status(self, text):
            raise AssertionError("No status message expected")

        def acknowledge_callback(self, callback_query_id, text):
            raise AssertionError("No callback expected")

    def api_factory(*, token, chat_id):
        api_calls.append((token, chat_id))
        return Api()

    class Coordinator:
        def command_for_token(self, token):
            return None

        def handle(self, command):
            raise AssertionError("No application callback expected")

    runtime = build_production_runtime(
        state_path=tmp_path / "worker-state.json",
        config_path=config_path,
        secret_store=SecretStore(),
        api_factory=api_factory,
        application_coordinator=Coordinator(),
        application_api_factory=lambda **kwargs: SimpleNamespace(),
        telegram_poll_timeout=0,
    )

    status = runtime.run_once()

    assert status["health"] == "healthy"
    assert set(status["capabilities"]) == {"telegram"}
    assert secret_reads == [("job-agent.telegram", "worker-bot")]
    assert api_calls == [("telegram-secret", "99")]
    assert "telegram-secret" not in config_path.read_text(encoding="utf-8")


def test_production_factory_persists_preparation_cursors_beside_worker_state(
    tmp_path,
):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "job-agent.local-worker-config.v1",
                "telegram": {
                    "actor_id": "42",
                    "chat_id": "99",
                    "token_keychain_service": "job-agent.telegram",
                    "token_keychain_account": "worker-bot",
                },
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "private-state" / "worker-state.json"

    class Coordinator:
        def pending_preparation_ids(self):
            return ()

        def preparation_completion_ids(self):
            return ()

        def resume_pending(self, application_id):
            raise AssertionError(f"No preparation should resume: {application_id}")

        def command_for_token(self, token):
            return None

        def handle(self, command):
            raise AssertionError("No application callback expected")

    runtime = build_production_runtime(
        state_path=state_path,
        config_path=config_path,
        secret_store=SimpleNamespace(get=lambda service, account: "telegram-secret"),
        api_factory=lambda **kwargs: SimpleNamespace(
            poll_updates=lambda **poll: []
        ),
        application_coordinator=Coordinator(),
        application_api_factory=lambda **kwargs: SimpleNamespace(),
        telegram_poll_timeout=0,
    )

    cursor_path = (
        state_path.parent
        / "application-preparation-notification-cursors.json"
    )
    assert runtime.status()["capabilities"]["application_preparations"][
        "healthy"
    ]
    assert cursor_path.is_file()
    assert stat.S_IMODE(cursor_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(cursor_path.stat().st_mode) == 0o600
    assert json.loads(cursor_path.read_text(encoding="utf-8"))["location"] == str(
        cursor_path
    )


def test_production_factory_fails_closed_without_hosted_artifact_config(tmp_path):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "job-agent.local-worker-config.v1",
                "telegram": {
                    "actor_id": "42",
                    "chat_id": "99",
                    "token_keychain_service": "job-agent.telegram",
                    "token_keychain_account": "worker-bot",
                },
            }
        ),
        encoding="utf-8",
    )

    runtime = build_production_runtime(
        state_path=tmp_path / "worker-state.json",
        config_path=config_path,
        secret_store=SimpleNamespace(get=lambda service, account: "telegram-secret"),
        api_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Control-only Telegram runtime must not start")
        ),
    )

    assert runtime.status()["health"] == "disabled"
    assert runtime.status()["reason"] == "hosted_artifact_configuration_missing"


def test_production_factory_shares_application_issuer_and_worker_route(tmp_path):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "job-agent.local-worker-config.v1",
                "telegram": {
                    "actor_id": "42",
                    "chat_id": "99",
                    "token_keychain_service": "job-agent.telegram",
                    "token_keychain_account": "worker-bot",
                },
            }
        ),
        encoding="utf-8",
    )
    updates = []
    acknowledgements = []

    class WorkerApi:
        def poll_updates(self, *, offset, timeout):
            del timeout
            return [
                item
                for item in updates
                if offset is None or item["update_id"] >= offset
            ]

        def send_status(self, text):
            raise AssertionError(f"Unexpected status: {text}")

        def acknowledge_callback(self, callback_query_id, text):
            acknowledgements.append((callback_query_id, text))

    application_api_calls = []

    class ApplicationApi:
        def __init__(self, **kwargs):
            application_api_calls.append(kwargs)

    handled = []

    class Coordinator:
        def command_for_token(self, token):
            return command if token == command.token else None

        def handle(self, received):
            handled.append(received)
            return SimpleNamespace(status=SimpleNamespace(value="completed"))

    command = ActionCommand(
        token="application-authorization-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.SUBMIT,
            version="manifest-v1",
        ),
    )
    runtime = build_production_runtime(
        state_path=tmp_path / "worker-state.json",
        config_path=config_path,
        secret_store=SimpleNamespace(get=lambda service, account: "telegram-secret"),
        api_factory=lambda **kwargs: WorkerApi(),
        application_coordinator=Coordinator(),
        application_api_factory=lambda **kwargs: ApplicationApi(**kwargs),
        telegram_poll_timeout=0,
    )
    callback_data = runtime.callback_encoder(command)
    updates.append(
        {
            "update_id": 1,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": "42"},
                "message": {"chat": {"id": "99"}},
                "data": callback_data,
            },
        }
    )

    runtime.run_once()

    assert len(application_api_calls) == 1
    assert application_api_calls[0]["callback_encoder"] is runtime.callback_encoder
    assert len(callback_data.encode("utf-8")) <= 64
    assert handled == [command]
    assert acknowledgements == [("callback-1", "completed")]


def test_default_cli_exits_successfully_when_local_config_is_absent(tmp_path):
    assert main(["--state-path", str(tmp_path / "state.json")]) == 0
