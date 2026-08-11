from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_worker import CapabilityExecutionUnavailable, LocalWorker, WorkerCommand
from local_worker_store import (
    CapabilityClaimStatus,
    LocalWorkerStore,
    ReconciliationDecision,
    ReconciliationOutcome,
)
from local_worker_telegram import (
    CallbackAuthorizationStatus,
    CallbackContext,
    CallbackRoute,
    LocalWorkerTelegramRouter,
    TelegramUpdateStore,
    TelegramWorkerHttpApi,
)


def test_poll_failure_reports_only_safe_http_diagnostics():
    class Response:
        ok = False
        status_code = 409

        def json(self):
            return {
                "ok": False,
                "error_code": 409,
                "description": "sensitive upstream detail",
            }

    class Http:
        @staticmethod
        def get(*args, **kwargs):
            return Response()

    api = TelegramWorkerHttpApi(
        token="secret-token", chat_id="99", http=Http()
    )

    with pytest.raises(RuntimeError) as failure:
        api.poll_updates(offset=None, timeout=0)

    message = str(failure.value)
    assert "http_status=409" in message
    assert "error_code=409" in message
    assert "sensitive upstream detail" not in message
    assert "secret-token" not in message


def test_callback_authorization_is_scoped_short_lived_and_one_use(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    store = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current[0])
    authorization = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:submit:123",
        resume_generation=7,
        expires_at=current[0] + timedelta(minutes=2),
    )

    mismatch = store.consume_callback_authorization(
        token=authorization.token,
        actor_id="42",
        chat_id="other",
        resume_generation=7,
    )
    accepted = store.consume_callback_authorization(
        token=authorization.token,
        actor_id="42",
        chat_id="99",
        resume_generation=7,
    )
    replay = store.consume_callback_authorization(
        token=authorization.token,
        actor_id="42",
        chat_id="99",
        resume_generation=7,
    )

    assert mismatch.status == CallbackAuthorizationStatus.MISMATCHED
    assert accepted.status == CallbackAuthorizationStatus.AUTHORIZED
    assert accepted.authorization == authorization
    assert replay.status == CallbackAuthorizationStatus.REPLAYED


def test_callback_data_is_compact_and_keeps_scope_server_side(tmp_path):
    store = TelegramUpdateStore(tmp_path / "updates.sqlite")
    authorization = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:submit:123",
        resume_generation=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    assert len(authorization.callback_data.encode("utf-8")) <= 64
    assert authorization.route not in authorization.callback_data
    assert authorization.capability not in authorization.callback_data


def test_callback_authorization_rejects_long_lived_tokens(tmp_path):
    current = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)
    store = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current)

    with pytest.raises(ValueError, match="short-lived TTL"):
        store.issue_callback_authorization(
            actor_id="42",
            chat_id="99",
            route="applications",
            capability="applications",
            payload="app:submit:123",
            resume_generation=0,
            expires_at=current + timedelta(days=1),
        )


def test_delayed_callback_rechecks_pause_at_actual_external_boundary(tmp_path):
    state = LocalWorkerStore(tmp_path / "worker.json")
    worker = LocalWorker(store=state, capabilities={})
    updates = TelegramUpdateStore(tmp_path / "updates.sqlite")
    authorization = updates.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:submit:123",
        resume_generation=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    entered = threading.Event()
    release = threading.Event()
    effects: list[str] = []

    def handler(execution, context: CallbackContext):
        entered.set()
        release.wait(timeout=2)
        execution.checkpoint()
        effects.append(context.payload)
        return "ok"

    router = LocalWorkerTelegramRouter(
        api=FakeApi([callback_update(50, authorization.callback_data)]),
        store=updates,
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=handler,
            ),
        ),
    )

    failure = []

    def consume():
        try:
            router.consume_once(timeout=0)
        except Exception as error:
            failure.append(error)

    thread = threading.Thread(target=consume)
    thread.start()
    assert entered.wait(timeout=1)
    worker.control(WorkerCommand.PAUSE)
    release.set()
    thread.join(timeout=2)

    assert effects == []
    assert failure == []


