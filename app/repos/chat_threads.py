import aiosqlite

# The three lookup predicates mirror PR 1's partial unique indexes
# (uq_chat_threads_source / _source_speaker / _speaker). We SELECT with
# the same WHERE shape, then INSERT only when absent — get-or-create
# under the per-scope NULL-safe uniqueness.
_LOOKUP = {
    "source": (
        "SELECT id FROM chat_threads "
        "WHERE user_id=? AND scope='source' AND source_id=?",
        lambda uid, source_id, speaker_id: (uid, source_id),
    ),
    "source_speaker": (
        "SELECT id FROM chat_threads "
        "WHERE user_id=? AND scope='source_speaker' AND source_id=? AND speaker_id=?",
        lambda uid, source_id, speaker_id: (uid, source_id, speaker_id),
    ),
    "speaker": (
        "SELECT id FROM chat_threads "
        "WHERE user_id=? AND scope='speaker' AND speaker_id=?",
        lambda uid, source_id, speaker_id: (uid, speaker_id),
    ),
}


async def get_or_create(
    db: aiosqlite.Connection,
    *,
    user_id: int = 1,
    scope: str,
    source_id: str | None = None,
    speaker_id: int | None = None,
) -> int:
    if scope not in _LOOKUP:
        raise ValueError(f"unknown thread scope: {scope!r}")
    if scope == "source" and (source_id is None or speaker_id is not None):
        raise ValueError("scope='source' requires source_id and no speaker_id")
    if scope == "speaker" and (speaker_id is None or source_id is not None):
        raise ValueError("scope='speaker' requires speaker_id and no source_id")
    if scope == "source_speaker" and (source_id is None or speaker_id is None):
        raise ValueError("scope='source_speaker' requires both source_id and speaker_id")
    sql, args_fn = _LOOKUP[scope]
    cur = await db.execute(sql, args_fn(user_id, source_id, speaker_id))
    row = await cur.fetchone()
    if row is not None:
        return row["id"]
    cur = await db.execute(
        "INSERT INTO chat_threads (user_id, scope, source_id, speaker_id) "
        "VALUES (?, ?, ?, ?)",
        (user_id, scope, source_id, speaker_id),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid
