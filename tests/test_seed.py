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
        # marker set (version matches known_shows.json "version" field)
        assert await settings_repo.get(db, "known_shows_seed_version") == "2"
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


def test_reseed_sets_allin_channel_id(db):
    """Reseed must write channel_id='UCESLZhusAkFfsNsApnjF_Cg' onto the All-In row.
    This proves Part 1 of the speaker detection fix: the seed version bump triggers
    an ON CONFLICT DO UPDATE that patches the previously-NULL channel_id."""
    async def go():
        await seed.seed_known_shows(db)
        cur = await db.execute(
            "SELECT channel_id FROM known_shows "
            "WHERE name='All-In Podcast' AND user_id IS NULL"
        )
        row = await cur.fetchone()
        assert row is not None, "All-In Podcast must be seeded"
        assert row[0] == "UCESLZhusAkFfsNsApnjF_Cg", (
            f"All-In channel_id not set correctly: {row[0]!r}"
        )
    _run(go())


def test_reseed_updates_description_pattern(db):
    """Reseed must overwrite (or clear) description_pattern — the seed loader is
    the source of truth for that column, so a stale value must not survive a
    version bump."""
    async def go():
        # Seed once so the known_shows rows exist.
        await seed.seed_known_shows(db)

        # Manually corrupt the description_pattern on one seeded row.
        await db.execute(
            "UPDATE known_shows SET description_pattern='STALE' "
            "WHERE name='Lex Fridman Podcast' AND user_id IS NULL"
        )
        await db.commit()

        # Confirm the stale value is actually there.
        cur = await db.execute(
            "SELECT description_pattern FROM known_shows "
            "WHERE name='Lex Fridman Podcast' AND user_id IS NULL"
        )
        assert (await cur.fetchone())[0] == "STALE"

        # Force reseed by deleting the version marker.
        await db.execute(
            "DELETE FROM settings WHERE key='known_shows_seed_version'"
        )
        await db.commit()

        # Re-run the seed loader.
        await seed.seed_known_shows(db)

        # The seed entry for Lex Fridman Podcast has no description_pattern,
        # so the upsert must have written NULL — 'STALE' must be gone.
        cur = await db.execute(
            "SELECT description_pattern FROM known_shows "
            "WHERE name='Lex Fridman Podcast' AND user_id IS NULL"
        )
        assert (await cur.fetchone())[0] is None, (
            "Reseed did not overwrite stale description_pattern"
        )
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
