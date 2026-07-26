"""Live Telegram transport for a dedicated synthetic-test bot."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from typing import Any, Mapping

from macos_keychain import MacOSKeychainCredentialStore
from synthetic_e2e import (
    SyntheticApplicationJourney,
    SyntheticJourneyMessage,
    SyntheticTelegramSession,
)


@dataclass(frozen=True)
class SyntheticTestBotConfig:
    """Identity and Keychain reference for a bot used only by synthetic tests."""

    actor_id: str
    chat_id: str
    expected_bot_id: str
    token_keychain_service: str
    token_keychain_account: str
    production_token_keychain_service: str
    production_token_keychain_account: str

    @classmethod
    def load(cls, path: Path) -> "SyntheticTestBotConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Synthetic bot configuration must be an object")
        if set(payload) != {"version", "purpose", "telegram"}:
            raise ValueError("Synthetic bot configuration schema is invalid")
        if payload.get("version") != 1:
            raise ValueError("Unsupported synthetic bot configuration version")
        if payload.get("purpose") != "synthetic-e2e-test":
            raise ValueError(
                "A dedicated synthetic-e2e-test bot is required; "
                "the production bot is forbidden"
            )
        telegram = payload.get("telegram")
        expected = {
            "actor_id",
            "chat_id",
            "expected_bot_id",
            "token_keychain_service",
            "token_keychain_account",
            "production_token_keychain_service",
            "production_token_keychain_account",
        }
        if not isinstance(telegram, Mapping) or set(telegram) != expected:
            raise ValueError("Synthetic Telegram configuration is invalid")
        values = {name: str(telegram[name]).strip() for name in expected}
        if any(not value for value in values.values()):
            raise ValueError("Synthetic Telegram configuration is incomplete")
        return cls(**values)


class CurlTelegramBotApi:
    """Bot API transport using the macOS system TLS stack.

    The secret-bearing URL is supplied through curl's stdin config so the bot
    token never appears in the process argument list.
    """

    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        command_runner=subprocess.run,
        retry_sleep=time.sleep,
    ):
        if not token.strip() or not str(chat_id).strip():
            raise ValueError("Telegram token and chat ID are required")
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = str(chat_id)
        self._run = command_runner
        self._retry_sleep = retry_sleep

    def send_journey_message(self, message: SyntheticJourneyMessage) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": message.text,
            "disable_web_page_preview": True,
        }
        if message.buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {
                            "text": button.label,
                            "callback_data": button.callback_data,
                        }
                        for button in message.buttons
                    ]
                ]
            }
        self._json_post("sendMessage", payload)

    def send_document(self, path: Path, caption: str) -> None:
        self._call(
            "sendDocument",
            arguments=(
                "--request",
                "POST",
                "--form-string",
                f"chat_id={self._chat_id}",
                "--form-string",
                f"caption={caption}",
                "--form",
                f"document=@{Path(path)}",
            ),
            timeout=70,
        )

    def acknowledge_callback(self, callback_query_id: str, text: str) -> None:
        self._json_post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def poll_updates(
        self, *, offset: int | None, timeout: int
    ) -> list[dict[str, Any]]:
        params = [
            ("timeout", str(timeout)),
            ("allowed_updates", json.dumps(["message", "callback_query"])),
        ]
        if offset is not None:
            params.append(("offset", str(offset)))
        arguments = ["--get"]
        for key, value in params:
            arguments.extend(("--data-urlencode", f"{key}={value}"))
        payload = self._call(
            "getUpdates",
            arguments=tuple(arguments),
            timeout=timeout + 15,
        )
        result = payload.get("result")
        if not isinstance(result, list):
            raise RuntimeError("Telegram polling returned an invalid response")
        return [dict(item) for item in result if isinstance(item, Mapping)]

    def webhook_url(self) -> str:
        payload = self._call("getWebhookInfo")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Telegram webhook inspection returned invalid data")
        return str(result.get("url", ""))

    def bot_id(self) -> str:
        payload = self._call("getMe")
        result = payload.get("result")
        if not isinstance(result, Mapping) or result.get("id") is None:
            raise RuntimeError("Telegram bot identity is unavailable")
        return str(result["id"])

    def _json_post(
        self, method: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="job-agent-telegram-",
                suffix=".json",
                delete=False,
            ) as temporary:
                json.dump(dict(payload), temporary)
                temporary.flush()
                temporary_path = Path(temporary.name)
            return self._call(
                method,
                arguments=(
                    "--request",
                    "POST",
                    "--header",
                    "content-type: application/json",
                    "--data-binary",
                    f"@{temporary_path}",
                ),
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _call(
        self,
        method: str,
        *,
        arguments: tuple[str, ...] = (),
        timeout: int = 40,
    ) -> Mapping[str, Any]:
        endpoint = f"{self._base}/{method}"
        curl_config = (
            f'url = "{endpoint}"\n'
            "silent\n"
            "show-error\n"
            "fail-with-body\n"
            f"max-time = {timeout}\n"
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._run(
                    ["/usr/bin/curl", "--config", "-", *arguments],
                    input=curl_config,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError("Telegram curl request failed")
                payload = json.loads(result.stdout)
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("ok") is not True
                ):
                    raise RuntimeError("Telegram rejected the curl request")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self._retry_sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise last_error


@dataclass(frozen=True)
class LiveSyntheticResult:
    confirmation_id: str
    report_path: Path
    fake_ats_path: Path


def run_live_synthetic_e2e(
    *,
    root: Path,
    test_bot_config: Path,
    timeout_seconds: int = 1800,
) -> LiveSyntheticResult:
    """Run against a dedicated webhook-free bot; never mutate production."""

    config = SyntheticTestBotConfig.load(test_bot_config)
    keychain = MacOSKeychainCredentialStore()
    token = keychain.get(
        config.token_keychain_service, config.token_keychain_account
    )
    if not token:
        raise RuntimeError("Synthetic Telegram bot token is unavailable in Keychain")
    production_token = keychain.get(
        config.production_token_keychain_service,
        config.production_token_keychain_account,
    )
    if not production_token:
        raise RuntimeError("Production bot identity is unavailable for comparison")
    if secrets.compare_digest(token, production_token):
        raise RuntimeError("Production Telegram bot is forbidden for synthetic tests")
    telegram = CurlTelegramBotApi(token=token, chat_id=config.chat_id)
    if telegram.bot_id() != config.expected_bot_id:
        raise RuntimeError("Synthetic Telegram bot identity does not match config")
    if telegram.webhook_url():
        raise RuntimeError(
            "Synthetic test bot has an active webhook; refusing to delete or replace it"
        )
    offset = None
    journey = SyntheticApplicationJourney.create(root)
    session = SyntheticTelegramSession(
        journey=journey,
        telegram=telegram,
        actor_id=config.actor_id,
        chat_id=config.chat_id,
    )
    initial = session.start()
    print("SYNTHETIC_CARD_SENT", flush=True)
    deadline = time.monotonic() + timeout_seconds
    terminal = initial.terminal
    while time.monotonic() < deadline and not terminal:
        updates = telegram.poll_updates(offset=offset, timeout=25)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset or 0, update_id + 1)
            terminal = session.handle_update(update) or terminal
    if not terminal:
        raise TimeoutError("Synthetic Telegram journey timed out")
    status = journey.fake_ats_status()
    confirmation_id = str(status.get("confirmation_id", ""))
    report_path = root / "reports" / "synthetic-e2e-application.md"
    if (
        status.get("state") != "submitted"
        or int(status.get("submit_count", 0)) != 1
        or not confirmation_id.startswith("FAKE-ATS-")
        or not report_path.is_file()
    ):
        raise RuntimeError("Synthetic journey ended without verified evidence")
    print(
        json.dumps(
            {
                "event": "SYNTHETIC_SUBMISSION_VERIFIED",
                "confirmation_id": confirmation_id,
                "report_path": str(report_path),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return LiveSyntheticResult(
        confirmation_id=confirmation_id,
        report_path=report_path,
        fake_ats_path=root / "fake-ats.json",
    )
__all__ = [
    "CurlTelegramBotApi",
    "LiveSyntheticResult",
    "SyntheticTestBotConfig",
    "run_live_synthetic_e2e",
]