def test_delayed_callback_rechecks_expired_lease_before_external_effect(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    worker = LocalWorker(
        store=LocalWorkerStore(tmp_path / "worker.json", now=lambda: current[0]),
        capabilities={},
        execution_lease=timedelta(seconds=1),
    )
    updates = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current[0])
    authorization = updates.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:submit:123",
        resume_generation=0,
        expires_at=current[0] + timedelta(minutes=2),
    )
    effects = []

    def handler(execution, context):
        current[0] += timedelta(seconds=2)
        execution.checkpoint()
        effects.append(context.payload)
        return "unexpected"

    api = FakeApi([callback_update(51, authorization.callback_data)])
    router = LocalWorkerTelegramRouter(
        api=api,
        store=updates,
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=handler,
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert effects == []
    assert api.acknowledgements == [("callback-51", "Esito incerto: verifica manuale")]


def test_callback_authorization_new_update_replay_executes_once(tmp_path):
    store = TelegramUpdateStore(tmp_path / "updates.sqlite")
    callback_data = authorized_data(store, "app:submit")
    effects = []

    def handler(execution, context):
        execution.checkpoint()
        effects.append(context.payload)
        return "ok"

    api = FakeApi(
        [callback_update(60, callback_data), callback_update(61, callback_data)]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=store,
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(route="applications", prefixes=("app:",), handler=handler),
        ),
    )

    router.consume_once(timeout=0)

    assert effects == ["app:submit"]
    assert api.acknowledgements == [
        ("callback-60", "ok"),
        ("callback-61", "Azione già elaborata"),
    ]


def test_callback_authorization_stale_or_scope_mismatch_never_dispatches(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    store = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current[0])
    stale = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:stale",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    mismatched = store.issue_callback_authorization(
        actor_id="42",
        chat_id="expected-chat",
        route="applications",
        capability="telegram_callback",
        payload="app:mismatch",
        resume_generation=0,
        expires_at=current[0] + timedelta(minutes=1),
    )
    current[0] += timedelta(seconds=2)
    effects = []
    api = FakeApi(
        [
            callback_update(70, stale.callback_data),
            callback_update(71, mismatched.callback_data),
        ]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=store,
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: effects.append(context.payload),
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert effects == []
    assert api.acknowledgements == [
        ("callback-70", "Autorizzazione scaduta o non valida"),
        ("callback-71", "Autorizzazione scaduta o non valida"),
    ]


def test_expired_callback_can_only_use_scoped_stale_handler_once(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    store = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current[0])
    expired = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:expired-retry",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    current[0] += timedelta(seconds=2)
    effects = []
    api = FakeApi(
        [
            callback_update(72, expired.callback_data),
            callback_update(73, expired.callback_data),
            callback_update(
                74, expired.callback_data, user="attacker", chat="99"
            ),
        ]
    )
    route = CallbackRoute(
        route="applications",
        prefixes=("app:",),
        handler=lambda execution, context: effects.append("dispatch"),
        stale_handler=lambda execution, context: (
            effects.append(("reissue", context.payload)) or "expired"
        ),
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=store,
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(route,),
    )

    router.consume_once(timeout=0)

    assert effects == [("reissue", "app:expired-retry")]
    assert api.acknowledgements == [
        ("callback-72", "expired"),
        ("callback-73", "Azione già elaborata"),
        ("callback-74", "Non autorizzato"),
    ]


def test_replayed_stale_callback_can_resume_its_scoped_retry_handler(tmp_path):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    path = tmp_path / "updates.sqlite"
    store = TelegramUpdateStore(path, now=lambda: current[0])
    expired = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:expired-retry",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    current[0] += timedelta(seconds=2)
    recovery_attempts = []

    def recover(execution, context):
        execution.checkpoint()
        recovery_attempts.append(context.payload)
        if len(recovery_attempts) == 1:
            raise RuntimeError("worker stopped before replacement send")
        return "expired"

    route = CallbackRoute(
        route="applications",
        prefixes=("app:",),
        handler=lambda *_: "unexpected",
        stale_handler=recover,
        recover_stale_replay=True,
    )
    first = LocalWorkerTelegramRouter(
        api=FakeApi([callback_update(75, expired.callback_data)]),
        store=store,
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(route,),
    )

    with pytest.raises(RuntimeError, match="before replacement send"):
        first.consume_once(timeout=0)

    restarted_api = FakeApi([callback_update(76, expired.callback_data)])
    restarted = LocalWorkerTelegramRouter(
        api=restarted_api,
        store=TelegramUpdateStore(path, now=lambda: current[0]),
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(route,),
    )
    restarted.consume_once(timeout=0)

    assert recovery_attempts == ["app:expired-retry", "app:expired-retry"]
    assert restarted_api.acknowledgements == [("callback-76", "expired")]


def test_replayed_stale_callback_rejects_changed_scope_and_non_retry_payload(
    tmp_path,
):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    store = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: current[0])
    retry = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:expired-retry",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    prepare = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:prepare",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    wrong_route = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="other",
        capability="telegram_callback",
        payload="app:expired-retry",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    current[0] += timedelta(seconds=2)
    for authorization in (retry, prepare, wrong_route):
        assert store.consume_callback_authorization(
            token=authorization.token,
            actor_id="42",
            chat_id="99",
            resume_generation=0,
        ).status == CallbackAuthorizationStatus.STALE

    assert store.consume_callback_authorization(
        token=retry.token,
        actor_id="attacker",
        chat_id="99",
        resume_generation=0,
    ).status == CallbackAuthorizationStatus.MISMATCHED
    assert store.consume_callback_authorization(
        token=retry.token,
        actor_id="42",
        chat_id="other-chat",
        resume_generation=0,
    ).status == CallbackAuthorizationStatus.MISMATCHED
    assert store.consume_callback_authorization(
        token=retry.token,
        actor_id="42",
        chat_id="99",
        resume_generation=1,
    ).status == CallbackAuthorizationStatus.MISMATCHED

    recoveries = []

    def recover_retry(execution, context):
        execution.checkpoint()
        if context.payload != "app:expired-retry":
            return "mismatched"
        recoveries.append(context.payload)
        return "expired"

    api = FakeApi(
        [
            callback_update(77, prepare.callback_data),
            callback_update(78, wrong_route.callback_data),
            callback_update(79, "worker:cb:v1:forged"),
        ]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=store,
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda *_: "unexpected",
                stale_handler=recover_retry,
                recover_stale_replay=True,
            ),
        ),
    )
    router.consume_once(timeout=0)

    assert recoveries == []
    assert api.acknowledgements == [
        ("callback-77", "mismatched"),
        ("callback-78", "Azione già elaborata"),
        ("callback-79", "Autorizzazione scaduta o non valida"),
    ]


