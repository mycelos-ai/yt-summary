import asyncio

import pytest

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


def test_speaker_thread_null_source_matches_on_second_call(db):
    """
    Regression test: speaker scope threads are matched by speaker_id alone.
    A thread created with source_id=None must be found by a second call that
    omits source_id (defaults to None), not duplicated.
    """
    async def go():
        await _seed(db)
        # First call: explicit source_id=None
        a = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1, source_id=None)

        # Verify stored row has source_id IS NULL
        cur = await db.execute(
            "SELECT source_id FROM chat_threads WHERE id=?", (a,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["source_id"] is None, \
            f"Expected source_id to be NULL but got {row['source_id']!r}"

        # Second call: omit source_id (relies on default None)
        b = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)

        # Should find the same thread (NULL matching works)
        assert a == b, \
            f"Expected same thread id {a}, got {b} — NULL matching failed"

        # Verify exactly one thread for this speaker scope
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM chat_threads WHERE scope='speaker' AND speaker_id=1"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["cnt"] == 1, \
            f"Expected 1 speaker thread, found {row['cnt']} — duplicate created"
    _run(go())


def test_source_thread_null_speaker_matches_on_second_call(db):
    """
    Regression test: source scope threads are matched by source_id alone.
    A thread created with speaker_id=None must be found by a second call that
    omits speaker_id (defaults to None), not duplicated.
    """
    async def go():
        await _seed(db)
        # First call: explicit speaker_id=None
        a = await ct_repo.get_or_create(db, scope="source", source_id="v1", speaker_id=None)

        # Verify stored row has speaker_id IS NULL
        cur = await db.execute(
            "SELECT speaker_id FROM chat_threads WHERE id=?", (a,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["speaker_id"] is None, \
            f"Expected speaker_id to be NULL but got {row['speaker_id']!r}"

        # Second call: omit speaker_id (relies on default None)
        b = await ct_repo.get_or_create(db, scope="source", source_id="v1")

        # Should find the same thread (NULL matching works)
        assert a == b, \
            f"Expected same thread id {a}, got {b} — NULL matching failed"

        # Verify exactly one thread for this source scope
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM chat_threads WHERE scope='source' AND source_id='v1'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["cnt"] == 1, \
            f"Expected 1 source thread, found {row['cnt']} — duplicate created"
    _run(go())


# ── Shape-validation guard tests ──────────────────────────────────────────────

def test_get_or_create_rejects_speaker_scope_without_speaker_id(db):
    """scope='speaker' with speaker_id=None must raise ValueError and insert no row."""
    async def go():
        with pytest.raises(ValueError, match="speaker"):
            await ct_repo.get_or_create(db, scope="speaker", speaker_id=None)
        cur = await db.execute("SELECT COUNT(*) AS cnt FROM chat_threads")
        row = await cur.fetchone()
        assert row["cnt"] == 0, "orphan row must not be created"
    _run(go())


def test_get_or_create_rejects_source_scope_without_source_id(db):
    """scope='source' with source_id=None must raise ValueError and insert no row."""
    async def go():
        with pytest.raises(ValueError, match="source"):
            await ct_repo.get_or_create(db, scope="source", source_id=None)
        cur = await db.execute("SELECT COUNT(*) AS cnt FROM chat_threads")
        row = await cur.fetchone()
        assert row["cnt"] == 0, "orphan row must not be created"
    _run(go())


def test_get_or_create_rejects_source_speaker_missing_id(db):
    """scope='source_speaker' with either id None must raise ValueError."""
    async def go():
        # missing speaker_id
        with pytest.raises(ValueError, match="source_speaker"):
            await ct_repo.get_or_create(db, scope="source_speaker", source_id="v1", speaker_id=None)
        # missing source_id
        with pytest.raises(ValueError, match="source_speaker"):
            await ct_repo.get_or_create(db, scope="source_speaker", source_id=None, speaker_id=1)
        cur = await db.execute("SELECT COUNT(*) AS cnt FROM chat_threads")
        row = await cur.fetchone()
        assert row["cnt"] == 0, "orphan row must not be created"
    _run(go())


def test_get_or_create_valid_shapes_still_work(db):
    """All three valid shapes create exactly one row and are idempotent."""
    async def go():
        await _seed(db)
        # scope='source'
        a1 = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        a2 = await ct_repo.get_or_create(db, scope="source", source_id="v1")
        assert a1 == a2

        # scope='speaker'
        b1 = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)
        b2 = await ct_repo.get_or_create(db, scope="speaker", speaker_id=1)
        assert b1 == b2

        # scope='source_speaker'
        c1 = await ct_repo.get_or_create(db, scope="source_speaker", source_id="v1", speaker_id=1)
        c2 = await ct_repo.get_or_create(db, scope="source_speaker", source_id="v1", speaker_id=1)
        assert c1 == c2

        # three distinct threads
        assert len({a1, b1, c1}) == 3

        cur = await db.execute("SELECT COUNT(*) AS cnt FROM chat_threads")
        row = await cur.fetchone()
        assert row["cnt"] == 3
    _run(go())
