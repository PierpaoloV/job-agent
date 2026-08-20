from pathlib import Path
from hashlib import sha256
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import notify_telegram


class FakeTelegramResponse:
    def __init__(self, body, *, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = "redacted fake response"

    def json(self):
        return self._body


def test_send_requires_telegram_ack_and_returns_receipt(monkeypatch):
    monkeypatch.setattr(
        notify_telegram.requests,
        "post",
        lambda *args, **kwargs: FakeTelegramResponse({
            "ok": True,
            "result": {
                "message_id": 123,
                "chat": {"id": 42},
            },
        }),
    )

    receipt = notify_telegram._send(
        "test-token",
        "42",
        "diagnostic",
    )

    assert receipt.message_id == 123
    assert receipt.chat_id == "42"


def test_http_200_without_telegram_ok_is_not_a_delivery(monkeypatch):
    monkeypatch.setattr(
        notify_telegram.requests,
        "post",
        lambda *args, **kwargs: FakeTelegramResponse({
            "ok": False,
            "error_code": 400,
            "description": "message rejected",
        }),
    )

    with pytest.raises(RuntimeError, match="Telegram send rejected"):
        notify_telegram._send("test-token", "42", "diagnostic")


def test_digest_overflow_adds_a_batch_scoped_inline_action(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(
        notify_telegram,
        "_send",
        lambda token, chat_id, text, parse_mode="HTML", reply_markup=None: sent.append(
            (text, reply_markup)
        ),
    )

    notify_telegram.send_digest(
        [{"stable_id": "one", "title": "Role", "company": "Example"}],
        overflow_count=4,
        overflow_callback_data="discovery-overflow:digest-1-abc",
    )

    assert sent[-1][1] == {
        "inline_keyboard": [[{
            "text": "Mostra altre (4)",
            "callback_data": "discovery-overflow:digest-1-abc",
        }]]
    }


def test_protected_pdf_review_sends_both_documents_as_one_protected_group(
    monkeypatch, tmp_path
):
    cv = tmp_path / "cv.pdf"
    letter = tmp_path / "cover-letter.pdf"
    cv.write_bytes(b"%PDF-1.4\nreview cv")
    letter.write_bytes(b"%PDF-1.4\nreview letter")
    calls = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeTelegramResponse(
            {
                "ok": True,
                "result": [
                    {"message_id": 701, "chat": {"id": 42}},
                    {"message_id": 702, "chat": {"id": 42}},
                ],
            }
        )

    monkeypatch.setattr(notify_telegram.requests, "post", post)

    receipts = notify_telegram.send_protected_document_group(
        (
            notify_telegram.TelegramDocument(
                path=cv,
                filename="CV.pdf",
                caption="CV su misura",
                sha256="sha256:" + sha256(cv.read_bytes()).hexdigest(),
            ),
            notify_telegram.TelegramDocument(
                path=letter,
                filename="Lettera.pdf",
                caption="Lettera di presentazione",
                sha256="sha256:" + sha256(letter.read_bytes()).hexdigest(),
            ),
        )
    )

    assert [receipt.message_id for receipt in receipts] == [701, 702]
    url, request = calls[0]
    assert url == "https://api.telegram.org/bottest-token/sendMediaGroup"
    assert request["data"]["chat_id"] == "42"
    assert request["data"]["protect_content"] == "true"
    assert request["data"]["media"] == (
        '[{"type":"document","media":"attach://document_0",'
        '"caption":"CV su misura"},{"type":"document",'
        '"media":"attach://document_1",'
        '"caption":"Lettera di presentazione"}]'
    )
    assert request["files"]["document_0"][0] == "CV.pdf"
    assert request["files"]["document_1"][0] == "Lettera.pdf"
    assert request["timeout"] == 30
