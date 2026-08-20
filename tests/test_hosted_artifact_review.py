from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from application_domain import PreparedArtifacts  # noqa: E402
from hosted_artifact_review import (  # noqa: E402
    GatewayArtifactReviewPublisher,
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
            return Response({"status": "pending"})
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
            cv_hash="sha256:" + __import__("hashlib").sha256(cv.read_bytes()).hexdigest(),
            cover_letter_hash=(
                "sha256:"
                + __import__("hashlib").sha256(letter.read_bytes()).hexdigest()
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
    bind_url, bind = session.calls[1]
    assert bind_url.endswith("/v1/artifact-reviews/review-token-1/messages")
    assert bind["json"] == {
        "document_message_ids": [701, 702],
        "control_message_id": 703,
    }
