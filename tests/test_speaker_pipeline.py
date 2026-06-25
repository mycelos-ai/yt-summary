import asyncio
import json

from app.models import Video, VideoKind
from app.repos import source_speakers as ss_repo
from app.services import speaker_pipeline

ALLIN_CHANNEL_ID = "UCESLZhusAkFfsNsApnjF_Cg"
ALLIN_HOSTS = ["Chamath Palihapitiya", "Jason Calacanis", "David Sacks", "David Friedberg"]
ALLIN_TOPIC_TITLE = "OpenAI Misses Targets, Codex vs Claude, Elon vs Sam Trial"


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


async def _seed_allin_show(db):
    """Insert All-In Podcast known_shows row with real channel_id."""
    await db.execute(
        "INSERT INTO known_shows "
        "(user_id, name, channel_id, title_pattern, hosts_json, guest_rule, enabled) "
        "VALUES (NULL, 'All-In Podcast', ?, 'All-In', ?, NULL, 1) "
        "ON CONFLICT(name) WHERE user_id IS NULL DO UPDATE SET "
        "channel_id=excluded.channel_id, title_pattern=excluded.title_pattern, "
        "hosts_json=excluded.hosts_json",
        (ALLIN_CHANNEL_ID, json.dumps(ALLIN_HOSTS)),
    )
    await db.commit()


async def _seed_allin_video(db, vid="allin1", channel_id=ALLIN_CHANNEL_ID, title=None):
    t = title or ALLIN_TOPIC_TITLE
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title, channel_id, transcript) "
        "VALUES (?, 1, 'youtube', 'u', ?, ?, 'body')",
        (vid, t, channel_id),
    )
    await db.commit()


def _allin_video(*, vid="allin1", channel_id=ALLIN_CHANNEL_ID, title=None):
    t = title or ALLIN_TOPIC_TITLE
    return _video(
        id=vid,
        title=t,
        channel_id=channel_id,
        description="",
        transcript="body",
    )


# --- All-In detection tests (bug repro + fix proof) --------------------------

def test_allin_video_detects_via_channel_id(db):
    """channel_id match alone links all 4 hosts; topic-list title never has 'All-In'."""
    async def go():
        await _seed_allin_show(db)
        await _seed_allin_video(db)
        video = _allin_video()
        # Title does NOT contain "All-In" — only channel_id match should fire.
        assert "All-In" not in video.title, "test precondition: title must not contain 'All-In'"
        ids = await speaker_pipeline.detect_and_link(db, video)
        assert len(ids) == 4, f"expected 4 host ids, got {ids!r}"
        linked_names = {p.name for p in await ss_repo.list_for_source(db, "allin1")}
        assert linked_names == set(ALLIN_HOSTS), f"linked names mismatch: {linked_names!r}"
        # detection_source must be show_rule — query the join table directly
        cur = await db.execute(
            "SELECT DISTINCT detection_source FROM source_speakers WHERE source_id='allin1'"
        )
        sources = {r[0] for r in await cur.fetchall()}
        assert sources == {"show_rule"}, f"unexpected detection_source values: {sources!r}"
    _run(go())


def test_allin_video_no_detection_without_channel_id_or_title(db):
    """Bug repro: NULL channel_id + topic-list title (no 'All-In') yields no detection."""
    async def go():
        await _seed_allin_show(db)
        vid = "allin_null"
        await db.execute(
            "INSERT INTO videos (id, user_id, kind, url, title, channel_id, transcript) "
            "VALUES (?, 1, 'youtube', 'u', ?, NULL, 'body')",
            (vid, ALLIN_TOPIC_TITLE),
        )
        await db.commit()
        video = _allin_video(vid=vid, channel_id=None)
        ids = await speaker_pipeline.detect_and_link(db, video)
        assert ids == [], (
            f"expected no detection when channel_id=NULL and title has no 'All-In', got {ids!r}"
        )
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
