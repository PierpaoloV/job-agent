"""Owner-only durable receipts for idempotent local ATS fill recovery."""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from application_domain import FilledApplication


class JsonAtsFillReceiptStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def load(
        self, application_id: str, intent_id: str, artifact_version: str
    ) -> FilledApplication | None:
        path = self._path(application_id, intent_id, artifact_version)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("identity") != [
                application_id,
                intent_id,
                artifact_version,
            ]:
                raise ValueError
            os.chmod(path, 0o600)
            return FilledApplication.from_dict(value["result"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise RuntimeError("Local ATS fill receipt is unavailable") from None

    def load_session(
        self, application_id: str, intent_id: str, artifact_version: str
    ) -> dict[str, Any] | None:
        value = self._load_value(application_id, intent_id, artifact_version)
        if value is None or value.get("session") is None:
            return None
        session = value["session"]
        if not isinstance(session, dict):
            raise RuntimeError("Local ATS fill receipt is unavailable")
        return dict(session)

    def save(
        self,
        application_id: str,
        intent_id: str,
        result: FilledApplication,
    ) -> None:
        self.save_with_session(application_id, intent_id, result, session=None)

    def save_with_session(
        self,
        application_id: str,
        intent_id: str,
        result: FilledApplication,
        *,
        session: Mapping[str, Any] | None,
    ) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        path = self._path(application_id, intent_id, result.artifact_version)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {
                    "identity": [
                        application_id,
                        intent_id,
                        result.artifact_version,
                    ],
                    "result": _json_value(asdict(result)),
                    "session": _json_value(dict(session)) if session else None,
                },
                output,
                indent=2,
                sort_keys=True,
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def load_submission_session(
        self, application_id: str, artifact_version: str
    ) -> dict[str, Any] | None:
        """Find one unambiguous durable fill scope for a ready manifest."""

        if not self._root.exists():
            return None
        sessions: dict[str, dict[str, Any]] = {}
        for path in sorted(self._root.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                identity = value.get("identity")
                session = value.get("session")
                if (
                    not isinstance(identity, list)
                    or len(identity) != 3
                    or identity[0] != application_id
                    or identity[2] != artifact_version
                    or not isinstance(session, dict)
                ):
                    continue
                canonical = json.dumps(session, sort_keys=True, separators=(",", ":"))
                sessions[canonical] = dict(session)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                raise RuntimeError("Local ATS fill receipt is unavailable") from None
        if len(sessions) > 1:
            raise RuntimeError("Local ATS fill scope is ambiguous")
        return next(iter(sessions.values()), None)

    def _load_value(
        self, application_id: str, intent_id: str, artifact_version: str
    ) -> dict[str, Any] | None:
        path = self._path(application_id, intent_id, artifact_version)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("identity") != [
                application_id,
                intent_id,
                artifact_version,
            ]:
                raise ValueError
            os.chmod(path, 0o600)
            return dict(value)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise RuntimeError("Local ATS fill receipt is unavailable") from None

    def _path(
        self, application_id: str, intent_id: str, artifact_version: str
    ) -> Path:
        identity = "\u001f".join((application_id, intent_id, artifact_version))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


__all__ = ["JsonAtsFillReceiptStore"]
