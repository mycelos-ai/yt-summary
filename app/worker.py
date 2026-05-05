import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config
from app.repos import jobs as jobs_repo

log = logging.getLogger(__name__)

ProcessVideo = Callable[
    [aiosqlite.Connection, Config, str, Callable[[str], Awaitable[None]]],
    Awaitable[None],
]


class Worker:
    def __init__(
        self,
        db: aiosqlite.Connection,
        config: Config,
        process_video: ProcessVideo,
        poll_interval: float = 1.0,
    ):
        self._db = db
        self._config = config
        self._process_video = process_video
        self._poll_interval = poll_interval
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            job = await jobs_repo.claim_next(self._db)
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), self._poll_interval)
                continue
            try:
                job_id_capture = job.id

                async def set_step(step: str, _job_id: int = job_id_capture) -> None:  # noqa: B023
                    await jobs_repo.set_step(self._db, _job_id, step)

                await self._process_video(self._db, self._config, job.video_id, set_step)
                await jobs_repo.complete(self._db, job.id)
            except Exception as e:
                log.exception("job %s failed", job.id)
                await jobs_repo.fail(self._db, job.id, str(e))
