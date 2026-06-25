import asyncio
from datetime import UTC, datetime

from app.models import Video, VideoKind
from app.services import show_match


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def _video(**kw):
    ts = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    base = dict(
        id="v1",
        url="",
        title="",
        description="",
        thumbnail_path=None,
        duration_seconds=None,
        transcript=None,
        transcript_source=None,
        summary=None,
        summary_model=None,
        created_at=ts,
        updated_at=ts,
        kind=VideoKind.YOUTUBE,
        user_id=1,
        transcript_segments=None,
        youtube_id=None,
        source_language=None,
        summary_language=None,
        transcript_language=None,
        highlights_json=None,
        archived_at=None,
        image_query=None,
        related_links_json=None,
    )
    base.update(kw)
    return Video(**base)


def test_parse_guest_after():
    assert show_match._parse_guest("after:with ", "Money with Morgan Housel") == "Morgan Housel"


def test_parse_guest_before():
    assert show_match._parse_guest("before:: ", "Elon Musk: Mars | Lex #1") == "Elon Musk"


def test_identify_matches_channel_and_parses_guest(db):
    async def go():
        # Upsert so this works both on a fresh DB and after seeding has
        # already inserted "Lex Fridman Podcast" (seeding happens in init_schema
        # via seed.seed_known_shows). Use the same partial-index conflict target.
        await db.execute(
            "INSERT INTO known_shows (user_id, name, channel_id, hosts_json, guest_rule, enabled) "
            "VALUES (NULL, 'Lex Fridman Podcast', 'UCchan', '[\"Lex Fridman\"]', 'before:: ', 1) "
            "ON CONFLICT(name) WHERE user_id IS NULL DO UPDATE SET "
            "channel_id=excluded.channel_id, hosts_json=excluded.hosts_json, "
            "guest_rule=excluded.guest_rule"
        )
        await db.commit()
        v = _video(channel_id="UCchan", title="Elon Musk: Mars | Lex Fridman Podcast #1")
        out = await show_match.identify_from_metadata(db, v)
        names = {d.name for d in out}
        assert "Lex Fridman" in names and "Elon Musk" in names
        host = next(d for d in out if d.name == "Lex Fridman")
        assert host.is_host is True
    _run(go())


def test_identify_no_match_returns_empty(db):
    async def go():
        v = _video(channel_id="UNKNOWN", title="random")
        assert await show_match.identify_from_metadata(db, v) == []
    _run(go())
