import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.models import DigestStatus
from app.repos import digests as digests_repo
from app.repos import llm_models as llm_models_repo
from app.repos import videos as videos_repo
from app.services import digest as digest_service


async def _video_with_highlights(db, vid: str, hl: list[dict]) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=f"Title {vid}", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_highlights(db, vid, json.dumps(hl))


async def _default_llm(db) -> None:
    await llm_models_repo.insert(
        db, label="Test", provider_id="openai", model="openai/gpt-4o",
        api_key="key", base_url="", make_default=True,
    )


@pytest.mark.asyncio
async def test_generate_empty_pool_writes_silence_tldr(db, monkeypatch):
    fake_llm = AsyncMock()
    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)

    d = await digest_service.generate(
        db, user_id=1,
    )
    refreshed = await digests_repo.get(db, d.id)
    assert refreshed is not None
    assert refreshed.status == DigestStatus.READY
    assert refreshed.item_count == 0
    assert refreshed.tldr is not None
    assert "Nothing noteworthy" in refreshed.tldr
    fake_llm.assert_not_called()


@pytest.mark.asyncio
async def test_generate_ranks_pool_via_llm(db, monkeypatch):
    await _default_llm(db)
    await _video_with_highlights(
        db, "v1", [{"text": "a", "rank": 1, "reason": "y"}],
    )
    await _video_with_highlights(
        db, "v2", [{"text": "b", "rank": 1, "reason": "y"}],
    )
    fake_llm = AsyncMock(return_value=json.dumps({
        "tldr": "Two things happened.",
        "top_items": [
            {"video_id": "v1", "rank": 1, "hook": "h1", "reason": "r1"},
            {"video_id": "v2", "rank": 2, "hook": "h2", "reason": "r2"},
        ],
    }))
    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)

    d = await digest_service.generate(db, user_id=1)
    refreshed = await digests_repo.get(db, d.id)
    assert refreshed.status == DigestStatus.READY
    assert refreshed.tldr == "Two things happened."
    assert refreshed.item_count == 2
    top = json.loads(refreshed.top_items_json)
    assert {t["video_id"] for t in top} == {"v1", "v2"}


@pytest.mark.asyncio
async def test_generate_filters_empty_highlights_from_pool(db, monkeypatch):
    await _default_llm(db)
    await _video_with_highlights(db, "v1", [{"text": "a", "rank": 1, "reason": ""}])
    await _video_with_highlights(db, "v2", [])  # LLM said "nothing noteworthy"
    fake_llm = AsyncMock(return_value=json.dumps({
        "tldr": "One thing.", "top_items": [
            {"video_id": "v1", "rank": 1, "hook": "h", "reason": "r"},
        ],
    }))
    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)

    d = await digest_service.generate(db, user_id=1)
    refreshed = await digests_repo.get(db, d.id)
    assert refreshed.item_count == 1


@pytest.mark.asyncio
async def test_generate_drops_hallucinated_video_ids(db, monkeypatch):
    await _default_llm(db)
    await _video_with_highlights(db, "v1", [{"text": "a", "rank": 1, "reason": "y"}])
    fake_llm = AsyncMock(return_value=json.dumps({
        "tldr": "Real and fake.", "top_items": [
            {"video_id": "v1", "rank": 1, "hook": "real", "reason": ""},
            {"video_id": "ghost", "rank": 2, "hook": "fake", "reason": ""},
        ],
    }))
    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)

    d = await digest_service.generate(db, user_id=1)
    refreshed = await digests_repo.get(db, d.id)
    top = json.loads(refreshed.top_items_json)
    assert {t["video_id"] for t in top} == {"v1"}


@pytest.mark.asyncio
async def test_generate_marks_failed_on_invalid_json(db, monkeypatch):
    await _default_llm(db)
    await _video_with_highlights(db, "v1", [{"text": "a", "rank": 1, "reason": "y"}])
    fake_llm = AsyncMock(return_value="not valid json at all")
    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)

    d = await digest_service.generate(db, user_id=1)
    refreshed = await digests_repo.get(db, d.id)
    assert refreshed.status == DigestStatus.FAILED
    assert refreshed.error is not None


@pytest.mark.asyncio
async def test_generate_scopes_pool_per_user(db, monkeypatch):
    await _default_llm(db)
    await videos_repo.upsert_metadata(
        db, video_id="vA", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    # Pretend this video belongs to user 2:
    await db.execute("UPDATE videos SET user_id=2 WHERE id='vA'")
    await db.commit()
    await videos_repo.set_highlights(
        db, "vA", '[{"text":"x","rank":1,"reason":"y"}]',
    )

    fake_llm = AsyncMock()
    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)

    d = await digest_service.generate(db, user_id=1)
    refreshed = await digests_repo.get(db, d.id)
    assert refreshed.item_count == 0
    fake_llm.assert_not_called()


