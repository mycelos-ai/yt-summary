import asyncio

from app.models import Video, VideoKind
from app.repos import source_speakers as ss_repo
from app.services import speaker_pipeline


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


# The db fixture already seeds known_shows via init_schema/seed_known_shows.
# "Lex Fridman Podcast" is in the seed with channel_id="UCSHZKyawb77ixDdsGog4iWA",
# title_pattern="Lex Fridman Podcast", and guest_rule="before:: ".
# We seed a video row to satisfy the FK from source_speakers.
async def _seed_video_row(db, vid="v1", channel_id="UCSHZKyawb77ixDdsGog4iWA"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, channel_id, transcript) "
        "VALUES (?, 1, 'youtube', 'u', 'Elon Musk: Mars | Lex Fridman Podcast #1', ?, 'body')",
        (vid, channel_id),
    )
    await db.commit()


def _video(**kw):
    base = dict(
        id="v1", user_id=1, kind=VideoKind.YOUTUBE, url="u",
        title="Elon Musk: Mars | Lex Fridman Podcast #1", description="",
        thumbnail_path=None, duration_seconds=None, transcript="body",
        transcript_source=None, summary="s", summary_model="m",
        created_at=None, updated_at=None,
        transcript_segments=None, youtube_id=None,
        source_language=None, summary_language=None, transcript_language=None,
        highlights_json=None, archived_at=None, image_query=None,
        related_links_json=None, channel_id="UCSHZKyawb77ixDdsGog4iWA",
    )
    base.update(kw)
    return Video(**base)


def test_detect_and_link_links_host_and_guest(db):
    async def go():
        await _seed_video_row(db)
        ids = await speaker_pipeline.detect_and_link(db, _video())
        assert len(ids) == 2
        people = {p.name for p in await ss_repo.list_for_source(db, "v1")}
        assert "Lex Fridman" in people and "Elon Musk" in people
    _run(go())


def test_detect_and_link_skips_non_youtube(db):
    async def go():
        # A web item must not be detected against show rules.
        v = _video(kind=VideoKind.WEB, channel_id=None)
        assert await speaker_pipeline.detect_and_link(db, v) == []
    _run(go())


def test_detect_and_link_skips_when_no_transcript(db):
    async def go():
        await _seed_video_row(db)
        v = _video(transcript=None)
        assert await speaker_pipeline.detect_and_link(db, v) == []
    _run(go())


def test_detect_and_link_swallows_errors(db):
    async def go():
        # video id "ghost" has no videos row → link_speaker's FK will
        # raise — detect_and_link must swallow it and return [].
        # The seeded "Lex Fridman Podcast" show matches by channel_id so
        # identify_from_metadata returns names, then link_speaker hits FK.
        v = _video(id="ghost")
        result = await speaker_pipeline.detect_and_link(db, v)
        assert result == []  # FK violation on 'ghost' swallowed
    _run(go())
