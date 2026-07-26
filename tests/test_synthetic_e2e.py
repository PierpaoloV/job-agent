from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from synthetic_e2e import (  # noqa: E402
    SyntheticApplicationJourney,
    SyntheticTelegramSession,
)
from synthetic_e2e_live import (  # noqa: E402
    CurlTelegramBotApi,
    SyntheticTestBotConfig,
    run_live_synthetic_e2e,
)
import synthetic_e2e_live  # noqa: E402
from application_domain import OperationalStatus  # noqa: E402
from application_storage import JsonApplicationStore  # noqa: E402


def _button(message, label: str) -> str:
    return next(
        button.callback_data
        for button in message.buttons
        if button.label == label
    )


def test_real_application_workflow_reaches_one_verified_fake_submission(tmp_path):
    journey = SyntheticApplicationJourney.create(tmp_path)

    proposed = journey.start()
    assert proposed.title == "Synthetic AI Research Engineer"
    assert [button.label for button in proposed.buttons] == [
        "👍",
        "👎",
        "Dimmi di più",
    ]

    prepared = journey.handle(_button(proposed, "👍"))
    assert prepared.lifecycle_state == "CV pronto"
    assert [button.label for button in prepared.buttons] == ["Compila"]
    assert {path.name for path in prepared.documents} == {
        "curriculum-vitae.pdf",
        "cover-letter.pdf",
    }
    assert all(path.read_bytes().startswith(b"%PDF-") for path in prepared.documents)
    assert journey.fake_ats_status()["submit_count"] == 0

    filled = journey.handle(_button(prepared, "Compila"))
    assert filled.lifecycle_state == "pronta da inviare"
    assert [button.label for button in filled.buttons] == ["Invia"]
    assert "Synthetic answer — not for real use" in filled.text
    assert journey.fake_ats_status()["submit_count"] == 0

    submit_callback = _button(filled, "Invia")
    submitted = journey.handle(submit_callback)
    assert submitted.lifecycle_state == "inviata"
    assert submitted.buttons == ()
    assert submitted.confirmation_id.startswith("FAKE-ATS-")

    status = journey.fake_ats_status()
    assert status["submit_count"] == 1
    assert status["state"] == "submitted"
    assert Path(submitted.report_path).is_file()
    report = Path(submitted.report_path).read_text(encoding="utf-8")
    assert "Synthetic application report" in report
    assert submitted.confirmation_id in report

    replay = journey.handle(submit_callback)
    assert replay.lifecycle_state == "inviata"
    assert "già elaborata" in replay.text
    assert journey.fake_ats_status()["submit_count"] == 1

    persisted = json.loads(
        (tmp_path / "fake-ats.json").read_text(encoding="utf-8")
    )
    assert persisted["submit_count"] == 1


def test_journey_restart_resumes_at_cv_ready_without_replaying_prepare(tmp_path):
    first = SyntheticApplicationJourney.create(tmp_path)
    proposed = first.start()
    first.handle(_button(proposed, "👍"))

    resumed = SyntheticApplicationJourney.create(tmp_path).start()

    assert resumed.lifecycle_state == "CV pronto"
    assert [button.label for button in resumed.buttons] == ["Compila"]
    assert len(resumed.documents) == 2
    assert SyntheticApplicationJourney.create(tmp_path).fake_ats_status()[
        "submit_count"
    ] == 0


def test_journey_keeps_vacancy_identity_stable_across_every_restart(tmp_path):
    first = SyntheticApplicationJourney.create(tmp_path)
    proposed = first.start()
    first.handle(_button(proposed, "👍"))

    second = SyntheticApplicationJourney.create(tmp_path)
    prepared = second.start()
    second.handle(_button(prepared, "Compila"))

    third = SyntheticApplicationJourney.create(tmp_path)
    review = third.start()
    assert review.lifecycle_state == "pronta da inviare"
    assert [button.label for button in review.buttons] == ["Invia"]

    submitted = third.handle(_button(review, "Invia"))
    assert submitted.lifecycle_state == "inviata"
    assert third.fake_ats_status()["submit_count"] == 1


def test_synthetic_fixture_recovers_filled_manifest_after_old_freshness_bug(
    tmp_path,
):
    journey = SyntheticApplicationJourney.create(tmp_path)
    proposed = journey.start()
    prepared = journey.handle(_button(proposed, "👍"))
    journey.handle(_button(prepared, "Compila"))
    store = JsonApplicationStore(tmp_path / "state")
    snapshot = store.load("synthetic-e2e-application")
    store.save(
        replace(
            snapshot,
            authorization_version=snapshot.opportunity_version,
            manifest=None,
            operational_status=OperationalStatus.VACANCY_CHANGED,
        )
    )

    recovered = SyntheticApplicationJourney.create(tmp_path).start()

    assert recovered.lifecycle_state == "pronta da inviare"
    assert [button.label for button in recovered.buttons] == ["Invia"]


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.documents = []
        self.acknowledged = []

    def send_journey_message(self, message):
        self.messages.append(message)

    def send_document(self, path, caption):
        self.documents.append((path, caption))

    def acknowledge_callback(self, callback_query_id, text):
        self.acknowledged.append((callback_query_id, text))


