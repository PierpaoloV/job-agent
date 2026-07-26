"""Durable local storage for opportunity verification records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from opportunity_domain import OpportunityRecord


class JsonOpportunityStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def save(self, record: OpportunityRecord) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.lead.stable_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def load(self, stable_id: str) -> OpportunityRecord:
        path = self._path(stable_id)
        if not path.exists():
            raise KeyError(stable_id)
        return OpportunityRecord.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def list(self) -> tuple[OpportunityRecord, ...]:
        if not self._root.exists():
            return ()
        return tuple(
            OpportunityRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self._root.glob("*.json"))
        )

    def _path(self, stable_id: str) -> Path:
        digest = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"
