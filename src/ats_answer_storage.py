"""Restrictive, local-only persistence for ATS answers."""

from __future__ import annotations

from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
from typing import Callable, NotRequired, TypedDict, cast


class AnswerRequestRecord(TypedDict):
    application_id: str
    field_id: str
    semantic_key: str
    salary: bool
    created_at: str
    answered_at: NotRequired[str]
    scope: NotRequired[str]


class SubmittedAnswerRecord(TypedDict):
    answers: dict[str, str]
    recorded_at: str


class LocalProfileRecord(TypedDict):
    standardized_defaults: dict[str, str]
    protected_terms: list[str]


class VaultState(TypedDict):
    version: int
    defaults: dict[str, str]
    requests: dict[str, AnswerRequestRecord]
    one_use: dict[str, str]
    submitted: dict[str, SubmittedAnswerRecord]
    profile: LocalProfileRecord


_EMPTY_VAULT: VaultState = {
    "version": 1,
    "defaults": {},
    "requests": {},
    "one_use": {},
    "submitted": {},
    "profile": {"standardized_defaults": {}, "protected_terms": []},
}


class VaultError(RuntimeError):
    """A deliberately non-diagnostic error safe for general logs."""


class LocalAnswerVault:
    """JSON persistence whose directory and files are accessible only by the owner."""

    def __init__(self, path: Path):
        self._path = Path(path)

    @classmethod
    def for_repository(cls, repository_root: Path) -> "LocalAnswerVault":
        """Use the repository's already-gitignored local data tree."""

        return cls(Path(repository_root) / "data" / "private" / "ats-answers.json")

    def snapshot(self) -> VaultState:
        try:
            return deepcopy(self._read())
        except (OSError, json.JSONDecodeError, TypeError, KeyError, AttributeError):
            raise VaultError("Local answer vault is unavailable") from None

    def transact(
        self, operation: Callable[[VaultState], VaultState | None]
    ) -> VaultState:
        try:
            self._prepare_directory()
            lock_path = self._path.with_suffix(self._path.suffix + ".lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            os.chmod(lock_path, 0o600)
            with os.fdopen(descriptor, "a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    current = self._read()
                    updated = operation(deepcopy(current))
                    if updated is not None:
                        self._write(updated)
                        current = updated
                    return deepcopy(current)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except VaultError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, KeyError, AttributeError):
            raise VaultError("Local answer vault is unavailable") from None

    def _read(self) -> VaultState:
        if not self._path.exists():
            return deepcopy(_EMPTY_VAULT)
        value = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise VaultError("Local answer vault is unavailable")
        if value.get("version") != 1:
            raise VaultError("Local answer vault is unavailable")
        for key in ("defaults", "requests", "one_use", "submitted"):
            if not isinstance(value.get(key), dict):
                raise VaultError("Local answer vault is unavailable")
        value.setdefault(
            "profile", {"standardized_defaults": {}, "protected_terms": []}
        )
        if not isinstance(value["profile"], dict):
            raise VaultError("Local answer vault is unavailable")
        os.chmod(self._path, 0o600)
        return cast(VaultState, value)

    def _write(self, value: VaultState) -> None:
        self._prepare_directory()
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(self._path)
        os.chmod(self._path, 0o600)

    def _prepare_directory(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