def test_telegram_restart_after_submission_reemits_report_without_buttons(
    tmp_path,
):
    journey = SyntheticApplicationJourney.create(tmp_path)
    proposed = journey.start()
    prepared = journey.handle(_button(proposed, "👍"))
    review = journey.handle(_button(prepared, "Compila"))
    journey.handle(_button(review, "Invia"))
    telegram = FakeTelegram()

    resumed = SyntheticTelegramSession(
        journey=SyntheticApplicationJourney.create(tmp_path),
        telegram=telegram,
        actor_id="owner",
        chat_id="private-chat",
    ).start()

    assert resumed.lifecycle_state == "inviata"
    assert resumed.buttons == ()
    assert telegram.documents[-1][0].suffix == ".md"


def test_telegram_session_rejects_other_users_and_presents_every_gate(tmp_path):
    journey = SyntheticApplicationJourney.create(tmp_path)
    telegram = FakeTelegram()
    session = SyntheticTelegramSession(
        journey=journey,
        telegram=telegram,
        actor_id="owner",
        chat_id="private-chat",
    )

    proposed = session.start()
    prepare = _button(proposed, "👍")
    assert not session.handle_update(
        {
            "callback_query": {
                "id": "foreign",
                "from": {"id": "somebody-else"},
                "message": {"chat": {"id": "private-chat"}},
                "data": prepare,
            }
        }
    )
    assert journey.fake_ats_status()["state"] == "empty"

    assert not session.handle_update(
        {
            "callback_query": {
                "id": "prepare",
                "from": {"id": "owner"},
                "message": {"chat": {"id": "private-chat"}},
                "data": prepare,
            }
        }
    )
    assert len(telegram.documents) == 2
    prepared = telegram.messages[-1]

    assert not session.handle_update(
        {
            "callback_query": {
                "id": "fill",
                "from": {"id": "owner"},
                "message": {"chat": {"id": "private-chat"}},
                "data": _button(prepared, "Compila"),
            }
        }
    )
    filled = telegram.messages[-1]
    assert journey.fake_ats_status()["submit_count"] == 0

    assert session.handle_update(
        {
            "callback_query": {
                "id": "submit",
                "from": {"id": "owner"},
                "message": {"chat": {"id": "private-chat"}},
                "data": _button(filled, "Invia"),
            }
        }
    )
    assert journey.fake_ats_status()["submit_count"] == 1
    assert telegram.messages[-1].lifecycle_state == "inviata"
    assert telegram.documents[-1][0].suffix == ".md"
    assert [item[0] for item in telegram.acknowledged] == [
        "foreign",
        "prepare",
        "fill",
        "submit",
    ]


def test_live_config_refuses_a_non_test_bot(tmp_path):
    config = tmp_path / "bot.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "purpose": "production",
                "telegram": {
                    "actor_id": "owner",
                    "chat_id": "private-chat",
                    "expected_bot_id": "test-bot-id",
                    "token_keychain_service": "service",
                    "token_keychain_account": "account",
                    "production_token_keychain_service": "production-service",
                    "production_token_keychain_account": "production-account",
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        SyntheticTestBotConfig.load(config)
    except ValueError as error:
        assert "production bot is forbidden" in str(error)
    else:
        raise AssertionError("production bot was accepted by the synthetic runner")


def test_live_runner_refuses_active_webhook_before_polling(tmp_path, monkeypatch):
    config = tmp_path / "bot.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "purpose": "synthetic-e2e-test",
                "telegram": {
                    "actor_id": "owner",
                    "chat_id": "private-chat",
                    "expected_bot_id": "test-bot-id",
                    "token_keychain_service": "test-service",
                    "token_keychain_account": "test-account",
                    "production_token_keychain_service": "production-service",
                    "production_token_keychain_account": "production-account",
                },
            }
        ),
        encoding="utf-8",
    )

    class Keychain:
        def get(self, service, account):
            return (
                "dedicated-test-token"
                if service == "test-service"
                else "production-token"
            )

    class ActiveWebhookBot:
        def __init__(self, *, token, chat_id):
            assert token == "dedicated-test-token"

        def bot_id(self):
            return "test-bot-id"

        def webhook_url(self):
            return "https://production.invalid/telegram"

        def poll_updates(self, **kwargs):
            raise AssertionError("active-webhook bot was polled")

    monkeypatch.setattr(
        synthetic_e2e_live,
        "MacOSKeychainCredentialStore",
        Keychain,
    )
    monkeypatch.setattr(
        synthetic_e2e_live,
        "CurlTelegramBotApi",
        ActiveWebhookBot,
    )

    try:
        run_live_synthetic_e2e(
            root=tmp_path / "run",
            test_bot_config=config,
        )
    except RuntimeError as error:
        assert "active webhook" in str(error)
    else:
        raise AssertionError("active-webhook test bot was accepted")


def test_curl_transport_keeps_bot_token_out_of_process_arguments():
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))

        class Result:
            returncode = 0
            stdout = (
                '{"ok":true,"result":{"url":'
                '"https://gateway.example/telegram"}}'
            )
            stderr = ""

        return Result()

    api = CurlTelegramBotApi(
        token="super-secret-bot-token",
        chat_id="private-chat",
        command_runner=run,
    )

    assert api.webhook_url() == "https://gateway.example/telegram"
    arguments, options = calls[0]
    assert "super-secret-bot-token" not in " ".join(arguments)
    assert "super-secret-bot-token" in options["input"]
