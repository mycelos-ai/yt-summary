import asyncio
from unittest.mock import AsyncMock

import aiosqlite

from app.config import Config
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.scheduler import PlaylistScheduler


async def _make_playlist(db: aiosqlite.Connection, pid: str) -> None:
    await playlists_repo.create(
        db, playlist_id=pid, user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )


async def test_scheduler_calls_sync_for_each_playlist(db: aiosqlite.Connection, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    # Tiny interval so the loop fires immediately
    await settings_repo.set(db, "playlist_refresh_interval_hours", "0")

    sync_calls: list[str] = []

    async def fake_sync(db_, config_, playlist_id):
        sync_calls.append(playlist_id)

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=fake_sync, min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if len(sync_calls) >= 2:
            break
    scheduler.stop()
    await task

    assert set(sync_calls[:2]) == {"p1", "p2"}


async def test_scheduler_swallows_per_playlist_errors(
    db: aiosqlite.Connection, tmp_path
):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    await settings_repo.set(db, "playlist_refresh_interval_hours", "0")

    seen: list[str] = []

    async def flaky_sync(db_, config_, playlist_id):
        seen.append(playlist_id)
        if playlist_id == "p1":
            raise RuntimeError("boom")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=flaky_sync, min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if "p2" in seen:
            break
    scheduler.stop()
    await task

    assert "p1" in seen
    assert "p2" in seen


async def test_scheduler_stops_promptly(db: aiosqlite.Connection, tmp_path):
    """Stop must wake a long sleep so the task ends quickly."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    # No playlists, so the scheduler sleeps the whole interval.
    await settings_repo.set(db, "playlist_refresh_interval_hours", "10")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    # Give the scheduler a moment to settle into its first sleep.
    await asyncio.sleep(0.1)
    scheduler.stop()
    # Should return well within 1s, not 10 hours.
    await asyncio.wait_for(task, timeout=1.0)


async def test_scheduler_reads_minutes_setting_when_present(
    db: aiosqlite.Connection, tmp_path
):
    """The new minutes-based setting takes precedence over the legacy
    hours setting so users can configure intervals below 1 hour."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "15")
    await settings_repo.set(db, "playlist_refresh_interval_hours", "1")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    seconds = await scheduler._interval_seconds()
    assert seconds == 15 * 60


async def test_scheduler_falls_back_to_hours_setting(
    db: aiosqlite.Connection, tmp_path
):
    """If only the legacy hours setting is present, honour it so
    existing installs keep working without a manual migration."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await settings_repo.set(db, "playlist_refresh_interval_hours", "2")

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    seconds = await scheduler._interval_seconds()
    assert seconds == 2 * 3600


async def test_scheduler_default_is_60_minutes_when_no_setting(
    db: aiosqlite.Connection, tmp_path
):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=AsyncMock(), min_sleep_seconds=0.05
    )
    seconds = await scheduler._interval_seconds()
    assert seconds == 60 * 60


async def test_scheduler_records_last_tick_after_run(
    db: aiosqlite.Connection, tmp_path
):
    """Users can't see what they can't measure — after every full tick
    the scheduler writes `scheduler_last_tick_at` so the UI can show
    'last scan: 4 min ago'."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await _make_playlist(db, "p1")
    await settings_repo.set(db, "playlist_refresh_interval_minutes", "0")

    async def fake_sync(db_, config_, playlist_id):
        pass

    scheduler = PlaylistScheduler(
        db=db, config=config, sync_fn=fake_sync, min_sleep_seconds=0.05
    )
    task = asyncio.create_task(scheduler.run())
    last_tick = None
    for _ in range(40):
        await asyncio.sleep(0.05)
        last_tick = await settings_repo.get(db, "scheduler_last_tick_at")
        if last_tick:
            break
    scheduler.stop()
    await task

    assert last_tick is not None
    # Should be a recent ISO timestamp — parse it to be sure.
    from datetime import datetime
    parsed = datetime.fromisoformat(last_tick)
    # No timezone assertion — UTC-naive iso is fine for an internal
    # "last seen at" field. Just verify it's a recent moment.
    assert parsed.year >= 2024
