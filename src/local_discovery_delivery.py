"""Owner-local delivery of Telegram opportunity cards staged by Actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping


class DiscoveryNotificationCapability:
    """Synchronize authoritative state before sending pending role cards."""

    def __init__(
        self,
        *,
        state_sync: Callable[[], Any],
        schedule,
        now: Callable[[], datetime] | None = None,
        minimum_sync_interval: timedelta = timedelta(minutes=3),
    ) -> None:
        if not callable(state_sync):
            raise TypeError("Discovery state sync must be callable")
        if minimum_sync_interval <= timedelta(0):
            raise ValueError("Discovery sync interval must be positive")
        self._state_sync = state_sync
        self._schedule = schedule
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._minimum_sync_interval = minimum_sync_interval
        self._next_sync_at: datetime | None = None

    def recompute(self, resume_generation: int) -> None:
        return None

    def run_once(self, execution) -> None:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Discovery capability clock must be timezone-aware")
        if self._next_sync_at is None or now >= self._next_sync_at:
            execution.checkpoint()
            result = self._state_sync()
            if result is False:
                raise RuntimeError("No authoritative Actions state is available")
            self._next_sync_at = now + self._minimum_sync_interval
        self._schedule.dispatch_legacy_interactive(
            before_send=execution.checkpoint
        )
        self._schedule.dispatch_pending(before_send=execution.checkpoint)

    def status(self) -> Mapping[str, Any]:
        return {"state": "ready", "healthy": True}


__all__ = ["DiscoveryNotificationCapability"]
