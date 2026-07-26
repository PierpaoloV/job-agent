from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application_domain import (  # noqa: E402
    ActionCommand,
    AuthorizationScope,
    LifecycleState,
    WorkflowAction,
)
from application_workflow import ApplicationWorkflowCoordinator  # noqa: E402
from telegram_applications import (  # noqa: E402
    TelegramApplicationApi,
    TelegramPreparationCompletionNotifier,
    TelegramPreparationResolutionNotifier,
    preparation_completion_delivery_key,
    preparation_resolution_delivery_key,
    preparation_retry_reissue_delivery_key,
)
from telegram_delivery import TelegramDeliveryLedger  # noqa: E402
from hosted_tailoring import HostedPreparationResolution  # noqa: E402
from local_worker_telegram import (  # noqa: E402
    CallbackRoute,
    LocalWorkerTelegramRouter,
    TelegramUpdateStore,
)


class CompletionCoordinator:
    def __init__(self) -> None:
        self.authorizations = []
        self.application = SimpleNamespace(
            application_id="application-001",
            opportunity={
                "company": "Example AI",
                "title": "Research Engineer",
                "location": "Zurich",
            },
            official_vacancy=SimpleNamespace(
                version="sha256:" + "a" * 64,
            ),
            artifacts=SimpleNamespace(
                version="sha256:" + "b" * 64,
            ),
            next_action=WorkflowAction.FILL,
        )

    def get(self, application_id):
        assert application_id == self.application.application_id
        return self.application

    def issue_authorization(self, application_id, action, *, actor, ttl):
        self.authorizations.append((application_id, action, actor, ttl))
        return ActionCommand(
            token=f"fill-token-{len(self.authorizations)}",
            scope=AuthorizationScope(
                application_id=application_id,
                action=action,
                version=self.application.artifacts.version,
            ),
        )


class CompletionApi:
    def __init__(self, *, fail_after_send: bool = False) -> None:
        self.deliveries = []
        self.fail_after_send = fail_after_send

    def send_preparation_completed(self, summary, command):
        self.deliveries.append((summary, command))
        if self.fail_after_send:
            raise RuntimeError("response lost after possible send")


class ResolutionCoordinator:
    def __init__(self, *, retry_safe=True) -> None:
        self.retry_safe = retry_safe
        self.resolution_checks = 0
        self.authorizations = []
        self.intent = SimpleNamespace(
            intent_id="prepare:old-token",
            action=WorkflowAction.PREPARE,
            is_pending=True,
        )
        self.application = SimpleNamespace(
            application_id="application-001",
            opportunity={
                "company": "Example AI",
                "title": "Research Engineer",
                "location": "Zurich",
            },
            official_vacancy=SimpleNamespace(
                version="sha256:" + "a" * 64,
            ),
            operation_intents=(self.intent,),
        )

    def get(self, application_id):
        assert application_id == "application-001"
        return self.application

    def preparation_resolution(self, application_id):
        assert application_id == "application-001"
        self.resolution_checks += 1
        return HostedPreparationResolution(
            intent_id=self.intent.intent_id,
            phase="resolution_required",
            reason="no workflow run appeared before the deadline",
            retry_safe=self.retry_safe,
        )

    def issue_preparation_retry_authorization(
        self, application_id, *, actor, ttl
    ):
        self.authorizations.append((application_id, actor, ttl))
        return ActionCommand(
            token=f"retry-token-{len(self.authorizations)}",
            scope=AuthorizationScope(
                application_id=application_id,
                action=WorkflowAction.RETRY_PREPARATION,
                version=self.intent.intent_id,
            ),
        )


class ResolutionApi:
    def __init__(self, *, crash=False) -> None:
        self.deliveries = []
        self.crash = crash

    def send_preparation_resolution(self, summary, command):
        self.deliveries.append((summary, command))
        if self.crash:
            raise SystemExit("stopped after send boundary")


class ReplayExecution:
    def checkpoint(self, *, external_action=True):
        del external_action


class ReplayWorker:
    def status(self):
        return {"state": "resume", "resume_generation": 0}

    def execute_gated_action(self, capability, action):
        assert capability == "applications"
        return action(ReplayExecution())


