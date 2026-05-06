import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo

log = logging.getLogger(__name__)

SyncFn = Callable[[aiosqlite.Connection, Config, str], Awaitable[None]]


class PlaylistScheduler:
    """Periodically refresh every saved playlist.

    Reads `playlist_refresh_interval_hours` from settings each tick. The
    scheduler does not refresh on startup — it sleeps one interval first
    to avoid a refresh storm on container restart.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        sync_fn: SyncFn,
        *,
        min_sleep_seconds: float = 1.0,
    ) -> None:
        self._db = db
        self._config = config
        self._sync_fn = sync_fn
        self._min_sleep_seconds = min_sleep_seconds
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def _interval_seconds(self) -> float:
        raw = await settings_repo.get(self._db, "playlist_refresh_interval_hours")
        try:
            hours = float(raw) if raw is not None else 6.0
        except ValueError:
            hours = 6.0
        return max(self._min_sleep_seconds, hours * 3600)

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), seconds)

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self._sleep_or_stop(await self._interval_seconds())
            if self._stopped.is_set():
                return
            try:
                playlists = await playlists_repo.list_for_user(self._db, 1)
            except Exception:
                log.exception("scheduler: list_for_user failed")
                continue
            for playlist in playlists:
                if self._stopped.is_set():
                    return
                try:
                    await self._sync_fn(self._db, self._config, playlist.id)
                except Exception:
                    log.exception(
                        "scheduler: sync failed for playlist %s", playlist.id
                    )
