"""Durable persistence for validated deep-grading results."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from deep_grading_contract import DeepGradeResult


class DeepGradeStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save(self, result: DeepGradeResult) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(result.opportunity_id)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(
                json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(
            self._root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def load(self, opportunity_id: str) -> DeepGradeResult:
        path = self._path(opportunity_id)
        if not path.exists():
            raise KeyError(opportunity_id)
        return DeepGradeResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _path(self, opportunity_id: str) -> Path:
        digest = hashlib.sha256(opportunity_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"


__all__ = ["DeepGradeStore"]
