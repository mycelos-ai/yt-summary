import aiosqlite

from app.repos import tags as tags_repo
from app.repos import videos as videos_repo


async def _make_video(db: aiosqlite.Connection, vid: str) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
    )


async def test_upsert_tag_returns_id(db: aiosqlite.Connection):
    tid = await tags_repo.upsert_tag(db, "python")
    assert isinstance(tid, int)
    tid2 = await tags_repo.upsert_tag(db, "python")
    assert tid == tid2


async def test_upsert_tag_is_case_insensitive(db: aiosqlite.Connection):
    a = await tags_repo.upsert_tag(db, "Python")
    b = await tags_repo.upsert_tag(db, "PYTHON")
    c = await tags_repo.upsert_tag(db, "python")
    assert a == b == c


async def test_upsert_tag_rejects_empty(db: aiosqlite.Connection):
    import pytest
    with pytest.raises(ValueError):
        await tags_repo.upsert_tag(db, "   ")


async def test_set_tags_creates_links(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await tags_repo.set_tags_for_video(db, "v1", ["a", "b", "c"])
    tags = await tags_repo.tags_for_video(db, "v1")
    assert sorted(tags) == ["a", "b", "c"]


async def test_set_tags_replaces_existing(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await tags_repo.set_tags_for_video(db, "v1", ["a", "b"])
    await tags_repo.set_tags_for_video(db, "v1", ["c", "d"])
    tags = await tags_repo.tags_for_video(db, "v1")
    assert sorted(tags) == ["c", "d"]


async def test_set_tags_filters_blanks_and_duplicates(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await tags_repo.set_tags_for_video(db, "v1", ["a", "  ", "A", "b", "B  "])
    tags = await tags_repo.tags_for_video(db, "v1")
    # "a" and "A" collapse (case-insensitive); "b" and "B" likewise
    assert len(tags) == 2


async def test_set_tags_empty_list_clears(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await tags_repo.set_tags_for_video(db, "v1", ["a"])
    await tags_repo.set_tags_for_video(db, "v1", [])
    assert await tags_repo.tags_for_video(db, "v1") == []


async def test_tags_for_videos_batch(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await _make_video(db, "v3")
    await tags_repo.set_tags_for_video(db, "v1", ["python", "web"])
    await tags_repo.set_tags_for_video(db, "v2", ["python"])
    # v3 has none

    result = await tags_repo.tags_for_videos(db, ["v1", "v2", "v3"])
    assert sorted(result["v1"]) == ["python", "web"]
    assert result["v2"] == ["python"]
    assert "v3" not in result


async def test_tags_for_videos_empty_input(db):
    assert await tags_repo.tags_for_videos(db, []) == {}


async def test_videos_repo_list_recent_filters_by_tag(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await tags_repo.set_tags_for_video(db, "v1", ["python"])
    await tags_repo.set_tags_for_video(db, "v2", ["cooking"])
    rows = await videos_repo.list_recent(db, tag="python")
    assert [v.id for v in rows] == ["v1"]
    rows_other = await videos_repo.list_recent(db, tag="cooking")
    assert [v.id for v in rows_other] == ["v2"]
    rows_unknown = await videos_repo.list_recent(db, tag="nope")
    assert rows_unknown == []


async def test_videos_repo_list_recent_tag_is_case_insensitive(db: aiosqlite.Connection):
    await _make_video(db, "v1")
    await tags_repo.set_tags_for_video(db, "v1", ["Python"])
    rows = await videos_repo.list_recent(db, tag="python")
    assert [v.id for v in rows] == ["v1"]
    rows = await videos_repo.list_recent(db, tag="PYTHON")
    assert [v.id for v in rows] == ["v1"]


async def test_videos_repo_search_filters_by_tag(db: aiosqlite.Connection):
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u",
        title="Python guide", description="learn",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="v2", url="u",
        title="Cooking guide", description="learn",
        thumbnail_path=None, duration_seconds=None,
    )
    await tags_repo.set_tags_for_video(db, "v1", ["python"])
    await tags_repo.set_tags_for_video(db, "v2", ["cooking"])

    # FTS hits both for "guide"
    all_rows = await videos_repo.search(db, "guide")
    assert {v.id for v in all_rows} == {"v1", "v2"}

    # With tag filter, only v1
    py_rows = await videos_repo.search(db, "guide", tag="python")
    assert [v.id for v in py_rows] == ["v1"]
