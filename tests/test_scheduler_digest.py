from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.repos import digests as digests_repo
from app.repos import users as users_repo
from app.scheduler import DigestScheduler


@pytest.mark.asyncio
async def test_sweep_enqueues_when_hour_matches_and_none_today(
    db, config, monkeypatch,
):
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=True, digest_hour_local=7,
    )
    fake_generate = AsyncMock()
    monkeypatch.setattr(
        "app.scheduler.digest_service.generate", fake_generate,
    )
    sched = DigestScheduler(db, config)
    # Use today's date for now_local so the "today" window aligns with
    # the digests row's created_at = datetime('now') default.
    today = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
    await sched.sweep_once(now_local=today)
    fake_generate.assert_awaited_once_with(db, user_id=1)


@pytest.mark.asyncio
async def test_sweep_skips_when_digest_already_today(
    db, config, monkeypatch,
):
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=True, digest_hour_local=7,
    )
    # Seed an existing digest TODAY. digests_repo.create_pending stamps
    # created_at via datetime('now') so the "today" exists_in_range
    # check finds it regardless of period_start/period_end (those are
    # business-window timestamps, not row-insertion timestamps).
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=datetime.now() - timedelta(hours=24),
        period_end=datetime.now(),
    )
    fake_generate = AsyncMock()
    monkeypatch.setattr(
        "app.scheduler.digest_service.generate", fake_generate,
    )
    sched = DigestScheduler(db, config)
    today = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
    await sched.sweep_once(now_local=today)
    fake_generate.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_skips_disabled_profiles(db, config, monkeypatch):
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=False, digest_hour_local=7,
    )
    fake_generate = AsyncMock()
    monkeypatch.setattr(
        "app.scheduler.digest_service.generate", fake_generate,
    )
    sched = DigestScheduler(db, config)
    today = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
    await sched.sweep_once(now_local=today)
    fake_generate.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_skips_when_hour_does_not_match(
    db, config, monkeypatch,
):
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=True, digest_hour_local=7,
    )
    fake_generate = AsyncMock()
    monkeypatch.setattr(
        "app.scheduler.digest_service.generate", fake_generate,
    )
    sched = DigestScheduler(db, config)
    nine_am = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    await sched.sweep_once(now_local=nine_am)
    fake_generate.assert_not_called()
