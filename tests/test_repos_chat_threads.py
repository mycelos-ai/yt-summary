import asyncio

from app.repos import chat_threads as ct_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


async def _seed(db):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) "
        "VALUES ('v1', 1, 'youtube', 'u', 't')"
    )
    await db.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
    await db.commit()


def test_source_thread_is_stable(db):
    async def go():
        await _seed(db)
        a = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        b = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        assert a == b  # partial unique index → one thread per (user, source)
    _run(go())


def test_speaker_thread_is_stable(db):
    async def go():
        await _seed(db)
        a = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)
        b = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)
        assert a == b
    _run(go())


def test_source_speaker_thread_distinct_from_source(db):
    async def go():
        await _seed(db)
        s = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        sp = await ct_repo.get_or_create(
            db, scope="source_speaker", source_id="v1", speaker_id=1
        )
        assert s != sp
    _run(go())
