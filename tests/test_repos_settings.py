import aiosqlite

from app.repos import settings as settings_repo


async def test_get_returns_none_when_unset(db: aiosqlite.Connection):
    assert await settings_repo.get(db, "llm_model") is None


async def test_set_then_get(db: aiosqlite.Connection):
    await settings_repo.set(db, "llm_model", "openai/gpt-4o")
    assert await settings_repo.get(db, "llm_model") == "openai/gpt-4o"


async def test_set_overwrites(db: aiosqlite.Connection):
    await settings_repo.set(db, "llm_model", "a")
    await settings_repo.set(db, "llm_model", "b")
    assert await settings_repo.get(db, "llm_model") == "b"


async def test_get_all_returns_dict(db: aiosqlite.Connection):
    await settings_repo.set(db, "k1", "v1")
    await settings_repo.set(db, "k2", "v2")
    # init_schema seeds `embedding_dim_migrated=384` for the
    # 768→384 migration; subset-check rather than equality so we
    # don't couple this test to migration internals.
    actual = await settings_repo.get_all(db)
    assert actual["k1"] == "v1"
    assert actual["k2"] == "v2"


async def test_delete(db: aiosqlite.Connection):
    await settings_repo.set(db, "k", "v")
    await settings_repo.delete(db, "k")
    assert await settings_repo.get(db, "k") is None


async def test_settings_isolated_per_user(db: aiosqlite.Connection):
    # Default user is 1
    await settings_repo.set(db, "model", "user1-value")
    # Insert a row for user 2 directly
    await db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (2, 'model', 'user2-value')"
    )
    await db.commit()
    # The repo's get/set/get_all is implicitly user 1.
    assert await settings_repo.get(db, "model") == "user1-value"
    all_settings = await settings_repo.get_all(db)
    assert all_settings.get("model") == "user1-value"
