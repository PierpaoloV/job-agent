"""Idempotent hosted completion notification for prepared application files."""

from __future__ import annotations

import argparse
import html
import os
from pathlib import Path
import re
from typing import Callable, Sequence

from notify_telegram import (
    TelegramMessage,
    TelegramSendRejected,
    send_message,
)
from telegram_delivery import TelegramDeliveryLedger


_CANONICAL_APPLICATION_ID = re.compile(r"approved-[0-9a-f]{16}")
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_GITHUB_RUN_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*"
)


def arm_remote_preparation_completion(
    *,
    application_id: str,
    official_vacancy_version: str,
    run_url: str,
    ledger: TelegramDeliveryLedger,
) -> str | None:
    """Persist the at-most-once send boundary before Telegram is called."""

    _, delivery_key = _completion(
        application_id,
        official_vacancy_version,
        run_url,
    )
    ledger.stage_outbound(delivery_key)
    claim_token = ledger.claim_outbound(delivery_key)
    if claim_token is None:
        return None
    if not ledger.mark_outbound_sending(delivery_key, claim_token):
        raise RuntimeError(
            "Hosted preparation notification could not enter sending state"
        )
    return claim_token


def dispatch_remote_preparation_completion(
    *,
    application_id: str,
    official_vacancy_version: str,
    run_url: str,
    claim_token: str,
    ledger: TelegramDeliveryLedger,
    message_sender: Callable[[TelegramMessage], None] = send_message,
) -> bool:
    """Deliver a previously staged notification at most once."""

    message, delivery_key = _completion(
        application_id,
        official_vacancy_version,
        run_url,
    )
    if not ledger.outbound_claim_is_sending(delivery_key, claim_token):
        raise RuntimeError(
            "Hosted preparation notification claim is not active"
        )
    try:
        message_sender(message)
    except TelegramSendRejected:
        ledger.requeue_outbound_rejected(delivery_key, claim_token)
        raise
    except Exception:
        ledger.mark_outbound_uncertain(delivery_key, claim_token)
        raise
    if not ledger.mark_outbound_sent(delivery_key, claim_token):
        raise RuntimeError(
            "Hosted preparation notification acknowledgement was not persisted"
        )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage or dispatch a hosted preparation completion"
    )
    parser.add_argument("command", choices=("arm", "dispatch"))
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--official-vacancy-version", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--claim-token")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("data/telegram-deliveries.sqlite"),
    )
    args = parser.parse_args(argv)
    ledger = TelegramDeliveryLedger(args.ledger)
    kwargs = {
        "application_id": args.application_id,
        "official_vacancy_version": args.official_vacancy_version,
        "run_url": args.run_url,
        "ledger": ledger,
    }
    if args.command == "arm":
        claim_token = arm_remote_preparation_completion(**kwargs)
        _github_output("should_send", "true" if claim_token else "false")
        if claim_token is not None:
            _github_output("claim_token", claim_token)
    else:
        if not args.claim_token:
            raise ValueError("Hosted completion dispatch requires a claim token")
        dispatch_remote_preparation_completion(
            **kwargs,
            claim_token=args.claim_token,
        )
    return 0


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise RuntimeError("GITHUB_OUTPUT is required for hosted completion")
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def _completion(
    application_id: str,
    official_vacancy_version: str,
    run_url: str,
) -> tuple[TelegramMessage, str]:
    if not _CANONICAL_APPLICATION_ID.fullmatch(str(application_id)):
        raise ValueError("Hosted completion application id must be canonical")
    if not _CANONICAL_SHA256.fullmatch(str(official_vacancy_version)):
        raise ValueError("Hosted completion vacancy version must be canonical")
    if not _GITHUB_RUN_URL.fullmatch(str(run_url)):
        raise ValueError("Hosted completion run URL must be canonical")
    message = TelegramMessage(
        "✅ <b>CV e lettera pronti</b>\n\n"
        "La preparazione remota è conclusa per "
        f"<code>{html.escape(application_id)}</code>.\n"
        f'<a href="{html.escape(run_url, quote=True)}">'
        "Apri la run e il pacchetto cifrato</a>.\n\n"
        "Nessun modulo ATS è stato compilato o inviato."
    )
    delivery_key = (
        "hosted-preparation-complete:"
        f"{application_id}:{official_vacancy_version}:message:0"
    )
    return message, delivery_key


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "arm_remote_preparation_completion",
    "dispatch_remote_preparation_completion",
]
