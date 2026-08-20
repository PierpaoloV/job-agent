"""Durable fallback cleanup for Telegram review receipts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Mapping, Sequence

from notify_telegram import TelegramReceipt, delete_telegram_messages


CLEANUP_OBLIGATION_VERSION = "job-agent.telegram-review-cleanup.v1"
_REVIEW_ID = re.compile(r"[A-Za-z0-9_-]{8,48}")


@dataclass(frozen=True)
class TelegramReviewCleanupObligation:
    version: str
    review_id: str
    expires_at: str
    receipts: tuple[TelegramReceipt, ...]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "review_id": self.review_id,
            "expires_at": self.expires_at,
            "receipts": [
                {"message_id": item.message_id, "chat_id": item.chat_id}
                for item in self.receipts
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "TelegramReviewCleanupObligation":
        if set(value) != {"version", "review_id", "expires_at", "receipts"}:
            raise ValueError("Telegram review cleanup obligation is not canonical")
        raw_receipts = value.get("receipts")
        if not isinstance(raw_receipts, list):
            raise ValueError("Telegram review cleanup receipts are invalid")
        receipts = tuple(
            TelegramReceipt(
                message_id=int(item["message_id"]),
                chat_id=str(item["chat_id"]),
            )
            for item in raw_receipts
            if isinstance(item, Mapping)
            and set(item) == {"message_id", "chat_id"}
        )
        obligation = cls(
            version=str(value.get("version", "")),
            review_id=str(value.get("review_id", "")),
            expires_at=str(value.get("expires_at", "")),
            receipts=receipts,
        )
        obligation.validate()
        return obligation

    def validate(self) -> None:
        if self.version != CLEANUP_OBLIGATION_VERSION:
            raise ValueError("Unsupported Telegram review cleanup version")
        if not _REVIEW_ID.fullmatch(self.review_id) or not self.expires_at:
            raise ValueError("Telegram review cleanup identity is invalid")
        if not 2 <= len(self.receipts) <= 3:
            raise ValueError("Telegram review cleanup requires two or three receipts")
        if len({item.message_id for item in self.receipts}) != len(self.receipts):
            raise ValueError("Telegram review cleanup receipts repeat identity")
        if len({item.chat_id for item in self.receipts}) != 1:
            raise ValueError("Telegram review cleanup receipts cross chat scope")


class TelegramReviewCleanupStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save(
        self,
        *,
        review_id: str,
        expires_at: str,
        receipts: Sequence[TelegramReceipt],
    ) -> TelegramReviewCleanupObligation:
        obligation = TelegramReviewCleanupObligation(
            version=CLEANUP_OBLIGATION_VERSION,
            review_id=review_id,
            expires_at=expires_at,
            receipts=tuple(receipts),
        )
        obligation.validate()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        path = self._path(review_id)
        if path.exists():
            existing = self.load(review_id)
            if (
                existing.expires_at != expires_at
                or obligation.receipts[: len(existing.receipts)]
                != existing.receipts
            ):
                raise RuntimeError("Telegram review cleanup obligation differs")
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(
                json.dumps(obligation.to_dict(), indent=2, sort_keys=True) + "\n"
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return obligation

    def load(self, review_id: str) -> TelegramReviewCleanupObligation:
        value = json.loads(self._path(review_id).read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Telegram review cleanup obligation must be an object")
        parsed = TelegramReviewCleanupObligation.from_dict(value)
        if value != parsed.to_dict():
            raise ValueError("Telegram review cleanup obligation is not canonical")
        return parsed

    def all(self) -> tuple[TelegramReviewCleanupObligation, ...]:
        if not self._root.exists():
            return ()
        return tuple(self.load(path.stem) for path in sorted(self._root.glob("*.json")))

    def remove(self, review_id: str) -> None:
        path = self._path(review_id)
        if path.exists():
            path.unlink()

    def _path(self, review_id: str) -> Path:
        if not _REVIEW_ID.fullmatch(str(review_id)):
            raise ValueError("Telegram review cleanup id must be canonical")
        return self._root / f"{review_id}.json"


def reconcile_cleanup_obligations(store: TelegramReviewCleanupStore) -> int:
    removed = 0
    for obligation in store.all():
        delete_telegram_messages(obligation.receipts)
        store.remove(obligation.review_id)
        removed += 1
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("data/telegram-review-cleanup-obligations"),
    )
    args = parser.parse_args(argv)
    reconcile_cleanup_obligations(TelegramReviewCleanupStore(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLEANUP_OBLIGATION_VERSION",
    "TelegramReviewCleanupObligation",
    "TelegramReviewCleanupStore",
    "reconcile_cleanup_obligations",
]
