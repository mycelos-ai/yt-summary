from unittest.mock import AsyncMock, patch

from app.config import Config
from app.repos import videos as videos_repo


async def test_get_video_resource_returns_dict(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await videos_repo.upsert_metadata(
        db, video_id="vapi1", url="https://x", title="T",
        description="d", thumbnail_path=None, duration_seconds=300,
    )
    from app.services.api import get_video_resource
    resource = await get_video_resource(db, "vapi1")
    assert resource is not None
    assert resource["id"] == "vapi1"
    assert resource["title"] == "T"
    assert resource["summary_ready"] is False
    assert resource["kind"] == "youtube"
    assert resource["url"] == "https://x"
    assert resource["thumbnail_url"] is None  # no file on disk


async def test_get_video_resource_returns_none_for_unknown(db, tmp_path):
    from app.services.api import get_video_resource
    assert await get_video_resource(db, "nope") is None


async def test_list_videos_paginates(db, tmp_path):
    for i in range(5):
        await videos_repo.upsert_metadata(
            db, video_id=f"vapi{i}", url="u", title=f"V{i}",
            description="", thumbnail_path=None, duration_seconds=None,
        )
    from app.services.api import list_videos
    page1 = await list_videos(db, limit=2, offset=0)
    page2 = await list_videos(db, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {v["id"] for v in page1}.isdisjoint({v["id"] for v in page2})


async def test_submit_video_async_returns_pending(db, tmp_path):
    from app.services.api import submit_video
    from app.services.youtube import VideoMetadata

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    fake_meta = VideoMetadata(
        id="newvid12345",
        url="https://youtu.be/newvid12345",
        title="New",
        description="",
        duration_seconds=120,
        thumbnail_url=None,
    )

    with (
        patch("app.services.api.fetch_metadata", AsyncMock(return_value=fake_meta)),
        patch("app.services.api.download_thumbnail", AsyncMock(return_value=None)),
    ):
        result = await submit_video(
            db, config, url="https://youtu.be/newvid12345",
            user_id=1, wait=False, wait_timeout=0,
        )
    # Composite id under the seeded admin (user 1)
    assert result["id"] == "1:newvid12345"
    assert result["summary_ready"] is False
    assert result["kind"] == "youtube"


async def test_list_tags_returns_counts(db, tmp_path):
    from app.repos import tags as tags_repo
    await videos_repo.upsert_metadata(
        db, video_id="t1", url="u", title="t1",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="t2", url="u", title="t2",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await tags_repo.set_tags_for_video(db, "t1", ["python"])
    await tags_repo.set_tags_for_video(db, "t2", ["python", "fastapi"])

    from app.services.api import list_tags
    result = await list_tags(db)
    by_name = {t["name"]: t["count"] for t in result}
    assert by_name["python"] == 2
    assert by_name["fastapi"] == 1


async def test_list_playlists_resource_shape(db, tmp_path):
    from app.repos import playlists as playlists_repo
    await playlists_repo.create(
        db, playlist_id="PLapi1", user_id=1, url="u",
        title="Show", description="", thumbnail_path=None,
    )
    from app.services.api import list_playlists
    rows = await list_playlists(db, user_id=1)
    assert len(rows) == 1
    assert rows[0]["id"] == "PLapi1"
    assert rows[0]["title"] == "Show"
    assert rows[0]["video_count"] == 0