async def test_compute_window_no_previous_digest_caps_96h(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert end == now
    assert start == now - timedelta(hours=96)


async def test_compute_window_resumes_after_last_digest(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=30),
        period_end=now - timedelta(hours=6),
    )
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert start == now - timedelta(hours=6)
    assert end == now


async def test_compute_window_caps_stale_last_digest(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=300),
        period_end=now - timedelta(hours=200),
    )
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert start == now - timedelta(hours=96)


async def test_compute_window_ignores_failed_digests(db):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=10),
        period_end=now - timedelta(hours=2),
    )
    await digests_repo.mark_failed(db, digest_id=d.id, error="boom")
    start, end = await digest_service.compute_window(db, user_id=1, now=now)
    assert start == now - timedelta(hours=96)


async def _seed_video(
    db, video_id, *, hours_ago=1, highlights='[{"text": "x", "rank": 1}]',
    user_id=1,
):
    created = (
        datetime.now(UTC) - timedelta(hours=hours_ago)
    ).replace(microsecond=0)
    await db.execute(
        "INSERT INTO videos (id, user_id, url, title, highlights_json,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (video_id, user_id, "u", f"Title {video_id}", highlights,
         created.isoformat()),
    )
    await db.commit()


async def test_gather_pool_restricts_to_video_ids(db):
    await _seed_video(db, "v1")
    await _seed_video(db, "v2")
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start, video_ids=["v1"],
    )
    assert [i["video_id"] for i in pool] == ["v1"]


async def test_gather_pool_empty_selection_returns_empty(db):
    await _seed_video(db, "v1")
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start, video_ids=[],
    )
    assert pool == []


async def test_gather_pool_without_ids_keeps_old_behavior(db):
    await _seed_video(db, "v1")
    await _seed_video(db, "v2")
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start,
    )
    assert {i["video_id"] for i in pool} == {"v1", "v2"}


async def test_list_candidates_splits_eligible_and_missing(db):
    await _seed_video(db, "v1", hours_ago=1)
    await _seed_video(db, "v2", hours_ago=2, highlights=None)
    await _seed_video(db, "v3", hours_ago=200)  # outside window
    start = datetime.now(UTC) - timedelta(hours=96)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start,
    )
    assert [c["id"] for c in candidates] == ["v1"]
    assert candidates[0]["title"] == "Title v1"
    assert candidates[0]["kind"] == "youtube"
    assert missing == 1


async def test_list_candidates_scoped_by_user(db):
    await db.execute("INSERT INTO users (id, name) VALUES (2, 'other')")
    await db.commit()
    await _seed_video(db, "v9", user_id=2)
    start = datetime.now(UTC) - timedelta(hours=96)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start,
    )
    assert candidates == []
    assert missing == 0


async def test_run_for_existing_digest_uses_stored_selection(
    db, monkeypatch,
):
    """The background job must honour the selection persisted on the
    digest row — not re-derive the pool from the whole window."""
    await _seed_video(db, "v1")
    await _seed_video(db, "v2")
    now = datetime.now(UTC).replace(microsecond=0)
    d = await digests_repo.create_pending(
        db, user_id=1,
        period_start=now - timedelta(hours=96), period_end=now,
        selected_video_ids_json='["v1"]',
    )

    seen_pools = []

    async def fake_llm(**kwargs):
        seen_pools.append(kwargs["payload"])
        return (
            '{"tldr": "ok", "top_items": '
            '[{"video_id": "v1", "rank": 1, "hook": "h", "reason": "r"}]}'
        )

    monkeypatch.setattr(digest_service, "_call_digest_llm", fake_llm)
    await _default_llm(db)

    got = await digest_service.run_for_existing_digest(
        db, digest_id=d.id, user_id=1,
    )
    assert got.status.value == "ready"
    assert "v1" in seen_pools[0]
    assert "v2" not in seen_pools[0]


async def test_list_candidates_respects_period_end_upper_bound(db):
    await _seed_video(db, "v1", hours_ago=5)
    await _seed_video(db, "v2", hours_ago=1)
    start = datetime.now(UTC) - timedelta(hours=96)
    end = datetime.now(UTC) - timedelta(hours=3)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start, period_end=end,
    )
    assert [c["id"] for c in candidates] == ["v1"]
    assert missing == 0


async def test_gather_pool_excludes_archived(db):
    await _seed_video(db, "v1")
    await db.execute(
        "UPDATE videos SET archived_at=datetime('now') WHERE id='v1'"
    )
    await db.commit()
    start = datetime.now(UTC) - timedelta(hours=96)
    pool = await digest_service._gather_pool(
        db, user_id=1, period_start=start,
    )
    assert pool == []


async def test_list_candidates_excludes_archived(db):
    await _seed_video(db, "v1")
    await db.execute(
        "UPDATE videos SET archived_at=datetime('now') WHERE id='v1'"
    )
    await db.commit()
    start = datetime.now(UTC) - timedelta(hours=96)
    candidates, missing = await digest_service.list_candidates(
        db, user_id=1, period_start=start,
    )
    assert candidates == []
    assert missing == 0
