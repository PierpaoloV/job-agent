#!/usr/bin/env python3
"""Import legacy and candidate-supplied history into the local stores."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_storage import JsonApplicationStore  # noqa: E402
from history_migration import (  # noqa: E402
    HistoryMigrationService,
    LegacySqliteHistorySource,
    load_known_applications,
)
from opportunity_storage import JsonOpportunityStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently migrate the original job-agent history"
    )
    parser.add_argument("--legacy", type=Path, default=REPO_ROOT / "data/seen.sqlite")
    parser.add_argument(
        "--known",
        type=Path,
        default=REPO_ROOT / "data/known-applications.json",
    )
    parser.add_argument(
        "--opportunities",
        type=Path,
        default=REPO_ROOT / "data/migrated-opportunities",
    )
    parser.add_argument(
        "--applications",
        type=Path,
        default=REPO_ROOT / "data/applications",
    )
    args = parser.parse_args(argv)
    records = load_known_applications(args.known)
    service = HistoryMigrationService(
        legacy_source=LegacySqliteHistorySource(args.legacy),
        opportunity_store=JsonOpportunityStore(args.opportunities),
        application_store=JsonApplicationStore(args.applications),
        migrated_at=lambda: datetime.now(timezone.utc),
    )
    report = service.migrate(known_applications=records)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
