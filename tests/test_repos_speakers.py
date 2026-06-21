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
