#!/usr/bin/env python3
"""Public command-line entry point for Job Agent administration."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hosted_installer import (  # noqa: E402
    ConfigError,
    HostedInstaller,
    HostedInstallerError,
    build_provisioning_plan,
    default_state_path,
    load_hosted_config,
    render_plan,
    run_doctor,
    set_telegram_webhook,
    smoke_test_hosted,
)
from synthetic_e2e_live import run_live_synthetic_e2e  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-agent")
    modes = parser.add_subparsers(dest="mode", required=True)
    hosted = modes.add_parser("hosted", help="manage the hosted deployment")
    commands = hosted.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser(
        "init",
        help="plan or provision a hosted single-user deployment",
    )
    initialize.add_argument("--config", required=True, type=Path)
    initialize.add_argument("--state", type=Path)
    initialize.add_argument(
        "--dry-run",
        action="store_true",
        help="print the redacted plan without external mutations",
    )

    doctor = commands.add_parser(
        "doctor",
        help="check hosted prerequisites and configuration inputs",
    )
    doctor.add_argument("--config", required=True, type=Path)
    webhook = commands.add_parser("_set-webhook", help=argparse.SUPPRESS)
    webhook.add_argument("--config", required=True, type=Path)
    smoke = commands.add_parser("_smoke", help=argparse.SUPPRESS)
    smoke.add_argument("--config", required=True, type=Path)

    synthetic = modes.add_parser(
        "synthetic-e2e",
        help="run a controlled Telegram-to-fake-ATS application journey",
    )
    synthetic_commands = synthetic.add_subparsers(
        dest="command", required=True
    )
    synthetic_run = synthetic_commands.add_parser(
        "run",
        help="present a fake vacancy and wait for the owner approval gates",
    )
    synthetic_run.add_argument("--root", required=True, type=Path)
    synthetic_run.add_argument("--test-bot-config", required=True, type=Path)
    synthetic_run.add_argument("--timeout-seconds", type=int, default=1800)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.mode == "synthetic-e2e":
            result = run_live_synthetic_e2e(
                root=arguments.root,
                test_bot_config=arguments.test_bot_config,
                timeout_seconds=arguments.timeout_seconds,
            )
            print(
                "Synthetic submission verified: "
                f"{result.confirmation_id}; report: {result.report_path}"
            )
            return 0

        config = load_hosted_config(arguments.config)
        if arguments.command == "_set-webhook":
            set_telegram_webhook(config)
            return 0
        if arguments.command == "_smoke":
            smoke_test_hosted(config)
            return 0
        if arguments.command == "doctor":
            report = run_doctor(config)
            print(report.render())
            if report.healthy:
                print("Hosted prerequisites are ready.")
                return 0
            print(
                f"Run `job-agent hosted doctor --config {arguments.config}` "
                "again after fixing the failures."
            )
            return 1

        steps = build_provisioning_plan(
            config,
            resolve_secret_inputs=not arguments.dry_run,
        )
        if arguments.dry_run:
            print(render_plan(config, steps))
            return 0

        report = run_doctor(config)
        if not report.healthy:
            print(report.render())
            print(
                "Hosted provisioning did not start because preflight failed. "
                "Fix the reported checks and retry."
            )
            return 1

        state_path = arguments.state or default_state_path(config)
        result = HostedInstaller(config, state_path=state_path).install()
        if result.completed:
            print(f"Hosted provisioning completed. State: {state_path}")
            return 0
        return 1
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except HostedInstallerError as exc:
        print(f"Provisioning stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