class CallbackApi:
    def __init__(self, updates):
        self.updates = list(updates)
        self.acknowledgements = []

    def poll_updates(self, *, offset, timeout):
        del timeout
        return [
            update
            for update in self.updates
            if offset is None or update["update_id"] >= offset
        ]

    def acknowledge_callback(self, callback_query_id, text):
        self.acknowledgements.append((callback_query_id, text))


def callback_update(update_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": "42"},
            "message": {"chat": {"id": "99"}},
            "data": data,
        },
    }


def build_resolution_notifier(tmp_path, coordinator, api):
    return TelegramPreparationResolutionNotifier(
        coordinator=coordinator,
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "telegram-deliveries.sqlite"),
        actor="42",
    )


def build_notifier(tmp_path, coordinator, api):
    return TelegramPreparationCompletionNotifier(
        coordinator=coordinator,
        api=api,
        ledger=TelegramDeliveryLedger(tmp_path / "telegram-deliveries.sqlite"),
        actor="42",
    )


def test_completed_preparation_sends_role_specific_compila_once_across_restart(
    tmp_path,
):
    coordinator = CompletionCoordinator()
    api = CompletionApi()

    first = build_notifier(tmp_path, coordinator, api)
    restarted = build_notifier(tmp_path, coordinator, api)

    assert first.notify("application-001") is True
    assert restarted.notify("application-001") is False
    assert len(api.deliveries) == 1
    summary, command = api.deliveries[0]
    assert summary.company == "Example AI"
    assert summary.title == "Research Engineer"
    assert summary.location == "Zurich"
    assert summary.official_vacancy_version == "sha256:" + "a" * 64
    assert summary.artifact_version == "sha256:" + "b" * 64
    assert command.scope.action == WorkflowAction.FILL
    assert coordinator.authorizations == [
        (
            "application-001",
            WorkflowAction.FILL,
            "42",
            timedelta(minutes=30),
        )
    ]


def test_completion_notifier_exposes_read_only_delivery_eligibility(tmp_path):
    coordinator = CompletionCoordinator()
    api = CompletionApi()
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    notifier = build_notifier(tmp_path, coordinator, api)
    key = preparation_completion_delivery_key(
        "application-001", "sha256:" + "b" * 64
    )

    assert notifier.needs_delivery("application-001") is True
    TelegramDeliveryLedger(ledger_path).stage_outbound(key)
    assert notifier.needs_delivery("application-001") is True
    assert TelegramDeliveryLedger(ledger_path).claim_outbound(key) is not None
    assert notifier.needs_delivery("application-001") is True

    assert notifier.notify("application-001") is True
    assert notifier.needs_delivery("application-001") is False


@pytest.mark.parametrize("status", ("sending", "sent", "uncertain"))
def test_completion_notifier_deduplicates_terminal_delivery_states(
    tmp_path, status
):
    coordinator = CompletionCoordinator()
    api = CompletionApi()
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    ledger = TelegramDeliveryLedger(ledger_path)
    key = preparation_completion_delivery_key(
        "application-001", "sha256:" + "b" * 64
    )
    ledger.stage_outbound(key)
    token = ledger.claim_outbound(key)
    assert token is not None
    assert ledger.mark_outbound_sending(key, token)
    if status == "sent":
        assert ledger.mark_outbound_sent(key, token)
    elif status == "uncertain":
        assert ledger.mark_outbound_uncertain(key, token)

    notifier = build_notifier(tmp_path, coordinator, api)

    assert notifier.needs_delivery("application-001") is False
    assert notifier.notify("application-001") is False
    assert api.deliveries == []


def test_resolution_notice_is_role_specific_durable_and_never_exposes_compila(
    tmp_path,
):
    coordinator = ResolutionCoordinator()
    api = ResolutionApi()

    assert build_resolution_notifier(
        tmp_path, coordinator, api
    ).notify("application-001") is True
    assert build_resolution_notifier(
        tmp_path, coordinator, api
    ).notify("application-001") is False

    assert len(api.deliveries) == 1
    summary, command = api.deliveries[0]
    assert summary.title == "Research Engineer"
    assert summary.reason == "no workflow run appeared before the deadline"
    assert command.scope.action == WorkflowAction.RETRY_PREPARATION
    assert len(coordinator.authorizations) == 1


