from pathlib import Path
import hashlib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from application_domain import PreparedArtifacts  # noqa: E402
from hosted_artifact_review import (  # noqa: E402
    GatewayArtifactReviewPublisher,
    acknowledge_gateway_artifact_review,
    recover_gateway_artifact_review_dispatch,
)
from notify_telegram import TelegramReceipt  # noqa: E402


APPLICATION_ID = "approved-b0a227c91dd404d4"
VACANCY_VERSION = "sha256:" + "a" * 64
PACKAGE_HASH = "sha256:" + "b" * 64
RUN_URL = "https://github.com/PierpaoloV/job-agent/actions/runs/32362119028"


class Response:
    def __init__(self, body, *, status=200):
        self.body = body
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self):
        return self.body


class GatewaySession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/v1/review-authorizations"):
            return Response(
                {
                    "review_id": "review-token-1",
                    "expires_at": "2026-08-21T10:00:00.000Z",
                    "buttons": [
                        {
                            "text": "✅ Approva",
                            "callback_data": "jar1:review-token-2",
                        },
                        {
                            "text": "🔄 Rigenera",
                            "callback_data": "jar1:review-token-3",
                        },
                    ],
                }
            )
        if url.endswith("/v1/artifact-reviews/review-token-1/messages"):
            return Response(
                {
                    "status": (
                        "pending"
                        if "control_message_id" in kwargs["json"]
                        else "documents_sent"
                    )
                }
            )
        if url.endswith(
            "/v1/artifact-reviews/review-token-1/publication-cleanup"
        ):
            return Response({"status": "expiry_cleanup_uncertain"})
        raise AssertionError(f"unexpected URL: {url}")


