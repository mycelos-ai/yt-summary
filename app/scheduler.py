from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.heartbeat import HeartbeatRegistry

import aiosqlite

from app.config import Config
from app.repos import embeddings as embeddings_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services import embeddings as embeddings_service

log = logging.getLogger(__name__)

SyncFn = Callable[[aiosqlite.Connection, Config, str], Awaitable[None]]

# Default refresh interval if no setting is present.
_DEFAULT_INTERVAL_MINUTES = 60.0


class PlaylistScheduler:
    """Periodically refresh every saved playlist.

    Reads `playlist_refresh_interval_minutes` from settings each tick
    (preferred), falling back to the legacy `playlist_refresh_interval_hours`
    setting for installs that haven't migrated yet. After every full tick
    the scheduler writes `scheduler_last_tick_at` so the settings UI can
    show users when the last scan ran — useful for diagnosing a stalled
    or slow Pi.

    The scheduler does not refresh on startup — it sleeps one interval
    first to avoid a refresh storm on container restart.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        sync_fn: SyncFn,
        *,
        min_sleep_seconds: float = 1.0,
        heartbeat: HeartbeatRegistry | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._sync_fn = sync_fn
        self._min_sleep_seconds = min_sleep_seconds
        self._heartbeat = heartbeat
        self._stopped = asyncio.Event()
        self._tick_requested = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def _interval_seconds(self) -> float:
        """Resolve the configured interval into seconds.

        Order of precedence:
        1. ``playlist_refresh_interval_minutes`` (new, lets users pick
           sub-hour intervals like 15 min)
        2. ``playlist_refresh_interval_hours`` (legacy, kept so existing
           installs keep working without manual migration)
        3. 60-minute default
        """
        minutes_raw = await settings_repo.get(
            self._db, "playlist_refresh_interval_minutes"
        )
        if minutes_raw is not None:
            try:
                minutes = float(minutes_raw)
            except ValueError:
                minutes = _DEFAULT_INTERVAL_MINUTES
        else:
            hours_raw = await settings_repo.get(
                self._db, "playlist_refresh_interval_hours"
            )
            if hours_raw is not None:
                try:
                    minutes = float(hours_raw) * 60
                except ValueError:
                    minutes = _DEFAULT_INTERVAL_MINUTES
            else:
                minutes = _DEFAULT_INTERVAL_MINUTES
        return max(self._min_sleep_seconds, minutes * 60)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Wait up to ``seconds``, but return early on stop OR tick request.

        The tick-request event lets the diagnostics page's 'Jetzt prüfen'
        button trigger a refresh without waiting out the rest of the
        interval. The flag is cleared on wakeup so the next iteration
        sleeps normally — otherwise the loop would hot-spin.
        """
        stop_task = asyncio.create_task(self._stopped.wait())
        tick_task = asyncio.create_task(self._tick_requested.wait())
        try:
            await asyncio.wait(
                {stop_task, tick_task},
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, tick_task):
                if not t.done():
                    t.cancel()
            self._tick_requested.clear()

    async def _record_tick(self) -> None:
        """Stamp 'scheduler_last_tick_at' with the current UTC time."""
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            await settings_repo.set(self._db, "scheduler_last_tick_at", now)
        except Exception:
            # Don't let an observability write break the actual work loop.
            log.exception("scheduler: failed to record last-tick timestamp")

    async def _reembed_pending_batch(self, limit: int = 10) -> int:
        """Drain up to `limit` videos that need re-embedding.

        Per-video failures are logged and skipped — one bad video must
        not stop the batch. Returns the count of successful embeds for
        the heartbeat step string.
        """
        try:
            ids = await embeddings_repo.videos_pending_reembed(
                self._db, limit
            )
        except Exception:
            log.exception("reembed: videos_pending_reembed failed")
            return 0

        n_done = 0
        for video_id in ids:
            try:
                video = await videos_repo.get(self._db, video_id)
                if video is None or not video.summary:
                    continue
                vector = await embeddings_service.embed_text(video.summary)
                await embeddings_repo.upsert_summary_embedding(
                    self._db, video_id, vector,
                )
                n_done += 1
            except Exception:
                log.exception("reembed: video %s failed", video_id)
        return n_done

    async def run(self) -> None:
        while not self._stopped.is_set():
            # Resolve the interval BEFORE the heartbeat so the
            # last_tick_at timestamp truthfully marks the start of
            # the idle period — the DB round-trip can take tens of ms
            # on a busy Pi and that staleness matters for the
            # diagnostics page's 3× alive/stale threshold.
            interval = await self._interval_seconds()
            self._touch(current_step="sleeping")
            await self._sleep_or_stop(interval)
            if self._stopped.is_set():
                return
            self._touch(current_step="scanning")
            try:
                playlists = await playlists_repo.list_for_user(self._db, 1)
            except Exception:
                log.exception("scheduler: list_for_user failed")
                await self._record_tick()
                continue
            for playlist in playlists:
                if self._stopped.is_set():
                    return
                self._touch(current_step=f"syncing {playlist.id}")
                try:
                    await self._sync_fn(self._db, self._config, playlist.id)
                except Exception:
                    log.exception(
                        "scheduler: sync failed for playlist %s", playlist.id
                    )
            n_reembedded = await self._reembed_pending_batch(limit=10)
            if n_reembedded:
                self._touch(
                    current_step=f"re-embedded {n_reembedded} videos"
                )
            await self._record_tick()

    def request_tick(self) -> None:
        """Wake the scheduler so the next iteration runs immediately.

        Idempotent — a second request before the loop wakes is a no-op
        (asyncio.Event is set-once-until-cleared).
        """
        self._tick_requested.set()

    def _touch(self, *, current_step: str | None = None) -> None:
        if self._heartbeat is not None:
            self._heartbeat.touch("scheduler", current_step=current_step)

    async def current_interval_seconds(self) -> float:
        """Public wrapper around the resolved interval.

        The diagnostics page calls this to compute the 'alive vs stale'
        threshold for the scheduler heartbeat (3× this value).
        """
        return await self._interval_seconds()