def test_resolution_notifier_exposes_read_only_delivery_eligibility(tmp_path):
    coordinator = ResolutionCoordinator()
    api = ResolutionApi()
    notifier = build_resolution_notifier(tmp_path, coordinator, api)

    assert notifier.needs_delivery("application-001") is True
    assert notifier.notify("application-001") is True
    assert notifier.needs_delivery("application-001") is False


def test_unsafe_resolution_notice_has_no_button_or_retry_authorization(tmp_path):
    coordinator = ResolutionCoordinator(retry_safe=False)
    api = ResolutionApi()

    assert build_resolution_notifier(
        tmp_path, coordinator, api
    ).notify("application-001") is True

    summary, command = api.deliveries[0]
    assert summary.outcome == "resolution_required"
    assert command is None
    assert coordinator.authorizations == []

    coordinator.retry_safe = True
    assert build_resolution_notifier(
        tmp_path, coordinator, api
    ).notify("application-001") is True
    assert api.deliveries[1][1].scope.action == (
        WorkflowAction.RETRY_PREPARATION
    )
    assert len(coordinator.authorizations) == 1


def test_resolution_crash_after_sending_marker_never_resends(tmp_path):
    coordinator = ResolutionCoordinator()
    crashing_api = ResolutionApi(crash=True)
    notifier = build_resolution_notifier(tmp_path, coordinator, crashing_api)
    key = preparation_resolution_delivery_key(
        "application-001",
        "prepare:old-token",
        "resolution_required",
        "no workflow run appeared before the deadline",
        retry_available=True,
    )

    with pytest.raises(SystemExit):
        notifier.notify("application-001")
    assert TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    ).outbound_status(key) == "sending"

    restarted_api = ResolutionApi()
    assert build_resolution_notifier(
        tmp_path, coordinator, restarted_api
    ).notify("application-001") is False
    assert restarted_api.deliveries == []
    assert len(coordinator.authorizations) == 1


def test_expired_retry_click_reissues_one_fresh_visible_control(tmp_path):
    coordinator = ResolutionCoordinator()
    api = ResolutionApi()
    notifier = build_resolution_notifier(tmp_path, coordinator, api)
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )

    assert notifier.reissue_expired_retry(expired) is True
    assert notifier.reissue_expired_retry(expired) is False

    assert len(api.deliveries) == 1
    summary, replacement = api.deliveries[0]
    assert summary.intent_id == expired.scope.version
    assert replacement.scope.action == WorkflowAction.RETRY_PREPARATION
    assert replacement.token == "retry-token-1"
    assert coordinator.authorizations == [
        ("application-001", "42", timedelta(minutes=30))
    ]
    key = preparation_retry_reissue_delivery_key(expired)
    assert TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    ).outbound_status(key) == "sent"


@pytest.mark.parametrize("pre_send_status", ("pending", "claimed"))
def test_expired_retry_replay_recovers_only_pre_send_delivery(
    tmp_path, pre_send_status
):
    coordinator = ResolutionCoordinator()
    api = ResolutionApi()
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )
    ledger = TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    )
    key = preparation_retry_reissue_delivery_key(expired)
    ledger.stage_outbound(key)
    if pre_send_status == "claimed":
        assert ledger.claim_outbound(key) is not None

    assert build_resolution_notifier(
        tmp_path, coordinator, api
    ).reissue_expired_retry(expired) is True

    assert len(api.deliveries) == 1
    assert api.deliveries[0][1].scope.action == (
        WorkflowAction.RETRY_PREPARATION
    )
    assert coordinator.resolution_checks == 1
    assert TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    ).outbound_status(key) == "sent"


