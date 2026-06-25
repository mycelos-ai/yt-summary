import asyncio

from app.repos import settings as settings_repo
from app.services import seed


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
        assert await settings_repo.get(db, "known_shows_seed_version") == "7"
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
        assert await settings_repo.get(db, "known_speakers_seed_version") == "9"
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


def test_seed_speakers_carries_curated_photo_paths(db):
    async def go():
        await seed.seed_known_speakers(db)
        cur = await db.execute(
            "SELECT avatar_photo_path, style_note FROM known_speakers "
            "WHERE name_key='steven bartlett'"
        )
        row = await cur.fetchone()
        assert row is not None
        # After the fix, seed photos are stored as relative static paths
        assert row["avatar_photo_path"] == "podcasters/steven-bartlett.png", (
            f"Expected relative static path, got: {row['avatar_photo_path']!r}"
        )
        assert "reflective interviewer" in row["style_note"]

    _run(go())


def test_seed_photo_stored_as_relative_static_path(db):
    """Seed photos (app/static/podcasters/*.png) must be stored as relative
    web paths ('podcasters/x.png'), NOT as absolute filesystem paths."""
    async def go():
        await seed.seed_known_speakers(db)
        cur = await db.execute(
            "SELECT avatar_photo_path FROM known_speakers "
            "WHERE name_key='chamath palihapitiya'"
        )
        row = await cur.fetchone()
        assert row is not None
        path = row["avatar_photo_path"]
        # Must be the relative web path — no leading slash, no filesystem prefix
        assert path == "podcasters/chamath-palihapitiya.png", (
            f"Expected relative static path, got: {path!r}"
        )
        # Must NOT contain any absolute-path components
        assert not path.startswith("/"), f"Path must not be absolute: {path!r}"
        assert "app/static" not in path, f"Path must not contain app/static: {path!r}"
        assert "/Users/" not in path, f"Path must not be a host path: {path!r}"
        assert path.count("/") == 1, f"Should be exactly one slash (subdir/file): {path!r}"
    _run(go())


def test_seed_links_and_photographs_unlinked_speaker(db):
    """An existing speakers row with known_speaker_id=NULL but matching name_key
    must get known_speaker_id backfilled AND avatar_photo_path set to the relative
    static path on re-seed."""
    async def go():
        # First seed known_speakers so the catalog rows exist
        await seed.seed_known_speakers(db)

        # Insert a profile speaker simulating show-match detection: name matches
        # Chamath but known_speaker_id is NULL (not yet linked), no photo
        await db.execute(
            "INSERT INTO speakers (user_id, name, name_key, avatar_photo_path) "
            "VALUES (1, 'Chamath Palihapitiya', 'chamath palihapitiya', NULL)"
        )
        # Force re-seed by deleting the version marker
        await db.execute(
            "DELETE FROM settings WHERE key='known_speakers_seed_version'"
        )
        await db.commit()

        # Re-run the seed loader
        await seed.seed_known_speakers(db)

        # The un-linked speaker must now have known_speaker_id and avatar_photo_path set
        cur = await db.execute(
            "SELECT known_speaker_id, avatar_photo_path FROM speakers "
            "WHERE name_key='chamath palihapitiya' AND user_id=1"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["known_speaker_id"] is not None, (
            "known_speaker_id must be backfilled by name_key match"
        )
        assert row["avatar_photo_path"] == "podcasters/chamath-palihapitiya.png", (
            f"Expected relative static path, got: {row['avatar_photo_path']!r}"
        )
    _run(go())


def test_reseed_replaces_stale_absolute_seed_photo_path(db):
    """A speakers row with a stale ABSOLUTE seed path (contains 'podcasters/')
    must get overwritten with the new relative path on re-seed.
    A stale path looks like '/Users/old/app/static/podcasters/chamath.png' or
    '/app/app/static/podcasters/chamath.png' — both contain 'podcasters/'."""
    async def go():
        # Seed so known_speakers rows exist
        await seed.seed_known_speakers(db)

        # Find the known_speaker id for Chamath
        cur = await db.execute(
            "SELECT id FROM known_speakers WHERE name_key='chamath palihapitiya'"
        )
        known_id = (await cur.fetchone())["id"]

        # Insert a profile speaker with a STALE ABSOLUTE seed path
        stale_path = "/Users/old/app/static/podcasters/chamath-palihapitiya.png"
        await db.execute(
            "INSERT INTO speakers "
            "(user_id, known_speaker_id, name, name_key, avatar_photo_path) "
            "VALUES (1, ?, 'Chamath Palihapitiya', 'chamath palihapitiya', ?)",
            (known_id, stale_path),
        )
        await db.execute(
            "DELETE FROM settings WHERE key='known_speakers_seed_version'"
        )
        await db.commit()

        # Re-seed
        await seed.seed_known_speakers(db)

        cur = await db.execute(
            "SELECT avatar_photo_path FROM speakers "
            "WHERE name_key='chamath palihapitiya' AND user_id=1"
        )
        row = await cur.fetchone()
        assert row["avatar_photo_path"] == "podcasters/chamath-palihapitiya.png", (
            f"Stale absolute seed path was not overwritten; got: {row['avatar_photo_path']!r}"
        )
    _run(go())


def test_reseed_preserves_manual_upload_photo(db):
    """A speakers row with a manual upload path (contains 'speaker_photos',
    NOT 'podcasters/') must NOT be overwritten on re-seed."""
    async def go():
        # Seed so known_speakers rows exist
        await seed.seed_known_speakers(db)

        cur = await db.execute(
            "SELECT id FROM known_speakers WHERE name_key='chamath palihapitiya'"
        )
        known_id = (await cur.fetchone())["id"]

        # Insert a profile speaker with a manual upload path
        manual_path = "/data/speaker_photos/2.jpg"
        await db.execute(
            "INSERT INTO speakers "
            "(user_id, known_speaker_id, name, name_key, avatar_photo_path) "
            "VALUES (1, ?, 'Chamath Palihapitiya', 'chamath palihapitiya', ?)",
            (known_id, manual_path),
        )
        await db.execute(
            "DELETE FROM settings WHERE key='known_speakers_seed_version'"
        )
        await db.commit()

        # Re-seed
        await seed.seed_known_speakers(db)

        cur = await db.execute(
            "SELECT avatar_photo_path FROM speakers "
            "WHERE name_key='chamath palihapitiya' AND user_id=1"
        )
        row = await cur.fetchone()
        assert row["avatar_photo_path"] == manual_path, (
            f"Manual upload path was overwritten; got: {row['avatar_photo_path']!r}"
        )
    _run(go())


def test_seed_speakers_does_not_overwrite_existing_profile_photo(db):
    async def go():
        await seed.seed_known_speakers(db)
        cur = await db.execute(
            "SELECT id FROM known_speakers WHERE name_key='lex fridman'"
        )
        known_id = (await cur.fetchone())["id"]
        await db.execute(
            "INSERT INTO speakers "
            "(user_id, known_speaker_id, name, name_key, avatar_photo_path) "
            "VALUES (1, ?, 'Lex Fridman', 'lex fridman', '/custom/lex.png')",
            (known_id,),
        )
        await db.execute(
            "DELETE FROM settings WHERE key='known_speakers_seed_version'"
        )
        await db.commit()

        await seed.seed_known_speakers(db)

        cur = await db.execute(
            "SELECT avatar_photo_path FROM speakers WHERE name_key='lex fridman'"
        )
        row = await cur.fetchone()
        assert row["avatar_photo_path"] == "/custom/lex.png"

    _run(go())
