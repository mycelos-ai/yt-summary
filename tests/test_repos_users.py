import aiosqlite

from app.config import Config
from app.repos import tts_jobs as tts_jobs_repo
from app.repos import users as users_repo
from app.repos import videos as videos_repo


async def test_get_default_user_returns_seeded_user(db: aiosqlite.Connection):
    user = await users_repo.get_default_user(db)
    assert user is not None
    assert user.id == 1
    assert user.name == "admin"
    assert user.api_key_hash is None


async def test_set_api_key_persists_hash_and_prefix(db: aiosqlite.Connection):
    await users_repo.set_api_key(
        db, user_id=1, key_hash="sha256-of-key", key_prefix="yts_xQ4f"
    )
    user = await users_repo.get_default_user(db)
    assert user is not None
    assert user.api_key_hash == "sha256-of-key"
    assert user.api_key_prefix == "yts_xQ4f"
    assert user.api_key_created_at is not None


async def test_clear_api_key_resets_fields(db: aiosqlite.Connection):
    await users_repo.set_api_key(
        db, user_id=1, key_hash="h", key_prefix="p"
    )
    await users_repo.clear_api_key(db, user_id=1)
    user = await users_repo.get_default_user(db)
    assert user is not None
    assert user.api_key_hash is None
    assert user.api_key_prefix is None
    assert user.api_key_created_at is None


async def test_find_by_api_key_hash_returns_user(db: aiosqlite.Connection):
    await users_repo.set_api_key(
        db, user_id=1, key_hash="hash-aaa", key_prefix="yts_aaaa"
    )
    found = await users_repo.find_by_api_key_hash(db, "hash-aaa")
    assert found is not None
    assert found.id == 1


async def test_find_by_api_key_hash_returns_none_for_unknown(db: aiosqlite.Connection):
    found = await users_repo.find_by_api_key_hash(db, "no-such-hash")
    assert found is None


async def test_delete_user_removes_tts_audio_files(
    db: aiosqlite.Connection, config: Config
):
    """users_repo.delete must also unlink TTS MP3s for videos owned by
    the deleted profile, otherwise the files become orphaned on disk
    after the FK cascade nukes the tts_jobs rows."""
    user = await users_repo.create(db, name="Bob")
    await videos_repo.upsert_metadata(
        db,
        video_id="vidu",
        url="https://yt/vidu",
        title="T",
        description="",
        thumbnail_path=None,
        duration_seconds=60,
        user_id=user.id,
    )
    j = await tts_jobs_repo.enqueue(
        db, "vidu", "summary", "de", "thorsten", "medium"
    )
    mp3 = config.tts_audio_dir / "vidu" / "summary-de-thorsten-medium.mp3"
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"x")
    await tts_jobs_repo.complete(
        db,
        j.id,
        audio_path=str(mp3.relative_to(config.data_dir)),
        duration_seconds=10.0,
        translated_text=None,
    )

    await users_repo.delete(db, user.id, data_dir=config.data_dir)

    assert not mp3.exists()
    assert not (config.tts_audio_dir / "vidu").exists()


async def test_set_and_get_interest_profile(db: aiosqlite.Connection):
    # User 1 is seeded by init_schema (default profile).
    await users_repo.set_interest_profile(
        db, user_id=1, markdown="my interests", expected_version=0,
    )
    md, version = await users_repo.get_interest_profile(db, user_id=1)
    assert md == "my interests"
    assert version == 1


async def test_interest_profile_optimistic_lock_conflict(db: aiosqlite.Connection):
    await users_repo.set_interest_profile(
        db, user_id=1, markdown="v1", expected_version=0,
    )
    # Second writer thinks the profile is still at version 0 → conflict.
    ok = await users_repo.set_interest_profile(
        db, user_id=1, markdown="v2", expected_version=0,
    )
    assert ok is False
    md, version = await users_repo.get_interest_profile(db, user_id=1)
    assert md == "v1"
    assert version == 1


async def test_set_digest_prefs(db: aiosqlite.Connection):
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=True, digest_hour_local=8,
    )
    prefs = await users_repo.get_digest_prefs(db, user_id=1)
    assert prefs == (True, 8)


async def test_set_digest_prefs_round_trips_false(db: aiosqlite.Connection):
    # Verify the False path: 1→0 SQLite write then 0→False Python read.
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=True, digest_hour_local=10,
    )
    await users_repo.set_digest_prefs(
        db, user_id=1, digest_enabled=False, digest_hour_local=10,
    )
    enabled, hour = await users_repo.get_digest_prefs(db, user_id=1)
    assert enabled is False
    assert hour == 10


async def test_set_digest_prefs_rejects_out_of_range_hour(
    db: aiosqlite.Connection,
):
    import pytest

    with pytest.raises(ValueError):
        await users_repo.set_digest_prefs(
            db, user_id=1, digest_enabled=True, digest_hour_local=24,
        )
    with pytest.raises(ValueError):
        await users_repo.set_digest_prefs(
            db, user_id=1, digest_enabled=True, digest_hour_local=-1,
        )


async def test_get_interest_profile_returns_defaults_for_missing_user(
    db: aiosqlite.Connection,
):
    # User 999 doesn't exist.
    md, version = await users_repo.get_interest_profile(db, user_id=999)
    assert md is None
    assert version == 0


async def test_get_digest_prefs_returns_defaults_for_missing_user(
    db: aiosqlite.Connection,
):
    prefs = await users_repo.get_digest_prefs(db, user_id=999)
    assert prefs == (False, 7)