def test_same_stale_retry_click_after_presend_crash_shows_one_replacement(
    tmp_path,
):
    current = [datetime(2026, 7, 16, 10, tzinfo=timezone.utc)]
    coordinator = ResolutionCoordinator()
    delivery_api = ResolutionApi()
    notifier = build_resolution_notifier(tmp_path, coordinator, delivery_api)
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )
    update_store = TelegramUpdateStore(
        tmp_path / "updates.sqlite", now=lambda: current[0]
    )
    outer = update_store.issue_callback_authorization(
        actor_id="42",
        chat_id="99",
        route="applications",
        capability="applications",
        payload=f"app:{expired.token}",
        resume_generation=0,
        expires_at=current[0] + timedelta(seconds=1),
    )
    current[0] += timedelta(seconds=2)
    attempts = []

    def recover(execution, context):
        assert context.payload == f"app:{expired.token}"
        attempts.append(context.payload)

        def before_send():
            execution.checkpoint()
            if len(attempts) == 1:
                raise SystemExit("stopped after claim, before sending")

        notifier.reissue_expired_retry(expired, before_send=before_send)
        return "expired"

    route = CallbackRoute(
        route="applications",
        prefixes=("app:",),
        capability="applications",
        handler=lambda *_: pytest.fail("retry replay dispatched directly"),
        stale_handler=recover,
        recover_stale_replay=True,
    )
    worker = ReplayWorker()
    first = LocalWorkerTelegramRouter(
        api=CallbackApi([callback_update(80, outer.callback_data)]),
        store=update_store,
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(route,),
    )

    with pytest.raises(SystemExit, match="before sending"):
        first.consume_once(timeout=0)

    restarted_api = CallbackApi(
        [
            callback_update(81, outer.callback_data),
            callback_update(82, outer.callback_data),
        ]
    )
    restarted = LocalWorkerTelegramRouter(
        api=restarted_api,
        store=TelegramUpdateStore(
            tmp_path / "updates.sqlite", now=lambda: current[0]
        ),
        worker=worker,
        actor_id="42",
        chat_id="99",
        routes=(route,),
    )
    restarted.consume_once(timeout=0)

    assert len(delivery_api.deliveries) == 1
    assert len(attempts) == 3
    assert coordinator.resolution_checks == 3
    assert restarted_api.acknowledgements == [
        ("callback-81", "expired"),
        ("callback-82", "expired"),
    ]


@pytest.mark.parametrize("delivery_status", ("sending", "sent", "uncertain"))
def test_expired_retry_replay_never_reenters_possible_send_states(
    tmp_path, delivery_status
):
    coordinator = ResolutionCoordinator()
    api = ResolutionApi()
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )
    ledger = TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    )
    key = preparation_retry_reissue_delivery_key(expired)
    ledger.stage_outbound(key)
    claim_token = ledger.claim_outbound(key)
    assert claim_token is not None
    assert ledger.mark_outbound_sending(key, claim_token)
    if delivery_status == "sent":
        assert ledger.mark_outbound_sent(key, claim_token)
    elif delivery_status == "uncertain":
        assert ledger.mark_outbound_uncertain(key, claim_token)

    assert build_resolution_notifier(
        tmp_path, coordinator, api
    ).reissue_expired_retry(expired) is False

    assert api.deliveries == []
    assert coordinator.authorizations == []
    assert coordinator.resolution_checks == 1


def test_expired_retry_reissue_repeats_safety_check_and_fails_closed(tmp_path):
    coordinator = ResolutionCoordinator(retry_safe=False)
    api = ResolutionApi()
    notifier = build_resolution_notifier(tmp_path, coordinator, api)
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )

    assert notifier.reissue_expired_retry(expired) is False

    assert api.deliveries == []
    assert coordinator.authorizations == []
    assert coordinator.resolution_checks == 1
    assert TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    ).outbound_status(preparation_retry_reissue_delivery_key(expired)) is None


def test_expired_retry_reissue_possible_send_is_never_repeated(tmp_path):
    coordinator = ResolutionCoordinator()
    api = ResolutionApi(crash=True)
    notifier = build_resolution_notifier(tmp_path, coordinator, api)
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )

    with pytest.raises(SystemExit):
        notifier.reissue_expired_retry(expired)

    key = preparation_retry_reissue_delivery_key(expired)
    assert TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    ).outbound_status(key) == "sending"
    restarted_api = ResolutionApi()
    restarted = build_resolution_notifier(
        tmp_path, coordinator, restarted_api
    )
    assert restarted.reissue_expired_retry(expired) is False
    assert restarted_api.deliveries == []
    assert len(coordinator.authorizations) == 1