def test_callback_authorization_is_atomically_one_use_across_workers(tmp_path):
    store = TelegramUpdateStore(tmp_path / "updates.sqlite")
    authorization = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:submit",
        resume_generation=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    def consume(_):
        return store.consume_callback_authorization(
            token=authorization.token,
            actor_id="42",
            chat_id="99",
            resume_generation=0,
        ).status

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(consume, range(8)))

    assert statuses.count(CallbackAuthorizationStatus.AUTHORIZED) == 1
    assert statuses.count(CallbackAuthorizationStatus.REPLAYED) == 7


def test_legacy_update_database_migrates_callback_authorizations(tmp_path):
    path = tmp_path / "updates.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE updates (update_id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
    store = TelegramUpdateStore(path)

    authorization = store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="telegram_callback",
        payload="app:submit",
        resume_generation=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    result = store.consume_callback_authorization(
        token=authorization.token,
        actor_id="42",
        chat_id="99",
        resume_generation=0,
    )
    assert result.status == CallbackAuthorizationStatus.AUTHORIZED


class FakeWorker:
    def __init__(self):
        self.state = "resume"
        self.controls = []
        self.gated_actions = []
        self.reconciliations = []
        self.reconciliation_decision = None

    def control(self, command):
        command = WorkerCommand(command)
        self.controls.append(command)
        self.state = command.value
        return self.status()

    def status(self):
        return {
            "state": self.state,
            "health": "paused" if self.state == "pause" else "healthy",
            "resume_generation": 0,
            "capabilities": {},
        }

    def execute_gated_action(self, capability, action):
        self.gated_actions.append(capability)
        return action(FakeExecution())

    def reconcile_capability(self, capability, *, actor, provenance):
        self.reconciliations.append((capability, actor, provenance))
        assert self.reconciliation_decision is not None
        return self.reconciliation_decision


class FakeApi:
    def __init__(self, updates):
        self.updates = list(updates)
        self.offsets = []
        self.messages = []
        self.acknowledgements = []

    def poll_updates(self, *, offset, timeout):
        self.offsets.append(offset)
        return [
            item
            for item in self.updates
            if offset is None or item["update_id"] >= offset
        ]

    def send_status(self, text):
        self.messages.append(text)

    def acknowledge_callback(self, callback_query_id, text):
        self.acknowledgements.append((callback_query_id, text))


