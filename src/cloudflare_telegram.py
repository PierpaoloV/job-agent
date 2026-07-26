"""Cloud-issued Telegram controls for GitHub-hosted discovery delivery."""

from __future__ import annotations

import re
import secrets
import os
from typing import Any, Callable, Mapping

import requests

from application_identity import approved_application_id


_EXPECTED_LABELS = ("👍", "👎", "Dimmi di più")
_CALLBACK_DATA = re.compile(r"ja1:[A-Za-z0-9_-]{8,48}")


class GatewayRoleButtonFactory:
    """Request exact, opaque callback capabilities before sending a role card."""

    def __init__(
        self,
        *,
        endpoint: str,
        internal_token: str,
        actor_id: str,
        chat_id: str,
        session=requests,
        event_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._internal_token = internal_token.strip()
        self._actor_id = str(actor_id).strip()
        self._chat_id = str(chat_id).strip()
        self._session = session
        self._event_token_factory = event_token_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        if not all(
            (
                self._endpoint.startswith("https://"),
                self._internal_token,
                self._actor_id,
                self._chat_id,
            )
        ):
            raise ValueError("Cloud callback gateway configuration is incomplete")

    def __call__(self, job: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
        stable_id = str(job.get("stable_id", "")).strip()
        vacancy_version = str(
            job.get("official_vacancy_version", "")
        ).strip()
        application_id = approved_application_id(stable_id, vacancy_version)
        try:
            response = self._session.post(
                f"{self._endpoint}/v1/authorizations",
                headers={
                    "Authorization": f"Bearer {self._internal_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "event_id": self._event_token_factory(),
                    "application_id": application_id,
                    "official_vacancy_version": vacancy_version,
                    "actor_id": self._actor_id,
                    "chat_id": self._chat_id,
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                "Cloud callback authorization outcome is uncertain"
            ) from exc
        if not response.ok:
            raise RuntimeError(
                "Cloud callback gateway rejected authorization "
                f"(HTTP {response.status_code})"
            )
        try:
            value = response.json()
        except Exception as exc:
            raise RuntimeError(
                "Cloud callback gateway returned invalid JSON"
            ) from exc
        buttons = value.get("buttons") if isinstance(value, Mapping) else None
        if not isinstance(buttons, list) or len(buttons) != 3:
            raise RuntimeError("Cloud callback gateway returned invalid controls")
        normalized = []
        for expected, button in zip(_EXPECTED_LABELS, buttons, strict=True):
            if not isinstance(button, Mapping):
                raise RuntimeError(
                    "Cloud callback gateway returned invalid controls"
                )
            text = str(button.get("text", ""))
            callback_data = str(button.get("callback_data", ""))
            if (
                text != expected
                or not _CALLBACK_DATA.fullmatch(callback_data)
                or len(callback_data.encode("utf-8")) > 64
            ):
                raise RuntimeError(
                    "Cloud callback gateway returned invalid controls"
                )
            normalized.append(
                {"text": text, "callback_data": callback_data}
            )
        return tuple(normalized)


def gateway_button_factory_from_environment(
    environment: Mapping[str, str] | None = None,
) -> GatewayRoleButtonFactory | None:
    """Build hosted controls when all cloud bindings are explicitly present."""

    values = environment if environment is not None else os.environ
    names = (
        "JOB_AGENT_CALLBACK_GATEWAY_URL",
        "JOB_AGENT_CALLBACK_GATEWAY_TOKEN",
        "TELEGRAM_ACTOR_ID",
        "TELEGRAM_CHAT_ID",
    )
    configured = {name: str(values.get(name, "")).strip() for name in names}
    gateway_requested = bool(
        configured["JOB_AGENT_CALLBACK_GATEWAY_URL"]
        or configured["JOB_AGENT_CALLBACK_GATEWAY_TOKEN"]
    )
    if not gateway_requested:
        return None
    if not all(configured.values()):
        raise ValueError("Cloud callback gateway configuration is incomplete")
    return GatewayRoleButtonFactory(
        endpoint=configured["JOB_AGENT_CALLBACK_GATEWAY_URL"],
        internal_token=configured["JOB_AGENT_CALLBACK_GATEWAY_TOKEN"],
        actor_id=configured["TELEGRAM_ACTOR_ID"],
        chat_id=configured["TELEGRAM_CHAT_ID"],
    )


__all__ = [
    "GatewayRoleButtonFactory",
    "gateway_button_factory_from_environment",
]