def test_review_publisher_sends_protected_pdfs_then_binds_exact_receipts(tmp_path):
    cv = tmp_path / "cv.pdf"
    letter = tmp_path / "cover-letter.pdf"
    cv.write_bytes(b"%PDF-1.4\ncv")
    letter.write_bytes(b"%PDF-1.4\nletter")
    session = GatewaySession()
    documents = []
    controls = []

    receipt = GatewayArtifactReviewPublisher(
        endpoint="https://gateway.example",
        internal_token="internal-secret",
        actor_id="42",
        chat_id="42",
        session=session,
        document_sender=lambda value: (
            documents.extend(value)
            or (
                TelegramReceipt(message_id=701, chat_id="42"),
                TelegramReceipt(message_id=702, chat_id="42"),
            )
        ),
        control_sender=lambda message: (
            controls.append(message)
            or TelegramReceipt(message_id=703, chat_id="42")
        ),
    ).publish(
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        run_url=RUN_URL,
        artifacts=PreparedArtifacts(
            version="sha256:" + "c" * 64,
            cv_path=str(cv),
            cover_letter_path=str(letter),
            cv_hash="sha256:" + hashlib.sha256(cv.read_bytes()).hexdigest(),
            cover_letter_hash=(
                "sha256:"
                + hashlib.sha256(letter.read_bytes()).hexdigest()
            ),
        ),
    )

    assert receipt.review_id == "review-token-1"
    assert receipt.document_message_ids == (701, 702)
    assert receipt.control_message_id == 703
    assert receipt.expires_at == "2026-08-21T10:00:00.000Z"
    assert [item.filename for item in documents] == [
        "CV-approved-b0a227c91dd404d4.pdf",
        "Lettera-approved-b0a227c91dd404d4.pdf",
    ]
    assert controls[0].reply_markup == {
        "inline_keyboard": [[
            {"text": "✅ Approva", "callback_data": "jar1:review-token-2"},
            {"text": "🔄 Rigenera", "callback_data": "jar1:review-token-3"},
        ]]
    }
    assert "scadono automaticamente" in controls[0].text

    authorize_url, authorize = session.calls[0]
    assert authorize_url == "https://gateway.example/v1/review-authorizations"
    assert authorize["json"] == {
        "event_id": (
            "review:approved-b0a227c91dd404d4:"
            "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ),
        "application_id": APPLICATION_ID,
        "official_vacancy_version": VACANCY_VERSION,
        "package_hash": PACKAGE_HASH,
        "actor_id": "42",
        "chat_id": "42",
    }
    document_bind_url, document_bind = session.calls[1]
    assert document_bind_url.endswith(
        "/v1/artifact-reviews/review-token-1/messages"
    )
    assert document_bind["json"] == {
        "document_message_ids": [701, 702],
    }
    control_bind_url, control_bind = session.calls[2]
    assert control_bind_url.endswith(
        "/v1/artifact-reviews/review-token-1/messages"
    )
    assert control_bind["json"] == {
        "document_message_ids": [701, 702],
        "control_message_id": 703,
    }


def test_gateway_acknowledgement_happens_only_after_authoritative_state():
    calls = []

    class AckSession:
        @staticmethod
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response({"status": "approved"})

    status = acknowledge_gateway_artifact_review(
        endpoint="https://gateway.example",
        internal_token="internal-secret",
        review_id="review-token-1",
        action="approve_artifacts",
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        session=AckSession(),
    )

    assert status == "approved"
    assert calls[0][0].endswith(
        "/v1/artifact-reviews/review-token-1/decision-ack"
    )
    assert calls[0][1]["json"] == {
        "action": "approve_artifacts",
        "application_id": APPLICATION_ID,
        "official_vacancy_version": VACANCY_VERSION,
        "package_hash": PACKAGE_HASH,
    }


def test_dispatch_recovery_requires_confirmed_absence_of_a_github_run():
    class RecoverySession:
        @staticmethod
        def post(_url, **_kwargs):
            return Response({"status": "dispatch_accepted"})

    with pytest.raises(ValueError, match="explicitly confirmed"):
        recover_gateway_artifact_review_dispatch(
            endpoint="https://gateway.example",
            internal_token="internal-secret",
            review_id="review-token-1",
            action="approve_artifacts",
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            confirmed_absent=False,
            confirmed_failed=False,
            session=RecoverySession(),
        )

    assert recover_gateway_artifact_review_dispatch(
        endpoint="https://gateway.example",
        internal_token="internal-secret",
        review_id="review-token-1",
        action="approve_artifacts",
        application_id=APPLICATION_ID,
        official_vacancy_version=VACANCY_VERSION,
        package_hash=PACKAGE_HASH,
        confirmed_absent=True,
        confirmed_failed=False,
        session=RecoverySession(),
    ) == "dispatch_accepted"


def test_failed_control_binding_immediately_deletes_all_acknowledged_messages(
    tmp_path,
):
    cv = tmp_path / "cv.pdf"
    letter = tmp_path / "cover-letter.pdf"
    cv.write_bytes(b"%PDF-1.4\ncv")
    letter.write_bytes(b"%PDF-1.4\nletter")
    session = GatewaySession()
    original_post = session.post

    def fail_control_bind(url, **kwargs):
        if url.endswith("/messages") and "control_message_id" in kwargs["json"]:
            return Response({"error": "bind failed"}, status=503)
        return original_post(url, **kwargs)

    session.post = fail_control_bind
    deleted = []
    publisher = GatewayArtifactReviewPublisher(
        endpoint="https://gateway.example",
        internal_token="internal-secret",
        actor_id="42",
        chat_id="42",
        session=session,
        document_sender=lambda _documents: (
            TelegramReceipt(message_id=701, chat_id="42"),
            TelegramReceipt(message_id=702, chat_id="42"),
        ),
        control_sender=lambda _message: TelegramReceipt(
            message_id=703, chat_id="42"
        ),
        message_deleter=lambda receipts: deleted.extend(receipts),
    )

    with pytest.raises(Exception, match="rolled back"):
        publisher.publish(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url=RUN_URL,
            artifacts=PreparedArtifacts(
                version="sha256:" + "c" * 64,
                cv_path=str(cv),
                cover_letter_path=str(letter),
                cv_hash="sha256:" + hashlib.sha256(cv.read_bytes()).hexdigest(),
                cover_letter_hash=(
                    "sha256:" + hashlib.sha256(letter.read_bytes()).hexdigest()
                ),
            ),
        )

    assert [receipt.message_id for receipt in deleted] == [701, 702, 703]


def test_uncertain_compensating_delete_leaves_durable_cleanup_receipts(tmp_path):
    cv = tmp_path / "cv.pdf"
    letter = tmp_path / "cover-letter.pdf"
    cv.write_bytes(b"%PDF-1.4\ncv")
    letter.write_bytes(b"%PDF-1.4\nletter")
    session = GatewaySession()
    original_post = session.post

    def fail_control_bind(url, **kwargs):
        if url.endswith("/messages") and "control_message_id" in kwargs["json"]:
            return Response({"error": "bind failed"}, status=503)
        return original_post(url, **kwargs)

    session.post = fail_control_bind
    publisher = GatewayArtifactReviewPublisher(
        endpoint="https://gateway.example",
        internal_token="internal-secret",
        actor_id="42",
        chat_id="42",
        session=session,
        document_sender=lambda _documents: (
            TelegramReceipt(message_id=701, chat_id="42"),
            TelegramReceipt(message_id=702, chat_id="42"),
        ),
        control_sender=lambda _message: TelegramReceipt(
            message_id=703, chat_id="42"
        ),
        message_deleter=lambda _receipts: (_ for _ in ()).throw(
            RuntimeError("delete outcome unknown")
        ),
    )

    with pytest.raises(Exception, match="durably scheduled"):
        publisher.publish(
            application_id=APPLICATION_ID,
            official_vacancy_version=VACANCY_VERSION,
            package_hash=PACKAGE_HASH,
            run_url=RUN_URL,
            artifacts=PreparedArtifacts(
                version="sha256:" + "c" * 64,
                cv_path=str(cv),
                cover_letter_path=str(letter),
                cv_hash="sha256:" + hashlib.sha256(cv.read_bytes()).hexdigest(),
                cover_letter_hash=(
                    "sha256:" + hashlib.sha256(letter.read_bytes()).hexdigest()
                ),
            ),
        )

    cleanup_call = next(
        call for call in session.calls if call[0].endswith("/publication-cleanup")
    )
    assert cleanup_call[1]["json"] == {
        "document_message_ids": [701, 702],
        "control_message_id": 703,
    }
