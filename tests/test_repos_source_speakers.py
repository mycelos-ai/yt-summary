import asyncio

from app.repos import source_speakers as ss_repo
from app.repos import speakers as sp_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed_video(db, vid="v1"):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) "
        "VALUES (?, 1, 'youtube', 'u', 't')",
        (vid,),
    )
    await db.commit()


def test_link_is_idempotent_and_lists_speaker(db):
    async def go():
        await _seed_video(db)
        sid = await sp_repo.resolve_speaker(db, name="Chamath Palihapitiya")
        first = await ss_repo.link_speaker(
            db, "v1", sid, role="host", detection_source="show_rule"
        )
        again = await ss_repo.link_speaker(
            db, "v1", sid, role="host", detection_source="show_rule"
        )
        assert first == again  # UNIQUE(source_id, speaker_id) → same row, no dupe
        people = await ss_repo.list_for_source(db, "v1")
        assert [p.name for p in people] == ["Chamath Palihapitiya"]
        cur = await db.execute(
            "SELECT COUNT(*) FROM source_speakers WHERE source_id='v1'"
        )
        assert (await cur.fetchone())[0] == 1
    _run(go())


def test_unlink_removes_only_that_link(db):
    async def go():
        await _seed_video(db)
        a = await sp_repo.resolve_speaker(db, name="Jason Calacanis")
        b = await sp_repo.resolve_speaker(db, name="David Sacks")
        await ss_repo.link_speaker(db, "v1", a, detection_source="manual")
        await ss_repo.link_speaker(db, "v1", b, detection_source="manual")
        await ss_repo.unlink(db, "v1", a)
        people = await ss_repo.list_for_source(db, "v1")
        assert [p.name for p in people] == ["David Sacks"]
    _run(go())


def test_list_orders_by_sort_order(db):
    async def go():
        await _seed_video(db)
        a = await sp_repo.resolve_speaker(db, name="Beta")
        b = await sp_repo.resolve_speaker(db, name="Alpha")
        await ss_repo.link_speaker(db, "v1", a, detection_source="show_rule", sort_order=0)
        await ss_repo.link_speaker(db, "v1", b, detection_source="show_rule", sort_order=1)
        people = await ss_repo.list_for_source(db, "v1")
        assert [p.name for p in people] == ["Beta", "Alpha"]  # sort_order, then id
    _run(go())