def test_expired_retry_reissue_ambiguous_send_becomes_uncertain(tmp_path):
    coordinator = ResolutionCoordinator()

    class AmbiguousApi(ResolutionApi):
        def send_preparation_resolution(self, summary, command):
            self.deliveries.append((summary, command))
            raise RuntimeError("response lost after possible send")

    api = AmbiguousApi()
    notifier = build_resolution_notifier(tmp_path, coordinator, api)
    expired = ActionCommand(
        token="expired-retry-token",
        scope=AuthorizationScope(
            application_id="application-001",
            action=WorkflowAction.RETRY_PREPARATION,
            version="prepare:old-token",
        ),
    )

    with pytest.raises(RuntimeError, match="response lost"):
        notifier.reissue_expired_retry(expired)

    key = preparation_retry_reissue_delivery_key(expired)
    assert TelegramDeliveryLedger(
        tmp_path / "telegram-deliveries.sqlite"
    ).outbound_status(key) == "uncertain"
    restarted_api = ResolutionApi()
    assert build_resolution_notifier(
        tmp_path, coordinator, restarted_api
    ).reissue_expired_retry(expired) is False
    assert restarted_api.deliveries == []


def test_crash_after_stage_before_claim_resumes_same_completion_delivery(tmp_path):
    coordinator = CompletionCoordinator()
    api = CompletionApi()
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    key = preparation_completion_delivery_key(
        "application-001", "sha256:" + "b" * 64
    )
    TelegramDeliveryLedger(ledger_path).stage_outbound(key)

    restarted = build_notifier(tmp_path, coordinator, api)

    assert restarted.notify("application-001") is True
    assert len(api.deliveries) == 1
    assert TelegramDeliveryLedger(ledger_path).outbound_status(key) == "sent"


def test_crash_after_claim_before_send_recovers_and_delivers_on_restart(
    tmp_path,
):
    coordinator = CompletionCoordinator()
    api = CompletionApi()
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    ledger = TelegramDeliveryLedger(ledger_path)
    key = preparation_completion_delivery_key(
        "application-001", "sha256:" + "b" * 64
    )
    ledger.stage_outbound(key)
    assert ledger.claim_outbound(key) is not None

    restarted = build_notifier(tmp_path, coordinator, api)

    assert restarted.notify("application-001") is True
    assert len(coordinator.authorizations) == 1
    assert len(api.deliveries) == 1
    assert TelegramDeliveryLedger(ledger_path).outbound_status(key) == "sent"


def test_crash_after_sending_marker_never_regenerates_callback_or_resends(
    tmp_path,
):
    coordinator = CompletionCoordinator()
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    key = preparation_completion_delivery_key(
        "application-001", "sha256:" + "b" * 64
    )

    class CrashAtSendBoundaryApi:
        calls = 0

        def send_preparation_completed(self, summary, command):
            del summary, command
            self.calls += 1
            assert (
                TelegramDeliveryLedger(ledger_path).outbound_status(key)
                == "sending"
            )
            raise SystemExit("worker stopped after durable sending marker")

    crashing_api = CrashAtSendBoundaryApi()
    with pytest.raises(SystemExit):
        build_notifier(tmp_path, coordinator, crashing_api).notify(
            "application-001"
        )

    assert TelegramDeliveryLedger(ledger_path).outbound_status(key) == "sending"
    api = CompletionApi()
    restarted = build_notifier(tmp_path, coordinator, api)

    assert restarted.notify("application-001") is False
    assert len(coordinator.authorizations) == 1
    assert crashing_api.calls == 1
    assert api.deliveries == []


