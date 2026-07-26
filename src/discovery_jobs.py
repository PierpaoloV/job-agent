"""Deterministic command entry points used by the remote discovery workflow."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from discovery_schedule import (
    DiscoverySchedule,
    FileDiscoveryScheduleStore,
    SchedulePolicy,
)
from discovery_pending import PendingShortlistStore
from discovery_verification import verify_shortlist
from cloudflare_telegram import gateway_button_factory_from_environment
from hosted_artifact_preparation import HostedPreparationInputStore
from opportunity_decisions import (
    FileOpportunityDecisionStore,
    SuppressingDiscoveryNotifier,
)
from search_official_source import SearchOfficialSource
from telegram_delivery import TelegramScheduledNotifier
from vacancy_policy import VerificationState, verification_state
from workflow import ShortlistArtifact


class EnvironmentClock:
    """System clock with an explicit override for deterministic operations/tests."""

    def now(self) -> datetime:
        configured = os.environ.get("JOB_AGENT_NOW")
        if not configured:
            return datetime.now(timezone.utc)
        value = datetime.fromisoformat(configured.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("JOB_AGENT_NOW must include a timezone")
        return value


def ingest_and_screen(output: Path, *, days_back: int) -> int:
    import main

    coordinator = main.build_coordinator()
    pending = PendingShortlistStore()
    merged = pending.merge(
        coordinator.ingest_and_screen(days_back=days_back)
    )
    artifact = pending.merge(
        verify_shortlist(
            merged,
            source=SearchOfficialSource(),
            clock=EnvironmentClock(),
        )
    )
    artifact.write(output)
    count = len(_verified_shortlisted(artifact))
    _github_output("verified_count", str(count))
    _github_output("artifact_version", artifact.version)
    return count


def finalize_ingest(path: Path) -> None:
    import main

    artifact = ShortlistArtifact.read(path)
    main.LegacyPersistence().mark_seen(
        [item.as_job() for item in artifact.opportunities]
    )


def deep_grade(
    path: Path,
    *,
    preparation_store: HostedPreparationInputStore | None = None,
) -> list[dict[str, Any]]:
    import main

    artifact = ShortlistArtifact.read(path)
    jobs = [item.as_grading_job() for item in _verified_shortlisted(artifact)]
    if not jobs:
        return []
    # PortfolioDeepGrader iterates the whole input before sorting/truncating;
    # using its size here preserves one cached/idempotent grading per role.
    graded = main.ProductionPortfolioGrader(
        portfolio_policy=main._load_portfolio_policy()
    ).rank(jobs, len(jobs))
    (preparation_store or HostedPreparationInputStore(
        Path("data/hosted-preparation-inputs")
    )).capture_graded(graded)
    return graded


def dispatch_schedule(
    graded_jobs: Sequence[Mapping[str, Any]],
    *,
    state_path: Path,
    deliver: bool = True,
    dispatch_only: bool = False,
) -> None:
    policy = _schedule_policy()
    notifier = TelegramScheduledNotifier(
        role_button_factory=gateway_button_factory_from_environment()
    )
    schedule = DiscoverySchedule(
        store=FileDiscoveryScheduleStore(state_path),
        notifier=SuppressingDiscoveryNotifier(
            notifier,
            FileOpportunityDecisionStore(
                Path("data/opportunity-decisions.json")
            ),
        ),
        clock=EnvironmentClock(),
        policy=policy,
    )
    if dispatch_only:
        schedule.dispatch_pending()
    elif deliver:
        schedule.process(graded_jobs)
    else:
        schedule.stage(graded_jobs)


def _gradeable_shortlisted(
    artifact: ShortlistArtifact,
) -> tuple[Any, ...]:
    artifact.validate()
    return tuple(
        item
        for item in artifact.opportunities
        if item.shortlisted
        and (
            (
                verification_state(item.job.get("verification_status"))
                == VerificationState.VERIFIED
                and str(item.job.get("official_description", "")).strip()
            )
            or verification_state(item.job.get("verification_status"))
            == VerificationState.NEEDS_LOCAL_FETCH
        )
    )


def _verified_shortlisted(
    artifact: ShortlistArtifact,
) -> tuple[Any, ...]:
    """Backward-compatible name for all records safe to send to deep grading."""

    return _gradeable_shortlisted(artifact)


def _schedule_policy() -> SchedulePolicy:
    anchor_value = os.environ.get("JOB_AGENT_DIGEST_ANCHOR", "2026-01-01T00:00:00Z")
    anchor = datetime.fromisoformat(anchor_value.replace("Z", "+00:00"))
    return SchedulePolicy(
        digest_every=timedelta(
            days=float(os.environ.get("JOB_AGENT_DIGEST_DAYS", "3"))
        ),
        imminent_deadline=timedelta(
            hours=float(os.environ.get("JOB_AGENT_IMMINENT_HOURS", "36"))
        ),
        digest_limit=int(os.environ.get("JOB_AGENT_DIGEST_LIMIT", "10")),
        anchor=anchor,
    )


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def _read_jobs(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Graded jobs file must contain an array of objects")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest-and-screen")
    ingest.add_argument("--output", type=Path, required=True)
    ingest.add_argument("--days", type=int, default=1)

    finalize = commands.add_parser("finalize-ingest")
    finalize.add_argument("--artifact", type=Path, required=True)

    grade = commands.add_parser("deep-grade")
    grade.add_argument("--artifact", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)

    dispatch = commands.add_parser("dispatch-schedule")
    dispatch.add_argument("--graded-jobs", type=Path)
    dispatch.add_argument(
        "--state", type=Path, default=Path("data/discovery-schedule.json")
    )
    delivery_mode = dispatch.add_mutually_exclusive_group()
    delivery_mode.add_argument(
        "--stage-only",
        action="store_true",
        help="Persist delivery intents for the owner-local interactive worker",
    )
    delivery_mode.add_argument(
        "--dispatch-only",
        action="store_true",
        help="Deliver intents that were staged in authoritative state",
    )

    args = parser.parse_args(argv)
    if args.command == "ingest-and-screen":
        ingest_and_screen(args.output, days_back=args.days)
    elif args.command == "finalize-ingest":
        finalize_ingest(args.artifact)
    elif args.command == "deep-grade":
        jobs = deep_grade(args.artifact)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(jobs, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "dispatch-schedule":
        jobs = _read_jobs(args.graded_jobs)
        dispatch_schedule(
            jobs,
            state_path=args.state,
            deliver=not args.stage_only,
            dispatch_only=args.dispatch_only,
        )
        PendingShortlistStore().clear_graded(jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EnvironmentClock",
    "deep_grade",
    "dispatch_schedule",
    "finalize_ingest",
    "ingest_and_screen",
    "main",
]
