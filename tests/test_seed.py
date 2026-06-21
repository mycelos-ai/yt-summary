import asyncio

import pytest

from app.services import seed
from app.repos import settings as settings_repo


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_seed_shows_idempotent(db):
    async def go():
        await seed.seed_known_shows(db)
        await seed.seed_known_shows(db)   # second call must not duplicate
        cur = await db.execute("SELECT COUNT(*) FROM known_shows WHERE user_id IS NULL")
        n = (await cur.fetchone())[0]
        assert n >= 3
        # marker set
        assert await settings_repo.get(db, "known_shows_seed_version") == "1"
        # no duplication
        cur = await db.execute(
            "SELECT name, COUNT(*) c FROM known_shows GROUP BY name HAVING c>1"
        )
        assert await cur.fetchone() is None
    _run(go())


def test_seed_speakers_idempotent(db):
    async def go():
        await seed.seed_known_speakers(db)
        await seed.seed_known_speakers(db)
        cur = await db.execute("SELECT COUNT(*) FROM known_speakers")
        assert (await cur.fetchone())[0] >= 2
        # marker set
        assert await settings_repo.get(db, "known_speakers_seed_version") == "1"
        # no duplication by name_key
        cur = await db.execute(
            "SELECT name_key, COUNT(*) c FROM known_speakers GROUP BY name_key HAVING c>1"
        )
        assert await cur.fetchone() is None
    _run(go())


def test_reseed_does_not_break_with_linked_speaker(db):
    """After seeding, insert a profile speakers row with known_speaker_id pointing
    at a seeded known_speaker, then re-seed — must NOT raise FOREIGN KEY constraint
    failed. This is the bug that a wipe-based loader would trigger."""
    async def go():
        # Seed once so known_speakers rows exist
        await seed.seed_known_speakers(db)

        # Find the id of a seeded speaker (e.g. Lex Fridman)
        cur = await db.execute(
            "SELECT id FROM known_speakers WHERE name_key=?",
            ("lex fridman",),
        )
        row = await cur.fetchone()
        assert row is not None, "Lex Fridman should have been seeded"
        known_id = row[0]

        # Insert a profile speaker that links to this known_speaker_id
        await db.execute(
            "INSERT INTO speakers (user_id, known_speaker_id, name, name_key) "
            "VALUES (1, ?, 'Lex Fridman', 'lex fridman')",
            (known_id,),
        )
        await db.commit()

        # Bump the version marker so the seeder will actually run again
        await db.execute(
            "DELETE FROM settings WHERE key='known_speakers_seed_version'"
        )
        await db.commit()

        # Re-seed — must NOT raise FOREIGN KEY constraint failed
        await seed.seed_known_speakers(db)

        # The profile speaker's FK should still be valid
        cur = await db.execute(
            "SELECT known_speaker_id FROM speakers WHERE name_key='lex fridman'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == known_id
    _run(go())
