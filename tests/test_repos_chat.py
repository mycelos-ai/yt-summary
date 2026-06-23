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


async def test_append_and_history_by_thread_with_null_video(db: aiosqlite.Connection):
    await db.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
    await db.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
    await db.commit()
    await chat_repo.append(db, None, "user", "hi", thread_id=1)
    await chat_repo.append(db, None, "assistant", "yo", thread_id=1)
    msgs = await chat_repo.history(db, thread_id=1)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].video_id is None


async def test_history_both_none_raises(db: aiosqlite.Connection):
    import pytest
    with pytest.raises(ValueError, match="history requires video_id or thread_id"):
        await chat_repo.history(db)


async def test_history_no_thread_excludes_threaded_rows(db: aiosqlite.Connection):
    """No-thread history(video_id) must not return rows that belong to a thread.

    This is the regression gate for the persona→video-chat leak fix.
    The threadless branch (ELSE) must filter AND thread_id IS NULL so that
    persona/thread-scoped rows with the same video_id cannot bleed into
    the normal video-chat history, regardless of their video_id value.
    """
    # Seed video and a valid chat_threads row (FK target).
    await _video(db)
    await db.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'P','p')")
    await db.execute(
        "INSERT INTO chat_threads (id, user_id, scope, speaker_id) VALUES (99,1,'speaker',1)"
    )
    await db.commit()

    # A normal video-chat row (thread_id=NULL) — must appear in history(video_id).
    await chat_repo.append(db, "v1", "user", "VIDEO_MSG", user_id=1)

    # A thread-scoped row that carries the same video_id AND a thread_id —
    # simulates the pre-workaround leak shape (or a future mistake).
    await db.execute(
        "INSERT INTO chat_messages (user_id, video_id, role, content, thread_id) "
        "VALUES (1,'v1','assistant','PERSONA_MSG',99)"
    )
    await db.commit()

    # history(video_id) with no thread_id must return ONLY the normal row.
    msgs = await chat_repo.history(db, "v1")
    contents = [m.content for m in msgs]
    assert contents == ["VIDEO_MSG"], (
        f"Expected only VIDEO_MSG but got: {contents!r}. "
        "Thread-scoped row leaked into the no-thread video-chat history."
    )

    # The thread_id branch must still return the threaded row.
    thread_msgs = await chat_repo.history(db, thread_id=99)
    thread_contents = [m.content for m in thread_msgs]
    assert "PERSONA_MSG" in thread_contents, (
        f"thread_id branch should return PERSONA_MSG but got: {thread_contents!r}"
    )