def test_possible_send_without_ack_becomes_uncertain_and_never_resends(tmp_path):
    coordinator = CompletionCoordinator()
    api = CompletionApi(fail_after_send=True)
    notifier = build_notifier(tmp_path, coordinator, api)
    key = preparation_completion_delivery_key(
        "application-001", "sha256:" + "b" * 64
    )

    try:
        notifier.notify("application-001")
    except RuntimeError:
        pass
    else:
        raise AssertionError("ambiguous Telegram result was swallowed")

    restarted = build_notifier(tmp_path, coordinator, api)
    assert restarted.notify("application-001") is False
    assert len(api.deliveries) == 1
    assert len(coordinator.authorizations) == 1
    assert (
        TelegramDeliveryLedger(
            tmp_path / "telegram-deliveries.sqlite"
        ).outbound_status(key)
        == "uncertain"
    )


def test_concrete_api_exposes_compila_on_role_specific_completion_message():
    class Response:
        ok = True

        def json(self):
            return {"ok": True, "result": {}}

    class Http:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    http = Http()
    api = TelegramApplicationApi(
        token="secret",
        chat_id="99",
        user_id="42",
        callback_encoder=lambda command: f"encoded:{command.token}",
        http=http,
    )
    coordinator = CompletionCoordinator()
    command = coordinator.issue_authorization(
        "application-001",
        WorkflowAction.FILL,
        actor="42",
        ttl=timedelta(minutes=30),
    )
    application = coordinator.application

    api.send_preparation_completed(
        SimpleNamespace(
            application_id=application.application_id,
            company=application.opportunity["company"],
            title=application.opportunity["title"],
            location=application.opportunity["location"],
            official_vacancy_version=application.official_vacancy.version,
            artifact_version=application.artifacts.version,
        ),
        command,
    )

    payload = http.posts[0][1]["json"]
    assert "CV completo: Research Engineer" in payload["text"]
    assert "Azienda: Example AI" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"] == [
        [
            {
                "text": "Compila",
                "callback_data": "encoded:fill-token-1",
            }
        ]
    ]


def test_concrete_resolution_message_never_claims_ready_or_exposes_compila():
    class Response:
        ok = True

        def json(self):
            return {"ok": True, "result": {}}

    class Http:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    http = Http()
    api = TelegramApplicationApi(
        token="secret",
        chat_id="99",
        user_id="42",
        callback_encoder=lambda command: f"encoded:{command.token}",
        http=http,
    )
    coordinator = ResolutionCoordinator()
    command = coordinator.issue_preparation_retry_authorization(
        "application-001",
        actor="42",
        ttl=timedelta(minutes=30),
    )
    api.send_preparation_resolution(
        SimpleNamespace(
            application_id="application-001",
            company="Example AI",
            title="Research Engineer",
            location="Zurich",
            official_vacancy_version="sha256:" + "a" * 64,
            intent_id="prepare:old-token",
            outcome="failed",
            reason="dispatch rejected",
        ),
        command,
    )

    payload = http.posts[0][1]["json"]
    assert "Preparazione non completata: Research Engineer" in payload["text"]
    assert "CV completo" not in payload["text"]
    assert "Compila non è disponibile" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == (
        "Riprova preparazione"
    )


def test_completion_scan_requires_installed_artifacts_to_still_verify():
    artifacts = SimpleNamespace(version="sha256:" + "b" * 64)
    ready = SimpleNamespace(
        application_id="application-001",
        lifecycle_state=LifecycleState.CV_READY,
        artifacts=artifacts,
        official_vacancy=SimpleNamespace(version="sha256:" + "a" * 64),
        next_action=WorkflowAction.FILL,
        operation_intents=(
            SimpleNamespace(
                action=WorkflowAction.PREPARE,
                completed_at="2026-07-24T10:00:00+00:00",
            ),
        ),
    )
    verification = {"valid": False}
    coordinator = ApplicationWorkflowCoordinator(
        store=SimpleNamespace(list=lambda: (ready,)),
        tailoring=SimpleNamespace(
            verify_artifacts=lambda candidate: (
                candidate is artifacts and verification["valid"]
            )
        ),
        ats=SimpleNamespace(),
        report_writer=SimpleNamespace(),
        official_vacancies=SimpleNamespace(),
        clock=SimpleNamespace(),
    )

    assert coordinator.preparation_completion_ids() == ()
    verification["valid"] = True
    assert coordinator.preparation_completion_ids() == ("application-001",)
