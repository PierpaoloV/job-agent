from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hosted_preparation_completion import (  # noqa: E402
    HostedApplicationStateStore,
    arm_remote_preparation_completion,
    dispatch_remote_preparation_completion,
)
from notify_telegram import TelegramSendRejected  # noqa: E402
from telegram_delivery import TelegramDeliveryLedger  # noqa: E402


APPLICATION_ID = "approved-b0a227c91dd404d4"
VACANCY_VERSION = (
    "sha256:a1d8a8d0dc9191b386726710592e5a5a145741835b3a6808457c48ccb2c84bff"
)
RUN_URL = "https://github.com/PierpaoloV/job-agent/actions/runs/32252719094"
PACKAGE_HASH = "sha256:" + "c" * 64


def test_remote_preparation_completion_is_delivered_once(tmp_path):
    delivered = []
    ledger_path = tmp_path / "telegram-deliveries.sqlite"

    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(
            tmp_path / "hosted-application-state"
        ),
    )
    assert claim_token is not None
    dispatch_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        claim_token=claim_token,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(
            tmp_path / "hosted-application-state"
        ),
        message_sender=delivered.append,
    )
    assert arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(
            tmp_path / "hosted-application-state"
        ),
    ) is None

    assert len(delivered) == 1
    assert delivered[0].text == (
        "✅ <b>CV e lettera pronti</b>\n\n"
        "La preparazione remota è conclusa per "
        "<code>approved-b0a227c91dd404d4</code>.\n"
        '<a href="https://github.com/PierpaoloV/job-agent/actions/runs/'
        '32252719094">Apri la run e il pacchetto cifrato</a>.\n\n'
        "Nessun modulo ATS è stato compilato o inviato."
    )
    assert delivered[0].reply_markup is None


def test_remote_completion_rejects_wrong_claim_before_telegram(tmp_path):
    delivered = []
    ledger = TelegramDeliveryLedger(tmp_path / "telegram-deliveries.sqlite")
    assert arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=ledger,
        application_states=HostedApplicationStateStore(
            tmp_path / "hosted-application-state"
        ),
    ) is not None

    with pytest.raises(RuntimeError, match="claim is not active"):
        dispatch_remote_preparation_completion(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url=RUN_URL,
            claim_token="wrong-claim-token",
            ledger=ledger,
            application_states=HostedApplicationStateStore(
                tmp_path / "hosted-application-state"
            ),
            message_sender=delivered.append,
        )

    assert delivered == []


def test_remote_completion_persists_cv_ready_with_exact_package(tmp_path):
    state_root = tmp_path / "hosted-application-state"

    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(tmp_path / "telegram-deliveries.sqlite"),
        application_states=HostedApplicationStateStore(state_root),
    )

    assert claim_token is not None
    restored = HostedApplicationStateStore(state_root).load(APPLICATION_ID)
    assert restored.lifecycle_state == "CV pronto"
    assert restored.official_vacancy_version == VACANCY_VERSION
    assert restored.package_hash == PACKAGE_HASH
    assert restored.run_url == RUN_URL
    assert [event["state"] for event in restored.history] == [
        "approvata",
        "CV pronto",
    ]


def test_remote_completion_rejects_mismatched_run_before_telegram(tmp_path):
    delivered = []
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    state_root = tmp_path / "hosted-application-state"
    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    )
    assert claim_token is not None

    with pytest.raises(RuntimeError, match="state identity does not match"):
        dispatch_remote_preparation_completion(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url="https://github.com/PierpaoloV/job-agent/actions/runs/32252719095",
            claim_token=claim_token,
            ledger=TelegramDeliveryLedger(ledger_path),
            application_states=HostedApplicationStateStore(state_root),
            message_sender=delivered.append,
        )

    assert delivered == []


def test_uncertain_remote_completion_is_not_reclaimed_after_restart(tmp_path):
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    state_root = tmp_path / "hosted-application-state"
    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    )
    assert claim_token is not None

    def uncertain_sender(_message):
        raise RuntimeError("transport outcome unknown")

    with pytest.raises(RuntimeError, match="outcome unknown"):
        dispatch_remote_preparation_completion(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url=RUN_URL,
            claim_token=claim_token,
            ledger=TelegramDeliveryLedger(ledger_path),
            application_states=HostedApplicationStateStore(state_root),
            message_sender=uncertain_sender,
        )

    assert arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash="sha256:" + "d" * 64,
        run_url="https://github.com/PierpaoloV/job-agent/actions/runs/32252719095",
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    ) is None
    assert HostedApplicationStateStore(state_root).load(
        APPLICATION_ID
    ).package_hash == PACKAGE_HASH


def test_definite_rejection_is_retryable_after_restart(tmp_path):
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    state_root = tmp_path / "hosted-application-state"
    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    )
    assert claim_token is not None

    with pytest.raises(TelegramSendRejected):
        dispatch_remote_preparation_completion(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url=RUN_URL,
            claim_token=claim_token,
            ledger=TelegramDeliveryLedger(ledger_path),
            application_states=HostedApplicationStateStore(state_root),
            message_sender=lambda _message: (_ for _ in ()).throw(
                TelegramSendRejected("definite rejection")
            ),
        )

    replacement_hash = "sha256:" + "d" * 64
    replacement_url = (
        "https://github.com/PierpaoloV/job-agent/actions/runs/32252719095"
    )
    retry_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=replacement_hash,
        run_url=replacement_url,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    )
    assert retry_token is not None
    assert retry_token != claim_token
    restored = HostedApplicationStateStore(state_root).load(APPLICATION_ID)
    assert restored.package_hash == replacement_hash
    assert restored.run_url == replacement_url


def test_missing_post_send_ack_remains_unreclaimable(tmp_path, monkeypatch):
    ledger_path = tmp_path / "telegram-deliveries.sqlite"
    state_root = tmp_path / "hosted-application-state"
    ledger = TelegramDeliveryLedger(ledger_path)
    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=ledger,
        application_states=HostedApplicationStateStore(state_root),
    )
    assert claim_token is not None
    delivered = []
    monkeypatch.setattr(ledger, "mark_outbound_sent", lambda *_args: False)

    with pytest.raises(RuntimeError, match="acknowledgement was not persisted"):
        dispatch_remote_preparation_completion(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url=RUN_URL,
            claim_token=claim_token,
            ledger=ledger,
            application_states=HostedApplicationStateStore(state_root),
            message_sender=delivered.append,
        )

    assert len(delivered) == 1
    assert arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    ) is None
