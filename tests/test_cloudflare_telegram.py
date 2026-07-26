from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cloudflare_telegram import (
    GatewayRoleButtonFactory,
    gateway_button_factory_from_environment,
)


VACANCY_VERSION = f"sha256:{'a' * 64}"


class FakeResponse:
    def __init__(self, body, *, ok=True):
        self._body = body
        self.ok = ok
        self.status_code = 200 if ok else 503

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_gateway_button_factory_issues_exact_scoped_short_lived_controls():
    session = FakeSession(
        FakeResponse(
            {
                "buttons": [
                    {"text": "👍", "callback_data": "ja1:prepare-token"},
                    {"text": "👎", "callback_data": "ja1:discard-token"},
                    {
                        "text": "Dimmi di più",
                        "callback_data": "ja1:details-token",
                    },
                ],
                "expires_at": "2026-07-26T10:15:00Z",
            }
        )
    )
    factory = GatewayRoleButtonFactory(
        endpoint="https://gateway.example",
        internal_token="secret",
        actor_id="123456789",
        chat_id="123456789",
        session=session,
        event_token_factory=lambda: "delivery-123",
    )

    buttons = factory(
        {
            "stable_id": "modelco:ai-scientist",
            "official_vacancy_version": VACANCY_VERSION,
        }
    )

    assert [button["text"] for button in buttons] == [
        "👍",
        "👎",
        "Dimmi di più",
    ]
    url, request = session.calls[0]
    assert url == "https://gateway.example/v1/authorizations"
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert request["timeout"] == 15
    assert request["json"] == {
        "event_id": "delivery-123",
            "application_id": "approved-51e026d6e93916a2",
        "official_vacancy_version": VACANCY_VERSION,
        "actor_id": "123456789",
        "chat_id": "123456789",
    }


def test_gateway_button_factory_rejects_malformed_controls_before_telegram():
    factory = GatewayRoleButtonFactory(
        endpoint="https://gateway.example",
        internal_token="secret",
        actor_id="123456789",
        chat_id="123456789",
        session=FakeSession(
            FakeResponse(
                {
                    "buttons": [
                        {"text": "👍", "callback_data": "prepare:too-broad"},
                    ]
                }
            )
        ),
    )

    try:
        factory(
            {
                "stable_id": "modelco:ai-scientist",
                "official_vacancy_version": VACANCY_VERSION,
            }
        )
    except RuntimeError as error:
        assert "invalid controls" in str(error)
    else:
        raise AssertionError("Malformed gateway controls were accepted")


def test_environment_factory_is_enabled_only_by_complete_cloud_configuration():
    values = {
        "JOB_AGENT_CALLBACK_GATEWAY_URL": "https://gateway.example",
        "JOB_AGENT_CALLBACK_GATEWAY_TOKEN": "secret",
        "TELEGRAM_ACTOR_ID": "123456789",
        "TELEGRAM_CHAT_ID": "123456789",
    }

    assert isinstance(
        gateway_button_factory_from_environment(values),
        GatewayRoleButtonFactory,
    )
    try:
        gateway_button_factory_from_environment(
            {
                key: value
                for key, value in values.items()
                if key != "JOB_AGENT_CALLBACK_GATEWAY_TOKEN"
            }
        )
    except ValueError as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("Partial cloud configuration did not fail closed")

    assert gateway_button_factory_from_environment({}) is None