class FakeExecution:
    def __init__(self):
        self.checkpoints = 0

    def checkpoint(self, *, external_action=True):
        self.checkpoints += 1


def authorized_data(
    store,
    payload,
    *,
    route="applications",
    capability="telegram_callback",
    resume_generation=0,
):
    return store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route=route,
        capability=capability,
        payload=payload,
        resume_generation=resume_generation,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    ).callback_data


def message_update(update_id, text, *, user="42", chat="99"):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user},
            "chat": {"id": chat},
            "text": text,
        },
    }


def callback_update(update_id, data, *, user="42", chat="99"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user},
            "message": {"chat": {"id": chat}},
            "data": data,
        },
    }


def test_authorized_pause_precedes_mutating_callbacks_in_same_batch(tmp_path):
    worker = FakeWorker()
    routed = []
    api = FakeApi(
        [
            callback_update(10, "app:resume:application-1"),
            message_update(11, "/pausa"),
        ]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=TelegramUpdateStore(tmp_path / "updates.sqlite"),
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: (
                    routed.append(context.payload) or "ok"
                ),
            ),
        ),
    )

    handled = router.consume_once(timeout=0)

    assert handled == 2
    assert worker.controls == [WorkerCommand.PAUSE]
    assert routed == []
    assert api.acknowledgements == [("callback-10", "Worker in pausa")]


