"""Execute non-preparation Telegram decisions inside GitHub Actions."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from discovery_schedule import FileDiscoveryScheduleStore
from hosted_artifact_preparation import HostedPreparationInputStore
from notify_telegram import TelegramMessage, send_message
from opportunity_decisions import (
    FileOpportunityDecisionStore,
    ScheduleJobLookup,
    opportunity_details_messages,
)


def execute_decision(
    *,
    action: str,
    application_id: str,
    vacancy_version: str,
    reason: str | None = None,
    inputs,
    job_lookup: Callable[[str, str], Mapping[str, Any]],
    decisions: FileOpportunityDecisionStore,
    send_status: Callable[[str], None],
) -> str:
    if action not in {"details", "discard"}:
        raise ValueError("Hosted opportunity action must be details or discard")
    discard_reason = str(reason or "").strip()
    if action == "discard" and (
        not discard_reason or len(discard_reason) > 1000
    ):
        raise ValueError("Hosted discard requires a concise reason")
    prepared = inputs.load(application_id, vacancy_version)
    if prepared.official_vacancy.version != vacancy_version:
        raise ValueError("Hosted opportunity vacancy version mismatch")
    job = dict(job_lookup(application_id, vacancy_version))
    if action == "details":
        for message in opportunity_details_messages(job, prepared):
            send_status(message)
        return "Dettagli inviati"
    decisions.discard(
        application_id,
        vacancy_version,
        job=job,
        reason=discard_reason,
    )
    return "Opportunità scartata"


def execute_from_repository(
    *,
    root: Path,
    action: str,
    application_id: str,
    vacancy_version: str,
    reason: str | None = None,
) -> str:
    root = Path(root)
    return execute_decision(
        action=action,
        application_id=application_id,
        vacancy_version=vacancy_version,
        reason=reason,
        inputs=HostedPreparationInputStore(
            root / "data" / "hosted-preparation-inputs"
        ),
        job_lookup=ScheduleJobLookup(
            FileDiscoveryScheduleStore(
                root / "data" / "discovery-schedule.json"
            )
        ),
        decisions=FileOpportunityDecisionStore(
            root / "data" / "opportunity-decisions.json"
        ),
        send_status=lambda text: send_message(
            TelegramMessage(html.escape(text))
        ),
    )


def _event_reason(path: Path | None) -> str | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    payload = value.get("client_payload") if isinstance(value, dict) else None
    if not isinstance(payload, dict):
        return None
    reason = payload.get("reason")
    return str(reason) if reason is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--action", choices=("details", "discard"), required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--official-vacancy-version", required=True)
    parser.add_argument("--event-path", type=Path)
    args = parser.parse_args(argv)
    result = execute_from_repository(
        root=args.root,
        action=args.action,
        application_id=args.application_id,
        vacancy_version=args.official_vacancy_version,
        reason=_event_reason(args.event_path),
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["execute_decision", "execute_from_repository", "main"]
