from app.models import SynthesisStatus
from app.repos import syntheses as syntheses_repo
from app.repos import synthesis_messages as sm_repo


async def _thread(db, query="q"):
    s = await syntheses_repo.create_pending(
        db, user_id=1, query=query, source_ids=["v1"],
    )
    return s.id


async def test_append_user_is_ready(db):
    sid = await _thread(db)
    m = await sm_repo.append(
        db, synthesis_id=sid, role="user", content="hello",
        status=SynthesisStatus.READY,
    )
    assert m.role == "user"
    assert m.content == "hello"
    assert m.status == SynthesisStatus.READY


async def test_append_assistant_pending_then_ready(db):
    sid = await _thread(db)
    m = await sm_repo.append(
        db, synthesis_id=sid, role="assistant", content=None,
        status=SynthesisStatus.PENDING,
    )
    assert m.status == SynthesisStatus.PENDING
    assert m.content is None
    await sm_repo.mark_ready(db, message_id=m.id, content="the answer")
    got = await sm_repo.get(db, m.id)
    assert got.status == SynthesisStatus.READY
    assert got.content == "the answer"


async def test_mark_failed(db):
    sid = await _thread(db)
    m = await sm_repo.append(
        db, synthesis_id=sid, role="assistant", content=None,
        status=SynthesisStatus.PENDING,
    )
    await sm_repo.mark_failed(db, message_id=m.id, error="boom")
    got = await sm_repo.get(db, m.id)
    assert got.status == SynthesisStatus.FAILED
    assert got.error == "boom"


async def test_history_ordered(db):
    sid = await _thread(db)
    await sm_repo.append(db, synthesis_id=sid, role="user",
                         content="q1", status=SynthesisStatus.READY)
    await sm_repo.append(db, synthesis_id=sid, role="assistant",
                         content="a1", status=SynthesisStatus.READY)
    rows = await sm_repo.history(db, synthesis_id=sid)
    assert [(r.role, r.content) for r in rows] == [
        ("user", "q1"), ("assistant", "a1"),
    ]


async def test_first_pending_assistant(db):
    sid = await _thread(db)
    await sm_repo.append(db, synthesis_id=sid, role="user",
                         content="q", status=SynthesisStatus.READY)
    pend = await sm_repo.append(db, synthesis_id=sid, role="assistant",
                                content=None, status=SynthesisStatus.PENDING)
    found = await sm_repo.first_pending(db, synthesis_id=sid)
    assert found is not None and found.id == pend.id
