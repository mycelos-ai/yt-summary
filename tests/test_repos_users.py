import aiosqlite

from app.repos import users as users_repo


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
