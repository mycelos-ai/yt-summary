"""In-memory heartbeat registry for background workers.

Each long-running background task (summary worker, TTS worker,
playlist scheduler) calls :meth:`HeartbeatRegistry.touch` once per
loop iteration. The diagnostics page reads :meth:`snapshot` to
render an "is the worker alive?" view.

State is intentionally process-local — it's reset on container
restart, which is the only restart the operator cares about. No
DB row, no migration, no IPC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Heartbeat:
    """Snapshot of one worker's last observed state.

    ``last_tick_at`` is UTC-naive (matches SQLite ``datetime('now')``
    output used throughout the repos).
    """
    name: str
    last_tick_at: datetime
    current_job_id: int | None = None
    current_step: str | None = None


class HeartbeatRegistry:
    """Process-wide ``name -> Heartbeat`` map.

    Writes are single-producer per worker name; reads (snapshot)
    are O(workers) and return a shallow copy so the caller can
    iterate without racing further writes. No lock is needed —
    dict assignment is atomic in CPython.
    """

    def __init__(self) -> None:
        self._heartbeats: dict[str, Heartbeat] = {}

    def touch(
        self,
        name: str,
        *,
        current_job_id: int | None = None,
        current_step: str | None = None,
    ) -> None:
        self._heartbeats[name] = Heartbeat(
            name=name,
            last_tick_at=datetime.now(UTC).replace(tzinfo=None),
            current_job_id=current_job_id,
            current_step=current_step,
        )

    def snapshot(self) -> dict[str, Heartbeat]:
        """Return a shallow copy of the current registry."""
        return dict(self._heartbeats)
