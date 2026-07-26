"""Lossless seed loading and locked JSON persistence for the watchlist."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterator


STATE_VERSION = "job-agent.watchlist.v1"
_ATTEMPT_LOCKS_GUARD = threading.Lock()
_ATTEMPT_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class TargetedCompanySeed:
    raw_bytes: bytes
    sha256: str
    company_names: tuple[str, ...]

    @classmethod
    def read(cls, path: Path) -> "TargetedCompanySeed":
        raw = Path(path).read_bytes()
        return cls(
            raw_bytes=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
            company_names=_extract_company_names(raw.decode("utf-8")),
        )


@dataclass(frozen=True)
class ImportedSeed:
    sha256: str
    company_names: tuple[str, ...]
    source_hashes: tuple[str, ...] = ()


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "seed": None,
        "active_companies": {},
        "company_proposals": {},
        "alert_proposals": {},
        "callback_authorizations": {},
    }


class JsonWatchlistStore:
    def __init__(self, path: Path):
        self._path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_state()
        state = json.loads(self._path.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION:
            raise ValueError("Unsupported watchlist state version")
        return state

    def save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self._path)

    def transact(self, operation: Callable[[dict[str, Any]], Any]) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = self.load()
                result = operation(state)
                self.save(state)
                return result
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def subscription_attempt_lock(
        self, proposal_id: str, *, blocking: bool
    ) -> Iterator[bool]:
        """Fence one external attempt until it finishes or its process exits."""

        digest = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()
        lock_path = self._path.with_suffix(
            self._path.suffix + f".subscription-{digest}.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        thread_key = str(lock_path.resolve())
        with _ATTEMPT_LOCKS_GUARD:
            thread_lock = _ATTEMPT_LOCKS.setdefault(thread_key, threading.Lock())
        thread_acquired = thread_lock.acquire(blocking=blocking)
        if not thread_acquired:
            yield False
            return
        try:
            with lock_path.open("a+", encoding="utf-8") as lock:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                try:
                    fcntl.flock(lock.fileno(), flags)
                except BlockingIOError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            thread_lock.release()

    def import_seed(self, seed: TargetedCompanySeed) -> ImportedSeed:
        def operation(state: dict[str, Any]) -> ImportedSeed:
            existing = state.get("seed")
            revisions = _seed_revisions(existing)
            cumulative_names = list(
                () if existing is None else existing.get("company_names", ())
            )
            known_names = {_company_key(name) for name in cumulative_names}
            known_hashes = {str(item["sha256"]) for item in revisions}
            if seed.sha256 not in known_hashes:
                revisions.append(
                    {
                        "sha256": seed.sha256,
                        "company_names": list(seed.company_names),
                    }
                )
                for name in seed.company_names:
                    if _company_key(name) not in known_names:
                        known_names.add(_company_key(name))
                        cumulative_names.append(name)
            current_hash = str(revisions[-1]["sha256"])
            state["seed"] = {
                "sha256": current_hash,
                "company_names": cumulative_names,
                "revisions": revisions,
            }
            state["active_companies"] = {
                key: value
                for key, value in state["active_companies"].items()
                if value.get("source") != "seed"
            }
            return ImportedSeed(
                current_hash,
                tuple(cumulative_names),
                tuple(str(item["sha256"]) for item in revisions),
            )

        return self.transact(operation)

    def active_company_names(self) -> tuple[str, ...]:
        state = self.load()
        return tuple(
            item["name"]
            for _, item in sorted(state["active_companies"].items())
            if item.get("status", "active") == "active"
        )

    def company_monitoring_status(self, name: str) -> str | None:
        item = self.load()["active_companies"].get(_company_key(name))
        return None if item is None else str(item.get("status", "active"))


def company_key(name: str) -> str:
    return _company_key(name)


def _seed_revisions(existing: dict[str, Any] | None) -> list[dict[str, Any]]:
    if existing is None:
        return []
    revisions = existing.get("revisions")
    if revisions is not None:
        return [dict(item) for item in revisions]
    return [
        {
            "sha256": str(existing["sha256"]),
            "company_names": list(existing.get("company_names", ())),
        }
    ]


def _company_key(name: str) -> str:
    return " ".join(name.casefold().split())


_NUMBERED_COMPANY = re.compile(r"^\d+\.\s+\*\*(.+?)\*\*")
_BULLET_COMPANY = re.compile(r"^-\s+(?:\[(.+?)\]|\*\*(.+?)\*\*|([^—]+?))\s+—\s+")


def _extract_company_names(markdown: str) -> tuple[str, ...]:
    """Extract the explicit company labels while preserving first-seen order."""

    names: list[str] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        numbered = _NUMBERED_COMPANY.match(line)
        bullet = _BULLET_COMPANY.match(line)
        if numbered:
            name = numbered.group(1)
        elif bullet:
            name = next(group for group in bullet.groups() if group is not None)
        else:
            continue
        name = name.strip().rstrip(".: ")
        key = _company_key(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return tuple(names)
