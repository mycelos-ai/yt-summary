import asyncio
from app.services import show_match
from app.models import Video, VideoKind


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def _video(**kw):
    base = dict(id="v1", user_id=1, kind=VideoKind.YOUTUBE, url="", title="", description="")
    base.update(kw)
    return Video(**base)


def test_parse_guest_after():
    assert show_match._parse_guest("after:with ", "Money with Morgan Housel") == "Morgan Housel"


def test_parse_guest_before():
    assert show_match._parse_guest("before:: ", "Elon Musk: Mars | Lex #1") == "Elon Musk"


def test_identify_matches_channel_and_parses_guest(db):
    async def go():
        await db.execute(
            "INSERT INTO known_shows (user_id, name, channel_id, hosts_json, guest_rule, enabled) "
            "VALUES (NULL, 'Lex Fridman Podcast', 'UCchan', '[\"Lex Fridman\"]', 'before:: ', 1)"
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