def test_authorized_pause_dominates_older_resume_and_callback_in_same_batch(
    tmp_path,
):
    worker = FakeWorker()
    routed = []
    api = FakeApi(
        [
            message_update(10, "/riprendi"),
            callback_update(11, "app:submit"),
            message_update(12, "/pausa"),
        ]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=TelegramUpdateStore(tmp_path / "updates.sqlite"),
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: (
                    routed.append(context.payload) or "ok"
                ),
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert worker.controls == [WorkerCommand.PAUSE]
    assert routed == []
    assert api.acknowledgements == [("callback-11", "Worker in pausa")]


def test_mutating_callback_is_dispatched_through_worker_action_gate(tmp_path):
    worker = FakeWorker()
    routed = []
    store = TelegramUpdateStore(tmp_path / "updates.sqlite")
    router = LocalWorkerTelegramRouter(
        api=FakeApi([callback_update(15, authorized_data(store, "app:submit"))]),
        store=store,
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: (
                    execution.checkpoint() or routed.append(context.payload) or "ok"
                ),
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert worker.gated_actions == ["telegram_callback"]
    assert routed == ["app:submit"]


def test_pause_between_callback_status_and_action_gate_blocks_dispatch(tmp_path):
    routed = []

    class PausingAtBoundaryWorker(LocalWorker):
        def execute_gated_action(self, capability, action):
            self.control(WorkerCommand.PAUSE)
            return super().execute_gated_action(capability, action)

    worker = PausingAtBoundaryWorker(
        store=LocalWorkerStore(tmp_path / "worker-state.json"),
        capabilities={"control": object()},
    )
    updates = TelegramUpdateStore(tmp_path / "updates.sqlite")
    api = FakeApi([callback_update(16, authorized_data(updates, "app:submit"))])
    router = LocalWorkerTelegramRouter(
        api=api,
        store=updates,
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: (
                    routed.append(context.payload) or "ok"
                ),
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert routed == []
    assert worker.status()["state"] == "pause"
    assert api.acknowledgements == [("callback-16", "Worker in pausa")]


def test_uncertain_callback_claim_is_reported_as_manual_reconciliation(tmp_path):
    class UncertainWorker(FakeWorker):
        def execute_gated_action(self, capability, action):
            raise CapabilityExecutionUnavailable(
                capability, CapabilityClaimStatus.UNCERTAIN
            )

    updates = TelegramUpdateStore(tmp_path / "updates.sqlite")
    api = FakeApi([callback_update(17, authorized_data(updates, "app:submit"))])
    router = LocalWorkerTelegramRouter(
        api=api,
        store=updates,
        worker=UncertainWorker(),
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda *_: "unexpected",
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert api.acknowledgements == [("callback-17", "Esito incerto: verifica manuale")]


def test_global_resume_command_is_distinct_from_scoped_resume_callback(tmp_path):
    worker = FakeWorker()
    worker.state = "pause"
    routed = []
    updates = TelegramUpdateStore(tmp_path / "updates.sqlite")
    api = FakeApi(
        [
            message_update(20, "/riprendi"),
            callback_update(21, authorized_data(updates, "app:resume:application-1")),
        ]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=updates,
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: (
                    execution.checkpoint()
                    or routed.append(context.payload)
                    or "application resumed"
                ),
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert worker.controls == [WorkerCommand.RESUME]
    assert routed == ["app:resume:application-1"]
    assert api.acknowledgements == [("callback-21", "application resumed")]


def test_authorized_reconciliation_command_and_callback_use_worker_verifier(
    tmp_path,
):
    worker = FakeWorker()
    worker.reconciliation_decision = ReconciliationDecision(
        capability="applications",
        outcome=ReconciliationOutcome.RETRY_VERIFIED,
        evidence="No external effect found",
        provenance="telegram:command",
        decided_at="2026-07-16T12:00:00+00:00",
        actor="42",
    )
    command_api = FakeApi([message_update(22, "/riconcilia applications")])
    command_router = LocalWorkerTelegramRouter(
        api=command_api,
        store=TelegramUpdateStore(tmp_path / "commands.sqlite"),
        worker=worker,
        actor_id="42",
        chat_id="99",
    )

    command_router.consume_once(timeout=0)

    assert worker.reconciliations == [("applications", "42", "telegram:command")]
    assert command_api.messages

    worker.reconciliation_decision = ReconciliationDecision(
        capability="applications",
        outcome=ReconciliationOutcome.RETRY_VERIFIED,
        evidence="No external effect found",
        provenance="telegram:callback",
        decided_at="2026-07-16T12:01:00+00:00",
        actor="42",
    )
    callback_store = TelegramUpdateStore(tmp_path / "callbacks.sqlite")
    callback_api = FakeApi(
        [
            callback_update(
                23,
                authorized_data(
                    callback_store,
                    "worker:reconcile:applications",
                    route="reconcile",
                    capability="applications",
                ),
            )
        ]
    )
    callback_router = LocalWorkerTelegramRouter(
        api=callback_api,
        store=callback_store,
        worker=worker,
        actor_id="42",
        chat_id="99",
    )

    callback_router.consume_once(timeout=0)

    assert worker.reconciliations[-1] == (
        "applications",
        "42",
        "telegram:callback",
    )
    assert callback_api.acknowledgements == [
        ("callback-23", "Riconciliazione verificata")
    ]


def test_unauthorized_controls_and_callbacks_have_zero_effect(tmp_path):
    worker = FakeWorker()
    routed = []
    api = FakeApi(
        [
            message_update(30, "/pausa", user="attacker"),
            callback_update(31, "app:submit", chat="other-chat"),
        ]
    )
    router = LocalWorkerTelegramRouter(
        api=api,
        store=TelegramUpdateStore(tmp_path / "updates.sqlite"),
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=lambda execution, context: (
                    routed.append(context.payload) or "ok"
                ),
            ),
        ),
    )

    router.consume_once(timeout=0)

    assert worker.controls == []
    assert routed == []
    assert worker.state == "resume"
    assert api.acknowledgements == [("callback-31", "Non autorizzato")]


def test_callback_crash_is_durable_uncertain_and_never_replayed(tmp_path):
    calls = []

    def crash_after_effect(execution, context):
        execution.checkpoint()
        calls.append(context.payload)
        raise RuntimeError("crash after external effect")

    path = tmp_path / "updates.sqlite"
    first_store = TelegramUpdateStore(path)
    update = callback_update(40, authorized_data(first_store, "app:submit"))
    first_api = FakeApi([update])
    first = LocalWorkerTelegramRouter(
        api=first_api,
        store=first_store,
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=crash_after_effect,
            ),
        ),
    )

    try:
        first.consume_once(timeout=0)
    except RuntimeError:
        pass

    restarted_api = FakeApi([update])
    restarted = LocalWorkerTelegramRouter(
        api=restarted_api,
        store=TelegramUpdateStore(path),
        worker=FakeWorker(),
        actor_id="42",
        chat_id="99",
        routes=(
            CallbackRoute(
                route="applications",
                prefixes=("app:",),
                handler=crash_after_effect,
            ),
        ),
    )
    restarted.consume_once(timeout=0)

    assert calls == ["app:submit"]
    assert restarted_api.acknowledgements == [
        ("callback-40", "Esito incerto: verifica manuale")
    ]
    assert restarted_api.offsets == [None]
    assert TelegramUpdateStore(path).offset() == 41
