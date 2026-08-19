from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hosted_preparation_completion import (  # noqa: E402
    arm_remote_preparation_completion,
    dispatch_remote_preparation_completion,
)
from telegram_delivery import TelegramDeliveryLedger  # noqa: E402


APPLICATION_ID = "approved-b0a227c91dd404d4"
VACANCY_VERSION = (
    "sha256:a1d8a8d0dc9191b386726710592e5a5a145741835b3a6808457c48ccb2c84bff"
)
RUN_URL = "https://github.com/PierpaoloV/job-agent/actions/runs/32252719094"


def test_remote_preparation_completion_is_delivered_once(tmp_path):
    delivered = []
    ledger_path = tmp_path / "telegram-deliveries.sqlite"

    claim_token = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
    )
    assert claim_token is not None
    dispatch_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        run_url=RUN_URL,
        claim_token=claim_token,
        ledger=TelegramDeliveryLedger(ledger_path),
        message_sender=delivered.append,
    )
    assert arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
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
        run_url=RUN_URL,
        ledger=ledger,
    ) is not None

    with pytest.raises(RuntimeError, match="claim is not active"):
        dispatch_remote_preparation_completion(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            run_url=RUN_URL,
            claim_token="wrong-claim-token",
            ledger=ledger,
            message_sender=delivered.append,
        )

    assert delivered == []
