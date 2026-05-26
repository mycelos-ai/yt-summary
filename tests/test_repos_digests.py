from datetime import datetime, timedelta

import aiosqlite

from app.models import DigestStatus
from app.repos import digests as digests_repo


async def test_create_pending_and_get(db: aiosqlite.Connection):
    start = datetime(2026, 5, 25, 7, 0)
    end = start + timedelta(hours=24)
    d = await digests_repo.create_pending(
        db, user_id=1, period_start=start, period_end=end,
    )
    assert d.status == DigestStatus.PENDING
    fetched = await digests_repo.get(db, d.id)
    assert fetched is not None
    assert fetched.id == d.id


async def test_mark_ready_persists_payload(db: aiosqlite.Connection):
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
    )
    await digests_repo.mark_ready(
        db, digest_id=d.id, tldr="hello",
        top_items_json="[]", item_count=0,
    )
    fetched = await digests_repo.get(db, d.id)
    assert fetched is not None
    assert fetched.status == DigestStatus.READY
    assert fetched.tldr == "hello"
    assert fetched.item_count == 0


async def test_mark_failed_stores_error(db: aiosqlite.Connection):
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
    )
    await digests_repo.mark_failed(db, digest_id=d.id, error="LLM down")
    fetched = await digests_repo.get(db, d.id)
    assert fetched is not None
    assert fetched.status == DigestStatus.FAILED
    assert fetched.error == "LLM down"


async def test_exists_for_today(db: aiosqlite.Connection):
    today_start = datetime(2026, 5, 26, 0, 0)
    today_end = today_start + timedelta(days=1)
    assert await digests_repo.exists_in_range(
        db, user_id=1,
        range_start=today_start, range_end=today_end,
        in_states=("pending", "ready"),
    ) is False
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=today_start + timedelta(hours=7),
        period_end=today_start + timedelta(hours=31),
    )
    assert await digests_repo.exists_in_range(
        db, user_id=1,
        range_start=today_start, range_end=today_end,
        in_states=("pending", "ready"),
    ) is True


async def test_list_for_user(db: aiosqlite.Connection):
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
    )
    await digests_repo.create_pending(
        db, user_id=2,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
    )
    rows = await digests_repo.list_for_user(db, user_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0].user_id == 1
