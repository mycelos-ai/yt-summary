from collections.abc import Awaitable, Callable

import aiosqlite

from app.config import Config


async def process_video(
    db: aiosqlite.Connection,
    config: Config,
    video_id: str,
    set_step: Callable[[str], Awaitable[None]],
) -> None:
    """Pipeline stub. Real implementation lands in Phase 5+."""
    await set_step("done (stub)")
