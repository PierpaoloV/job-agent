"""Explicit operator-triggered Telegram transport smoke test."""

from __future__ import annotations

import html
import os
from pathlib import Path

from notify_telegram import send_message
from telegram_delivery import TelegramDeliveryLedger, TelegramScheduledNotifier


def main() -> int:
    repository = html.escape(os.environ.get("GITHUB_REPOSITORY", "local"))
    run_id = html.escape(os.environ.get("GITHUB_RUN_ID", "manual"))
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    ledger = TelegramDeliveryLedger(
        runner_temp / f"telegram-alert-smoke-{run_id}.sqlite"
    )
    TelegramScheduledNotifier(
        ledger=ledger,
        message_sender=send_message,
    ).send_alert(
        {
            "stable_id": f"telegram-smoke:{run_id}",
            "title": "[DIAGNOSTICA] AI Scientist",
            "company": "Job Agent Smoke Test",
            "location": "Zurich",
            "score": 0.99,
            "priority": "high",
            "url": (
                "https://github.com/"
                f"{repository}/actions/runs/{run_id}"
            ),
        },
        reason="top_tier",
        idempotency_key=f"telegram-smoke:{run_id}",
    )
    print("Telegram realistic alert smoke test delivered with persisted receipts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
