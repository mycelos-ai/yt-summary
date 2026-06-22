import asyncio
from app.repos import speakers as repo


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_normalize_name_key():
    assert repo.normalize_name_key("Chamath  Palihapitiya!") == "chamath palihapitiya"
    assert repo.normalize_name_key("CHAMATH") == "chamath"


def test_resolve_speaker_upserts_same_person(db):
    async def go():
        a = await repo.resolve_speaker(db, name="Chamath Palihapitiya")
        b = await repo.resolve_speaker(db, name="chamath  palihapitiya")  # same key
        c = await repo.resolve_speaker(db, name="Jason Calacanis")
        assert a == b
        assert a != c
        sp = await repo.get_speaker(db, a)
        assert sp.name == "Chamath Palihapitiya"   # first spelling wins
        assert sp.is_active is False
    _run(go())


async def _upsert_known_speaker(db, *, name, role, avatar_id, style_note):
    """Helper: upsert a known_speakers row and return its id.

    Uses ON CONFLICT(name_key) so it is safe even when the seed loader has
    already inserted the same name_key (e.g. 'lex fridman' is in the seed
    JSON and init_schema runs the seed on every fixture setup).
    """
    key = repo.normalize_name_key(name)
    await db.execute(
        "INSERT INTO known_speakers (name, name_key, role, avatar_id, style_note) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(name_key) DO UPDATE SET "
        "role=excluded.role, avatar_id=excluded.avatar_id, style_note=excluded.style_note",
        (name, key, role, avatar_id, style_note),
    )
    await db.commit()
    cur = await db.execute(
        "SELECT id FROM known_speakers WHERE name_key=?", (key,)
    )
    row = await cur.fetchone()
    return row["id"]


def test_resolve_speaker_inherits_known_speaker_identity(db):
    async def go():
        ks_id = await _upsert_known_speaker(
            db,
            name="Lex Fridman",
            role="host",
            avatar_id="lex_av",
            style_note="calm and probing",
        )
        sp_id = await repo.resolve_speaker(db, name="Lex Fridman")
        sp = await repo.get_speaker(db, sp_id)
        assert sp.known_speaker_id == ks_id
        assert sp.avatar_id == "lex_av"
        assert sp.style_note == "calm and probing"
        assert sp.role == "host"  # inherited from known_speakers (caller passed no role)
    _run(go())


def test_resolve_speaker_caller_role_wins(db):
    async def go():
        ks_id = await _upsert_known_speaker(
            db,
            name="Lex Fridman",
            role="host",
            avatar_id="lex_av",
            style_note="calm and probing",
        )
        sp_id = await repo.resolve_speaker(db, name="Lex Fridman", role="guest")
        sp = await repo.get_speaker(db, sp_id)
        assert sp.role == "guest"  # caller-provided role wins over seed role
        assert sp.known_speaker_id == ks_id  # identity still inherited
        assert sp.avatar_id == "lex_av"
        assert sp.style_note == "calm and probing"
    _run(go())


def test_resolve_speaker_no_known_match_is_null(db):
    async def go():
        sp_id = await repo.resolve_speaker(db, name="Random Person")
        sp = await repo.get_speaker(db, sp_id)
        assert sp.known_speaker_id is None
        assert sp.avatar_id is None
        assert sp.style_note is None
    _run(go())
