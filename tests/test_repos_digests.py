from datetime import UTC, datetime, timedelta

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
    # Use datetime.now() rather than a hard-coded date so the window
    # aligns with the row's created_at = datetime('now') default
    # regardless of the wall-clock day the test runs on.
    today_start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    today_end = today_start + timedelta(days=1)
    assert await digests_repo.exists_in_range(
        db, user_id=1,
        range_start=today_start, range_end=today_end,
        in_states=(DigestStatus.PENDING, DigestStatus.READY),
    ) is False
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=today_start + timedelta(hours=7),
        period_end=today_start + timedelta(hours=31),
    )
    assert await digests_repo.exists_in_range(
        db, user_id=1,
        range_start=today_start, range_end=today_end,
        in_states=(DigestStatus.PENDING, DigestStatus.READY),
    ) is True


async def test_exists_in_range_returns_false_outside_window(
    db: aiosqlite.Connection,
):
    """A digest created now must not show up when the queried window
    ended an hour ago. Guards against regressions in the datetime()
    normalisation."""
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
    )
    past_start = datetime(2020, 1, 1)
    past_end = datetime(2020, 1, 2)
    assert await digests_repo.exists_in_range(
        db, user_id=1,
        range_start=past_start, range_end=past_end,
        in_states=(DigestStatus.PENDING, DigestStatus.READY),
    ) is False


async def test_exists_in_range_respects_in_states_filter(
    db: aiosqlite.Connection,
):
    """A pending digest in-window must NOT match when in_states is
    restricted to ('ready',). Catches regressions where status IN (...)
    is stripped or always-trued."""
    today_start = datetime(2026, 5, 26, 0, 0)
    today_end = today_start + timedelta(days=1)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=today_start + timedelta(hours=7),
        period_end=today_start + timedelta(hours=31),
    )
    assert await digests_repo.exists_in_range(
        db, user_id=1,
        range_start=today_start, range_end=today_end,
        in_states=(DigestStatus.READY,),
    ) is False


async def test_mark_rendering_transitions_status(db: aiosqlite.Connection):
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=datetime(2026, 5, 25),
        period_end=datetime(2026, 5, 26),
    )
    await digests_repo.mark_rendering(db, digest_id=d.id)
    fetched = await digests_repo.get(db, d.id)
    assert fetched is not None
    assert fetched.status == DigestStatus.RENDERING


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


async def test_create_pending_persists_selection(db: aiosqlite.Connection):
    end = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=end - timedelta(hours=4), period_end=end,
        selected_video_ids_json='["a", "b"]',
    )
    assert d.selected_video_ids_json == '["a", "b"]'


async def test_create_pending_selection_defaults_to_null(
    db: aiosqlite.Connection,
):
    end = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=end - timedelta(hours=4), period_end=end,
    )
    assert d.selected_video_ids_json is None


async def test_latest_period_end_none_without_digests(
    db: aiosqlite.Connection,
):
    assert await digests_repo.latest_period_end(db, user_id=1) is None


async def test_latest_period_end_returns_newest(db: aiosqlite.Connection):
    now = datetime.now(UTC).replace(microsecond=0)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=48),
        period_end=now - timedelta(hours=24),
    )
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=24),
        period_end=now - timedelta(hours=2),
    )
    got = await digests_repo.latest_period_end(db, user_id=1)
    assert got == now - timedelta(hours=2)


async def test_latest_period_end_ignores_failed(db: aiosqlite.Connection):
    now = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=24), period_end=now,
    )
    await digests_repo.mark_failed(db, digest_id=d.id, error="boom")
    assert await digests_repo.latest_period_end(db, user_id=1) is None


async def test_latest_period_end_scoped_by_user(db: aiosqlite.Connection):
    now = datetime.now(UTC).replace(microsecond=0)
    await db.execute("INSERT INTO users (id, name) VALUES (2, 'other')")
    await db.commit()
    await digests_repo.create_pending(
        db, user_id=2,
        period_start=now - timedelta(hours=24), period_end=now,
    )
    assert await digests_repo.latest_period_end(db, user_id=1) is None
