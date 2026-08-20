from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hosted_preparation_completion import (  # noqa: E402
    HostedArtifactReviewDecisionStore,
    HostedApplicationStateStore,
    arm_remote_preparation_completion,
    dispatch_remote_preparation_completion,
    record_hosted_artifact_review_decision,
)
from application_domain import PreparedArtifacts  # noqa: E402
from notify_telegram import TelegramSendRejected  # noqa: E402
from telegram_delivery import TelegramDeliveryLedger  # noqa: E402


APPLICATION_ID = "approved-b0a227c91dd404d4"
VACANCY_VERSION = (
    "sha256:a1d8a8d0dc9191b386726710592e5a5a145741835b3a6808457c48ccb2c84bff"
)
RUN_URL = "https://github.com/PierpaoloV/job-agent/actions/runs/32252719094"
PACKAGE_HASH = "sha256:" + "c" * 64


def prepared_artifacts(tmp_path):
    cv = tmp_path / "cv.pdf"
    letter = tmp_path / "cover-letter.pdf"
    cv.write_bytes(b"%PDF-1.4\ncv")
    letter.write_bytes(b"%PDF-1.4\nletter")
    return PreparedArtifacts(
        version="sha256:" + "e" * 64,
        cv_path=str(cv),
        cover_letter_path=str(letter),
        cv_hash="sha256:" + __import__("hashlib").sha256(cv.read_bytes()).hexdigest(),
        cover_letter_hash=(
            "sha256:" + __import__("hashlib").sha256(letter.read_bytes()).hexdigest()
        ),
    )


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
        artifacts=prepared_artifacts(tmp_path),
        review_publisher=lambda **kwargs: delivered.append(kwargs),
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
    assert delivered[0]["application_id"] == APPLICATION_ID
    assert delivered[0]["official_vacancy_version"] == VACANCY_VERSION
    assert delivered[0]["package_hash"] == PACKAGE_HASH
    assert delivered[0]["run_url"] == RUN_URL
    assert delivered[0]["artifacts"].cv_path.endswith("cv.pdf")


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
            artifacts=prepared_artifacts(tmp_path),
            review_publisher=lambda **kwargs: delivered.append(kwargs),
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
            artifacts=prepared_artifacts(tmp_path),
            review_publisher=lambda **kwargs: delivered.append(kwargs),
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

    def uncertain_sender(**_kwargs):
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
            artifacts=prepared_artifacts(tmp_path),
            review_publisher=uncertain_sender,
        )

    assert arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=TelegramDeliveryLedger(ledger_path),
        application_states=HostedApplicationStateStore(state_root),
    ) is None
    assert HostedApplicationStateStore(state_root).load(
        APPLICATION_ID
    ).package_hash == PACKAGE_HASH


def test_regenerated_package_gets_a_distinct_review_delivery(tmp_path):
    ledger = TelegramDeliveryLedger(tmp_path / "telegram-deliveries.sqlite")
    states = HostedApplicationStateStore(tmp_path / "hosted-application-state")
    first = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        ledger=ledger,
        application_states=states,
    )

    second_hash = "sha256:" + "d" * 64
    second = arm_remote_preparation_completion(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=second_hash,
        run_url="https://github.com/PierpaoloV/job-agent/actions/runs/32252719095",
        ledger=ledger,
        application_states=states,
    )

    assert first is not None
    assert second is not None
    assert second != first
    assert states.load(APPLICATION_ID).package_hash == second_hash


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
            artifacts=prepared_artifacts(tmp_path),
            review_publisher=lambda **_kwargs: (_ for _ in ()).throw(
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
            artifacts=prepared_artifacts(tmp_path),
            review_publisher=lambda **kwargs: delivered.append(kwargs),
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


def test_artifact_approval_is_recorded_only_for_exact_published_package(tmp_path):
    states = HostedApplicationStateStore(tmp_path / "hosted-application-state")
    states.record_cv_ready(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
    )

    decision = record_hosted_artifact_review_decision(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        review_id="review-token-123",
        action="approve_artifacts",
        actor_id="42",
        chat_id="42",
        expected_actor_id="42",
        expected_chat_id="42",
        application_states=states,
        decisions=HostedArtifactReviewDecisionStore(
            tmp_path / "hosted-artifact-review-decisions"
        ),
    )

    assert decision.status == "approved"
    assert decision.package_hash == PACKAGE_HASH
    assert decision.review_id == "review-token-123"


def test_artifact_decision_rejects_wrong_package_before_state_change(tmp_path):
    states = HostedApplicationStateStore(tmp_path / "hosted-application-state")
    states.record_cv_ready(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
    )
    decisions = HostedArtifactReviewDecisionStore(
        tmp_path / "hosted-artifact-review-decisions"
    )

    with pytest.raises(RuntimeError, match="identity does not match"):
        record_hosted_artifact_review_decision(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash="sha256:" + "d" * 64,
            review_id="review-token-123",
            action="regenerate_artifacts",
            actor_id="42",
            chat_id="42",
            expected_actor_id="42",
            expected_chat_id="42",
            application_states=states,
            decisions=decisions,
        )

    assert tuple((tmp_path / "hosted-artifact-review-decisions").glob("*")) == ()
