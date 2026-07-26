from pathlib import Path
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
