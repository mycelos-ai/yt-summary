import aiosqlite

from app.repos import chat as chat_repo
from app.repos import videos as videos_repo


async def _video(db: aiosqlite.Connection) -> None:
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_append_and_history(db: aiosqlite.Connection):
    await _video(db)
    await chat_repo.append(db, "v1", "user", "what's it about?")
    await chat_repo.append(db, "v1", "assistant", "summary text")
    msgs = await chat_repo.history(db, "v1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "summary text"


async def test_history_empty_for_unknown_video(db: aiosqlite.Connection):
    assert await chat_repo.history(db, "nope") == []


async def test_append_uses_default_user(db: aiosqlite.Connection):
    await _video(db)
    msg = await chat_repo.append(db, "v1", "user", "hi")
    cursor = await db.execute(
        "SELECT user_id FROM chat_messages WHERE id=?", (msg.id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_append_accepts_explicit_user(db: aiosqlite.Connection):
    await _video(db)
    msg = await chat_repo.append(db, "v1", "user", "hi", user_id=7)
    cursor = await db.execute(
        "SELECT user_id FROM chat_messages WHERE id=?", (msg.id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 7
